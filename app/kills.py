"""Kill clips: find the moments a kill notification pops up on screen.

Games draw kills differently (a kill-feed row, a centre banner, a skull, a medal), so instead of
knowing every game, Lorekeeper learns from one example:

1. Teach     pause a VOD on a frame showing your kill notification and box it → a small edge template
2. Scan      decode only that corner of the screen at a few frames a second and slide the template over
               a slightly larger search area (kill feeds stack and slide), keeping the best few matches
               per frame. Cached per VOD, so changing sensitivity or clip settings is instant.
3. Kills     a kill is a new match appearing: the number of matching rows going up
4. Moments   kills close together chain into one moment (double, triple…); each becomes a clip with
               a few seconds of lead-in, ranked by kill count, how fast they came and how loud you got
"""
from __future__ import annotations

import io
import json
import subprocess
import time
import uuid

import numpy as np

from . import ai, voice
from .config import ffmpeg_bin, popen_kwargs, run_quiet
from .db import db
from .ingest.media import derived, fmt_ts
from .jobs import Cancelled, Job

FPS = 4.0              # frames scanned per second; kill notifications stay up for a second or more
TW = 96                # template width in pixels (height follows the box's shape)
K = 6                  # best matches kept per frame (enough for a stacked kill feed)
FLOOR = 0.3            # matches weaker than this aren't stored
SCAN_VERSION = 1
LEAD, TAIL, CHAIN = 6.0, 3.0, 10.0     # seconds before the first kill / after the last / max gap within a moment
MAX_LEN = 75.0
NAMES = {2: "Double kill", 3: "Triple kill", 4: "Quad kill", 5: "Five kills"}

TITLE_SCHEMA = {"type": "object", "properties": {"title": {"type": "string", "maxLength": 70}}, "required": ["title"]}
TITLE_SYSTEM = """You title gameplay clips from Twitch streams for TikTok and YouTube Shorts.
You get what happened ({what}) and what was said around it. Write a punchy title under 60 characters that
plays off the reaction or the play itself. No hashtags, no quotes, no emoji. Reply with JSON: {{"title": ...}}"""


class KillError(Exception):
    pass


# ------------------------------------------------------------------ frames

def _dims(src: dict) -> tuple[int, int]:
    m = src.get("meta") or {}
    w, h = int(m.get("width") or 0), int(m.get("height") or 0)
    if not w or not h:
        from . import ff
        info = ff.probe(src["path"])
        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if not w or not h:
        raise KillError("That item has no video picture to look at")
    return w, h


def _geometry(mk: dict, W: int, H: int) -> dict:
    """Where to crop in this VOD and how small to scale it so the box lands on the template size."""
    x, y, w, h = mk["x"], mk["y"], mk["w"], mk["h"]
    pad_x, pad_y = 0.2 * w, (mk["pad"] * h)
    x0, y0 = max(0.0, x - pad_x), max(0.0, y - pad_y)
    x1, y1 = min(1.0, x + w + pad_x), min(1.0, y + h + pad_y)
    cx, cy = int(x0 * W), int(y0 * H)
    cw, ch = max(2, int((x1 - x0) * W)), max(2, int((y1 - y0) * H))
    s = mk["tw"] / max(1.0, w * W)                     # source pixels → template pixels
    ow, oh = max(mk["tw"], int(round(cw * s / 2)) * 2), max(mk["th"], int(round(ch * s / 2)) * 2)
    return {"crop": f"crop={cw}:{ch}:{cx}:{cy}", "ow": ow, "oh": oh}


def _features(g: np.ndarray) -> np.ndarray:
    """Edge strength: kill notifications are crisp UI over a moving game, so edges beat raw brightness."""
    g = g.astype(np.float32)
    out = np.zeros_like(g)
    out[..., :-1, :] += np.abs(np.diff(g, axis=-2))
    out[..., :, :-1] += np.abs(np.diff(g, axis=-1))
    return out


def _grab_gray(path: str, t: float, vf: str, ow: int, oh: int) -> np.ndarray:
    r = run_quiet([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, t):.3f}", "-i", path,
                   "-frames:v", "1", "-vf", f"{vf},scale={ow}:{oh}:flags=area,format=gray",
                   "-f", "rawvideo", "pipe:1"])
    if r.returncode != 0 or len(r.stdout) < ow * oh:
        raise KillError("Couldn't read that frame of the video")
    return np.frombuffer(r.stdout[: ow * oh], np.uint8).reshape(oh, ow)


# ------------------------------------------------------------------ matching

class Matcher:
    """Normalised cross-correlation of one template over a batch of frames, via FFT + integral images."""

    def __init__(self, tmpl: np.ndarray, oh: int, ow: int):
        self.th, self.tw = tmpl.shape
        self.oh, self.ow = oh, ow
        t = tmpl - tmpl.mean()
        self.tnorm = float(np.sqrt((t ** 2).sum())) or 1.0
        pad = np.zeros((oh, ow), np.float32)
        pad[: self.th, : self.tw] = t
        self.tf = np.conj(np.fft.rfft2(pad))
        self.n = self.th * self.tw

    def ncc(self, frames: np.ndarray) -> np.ndarray:
        """frames: (B, oh, ow) features → (B, oh-th+1, ow-tw+1) scores in [-1, 1]."""
        th, tw = self.th, self.tw
        vr, vc = self.oh - th + 1, self.ow - tw + 1
        corr = np.fft.irfft2(np.fft.rfft2(frames) * self.tf, s=(self.oh, self.ow))[:, :vr, :vc]
        ii = np.pad(frames, ((0, 0), (1, 0), (1, 0))).cumsum(1).cumsum(2)
        ii2 = np.pad(frames.astype(np.float64) ** 2, ((0, 0), (1, 0), (1, 0))).cumsum(1).cumsum(2)

        def box(a):
            return a[:, th:th + vr, tw:tw + vc] - a[:, :vr, tw:tw + vc] - a[:, th:th + vr, :vc] + a[:, :vr, :vc]
        s1, s2 = box(ii), box(ii2)
        var = np.maximum(s2 - s1.astype(np.float64) ** 2 / self.n, 0)
        den = np.sqrt(var) * self.tnorm
        return np.where(den > 1e-3 * self.tnorm * np.sqrt(self.n), corr / np.maximum(den, 1e-9), 0).astype(np.float32)

    def peaks(self, scores: np.ndarray) -> np.ndarray:
        """Best K non-overlapping matches per frame (a kill feed shows several rows at once)."""
        B = scores.shape[0]
        out = np.zeros((B, K), np.float32)
        ry, rx = max(1, int(self.th * 0.6)), max(1, int(self.tw * 0.5))
        for b in range(B):
            m = scores[b].copy()
            for k in range(K):
                i = int(m.argmax())
                v = float(m.flat[i])
                if v < FLOOR:
                    break
                out[b, k] = v
                r, c = divmod(i, m.shape[1])
                m[max(0, r - ry):r + ry + 1, max(0, c - rx):c + rx + 1] = -1
        return out


# ------------------------------------------------------------------ markers

def _marker_row(r) -> dict:
    d = dict(r)
    d["template"] = np.frombuffer(d["template"], np.float32).reshape(d["th"], d["tw"])
    return d


def load_marker(mid: str) -> dict:
    r = db.one("SELECT * FROM kill_markers WHERE id=?", (mid,))
    if not r:
        raise KillError("That kill marker is gone")
    return _marker_row(r)


def _public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("template", "preview")}


def markers() -> list[dict]:
    return [_public(dict(r)) for r in db.q("SELECT * FROM kill_markers ORDER BY created_at")]


def marker(mid: str) -> dict:
    return _public(load_marker(mid))


def create_marker(name: str, source_id: str, t: float, box: dict, feed: bool = False) -> dict:
    src = db.source(source_id)
    if not src or src["kind"] != "video" or not src.get("path"):
        raise KillError("Pick a video to teach from")
    x, y = float(np.clip(box["x"], 0, 1)), float(np.clip(box["y"], 0, 1))
    w, h = float(np.clip(box["w"], 0, 1 - x)), float(np.clip(box["h"], 0, 1 - y))
    W, H = _dims(src)
    if w * W < 12 or h * H < 8:
        raise KillError("That box is too small. Draw it around the whole kill notification.")
    th = int(np.clip(round(TW * (h * H) / (w * W)), 8, 160))
    mk = {"x": x, "y": y, "w": w, "h": h, "tw": TW, "th": th, "pad": 2.5 if feed else 0.4}
    cw, ch, cx, cy = max(2, int(w * W)), max(2, int(h * H)), int(x * W), int(y * H)
    gray = _grab_gray(src["path"], t, f"crop={cw}:{ch}:{cx}:{cy}", TW, th)
    tmpl = _features(gray)
    if tmpl.std() < 2.0:
        raise KillError("There's nothing in that box. Pause on a frame where the kill notification is showing.")
    mid = uuid.uuid4().hex[:10]
    db.run("INSERT INTO kill_markers(id,name,source_id,t,x,y,w,h,pad,tw,th,template,preview,threshold,created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (mid, name.strip() or "Kill", source_id, float(t), x, y, w, h, mk["pad"], TW, th,
            tmpl.astype(np.float32).tobytes(), _png(gray), 0.0, time.time()))
    return marker(mid)


def _png(gray: np.ndarray) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(gray).save(buf, "PNG")
    return buf.getvalue()


def update_marker(mid: str, name: str | None = None, threshold: float | None = None) -> dict:
    if name is not None and name.strip():
        db.run("UPDATE kill_markers SET name=? WHERE id=?", (name.strip(), mid))
    if threshold is not None:
        db.run("UPDATE kill_markers SET threshold=? WHERE id=?", (float(np.clip(threshold, -0.2, 0.2)), mid))
    return marker(mid)


def delete_marker(mid: str) -> None:
    db.run("DELETE FROM kill_markers WHERE id=?", (mid,))


# ------------------------------------------------------------------ scanning

def _cache(sid: str, mid: str):
    return derived(sid) / f"kills_{mid}_v{SCAN_VERSION}.npz"


def load_scan(sid: str, mid: str) -> dict | None:
    p = _cache(sid, mid)
    if not p.exists():
        return None
    z = np.load(p)
    return {"t": z["t"], "peaks": z["peaks"]}


def scan(sid: str, mk: dict, job: Job | None = None, p0: float = 0.0, p1: float = 1.0) -> dict:
    """Best template matches per sampled frame across the whole VOD (cached)."""
    hit = load_scan(sid, mk["id"])
    if hit:
        return hit
    src = db.source(sid)
    if not src or src["kind"] != "video" or not src.get("path"):
        raise KillError("Kill clips need a video")
    W, H = _dims(src)
    g = _geometry(mk, W, H)
    ow, oh = g["ow"], g["oh"]
    dur = float(src.get("duration") or 0)
    m = Matcher(mk["template"], oh, ow)
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", src["path"], "-an", "-sn", "-dn",
           "-vf", f"fps={FPS},{g['crop']},scale={ow}:{oh}:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **popen_kwargs())
    if job:
        job.proc = proc
    size, batch = ow * oh, 64
    peaks: list[np.ndarray] = []
    n = 0
    t0 = time.time()
    try:
        while True:
            buf = proc.stdout.read(size * batch)  # type: ignore[union-attr]
            got = len(buf) // size
            if got:
                frames = np.frombuffer(buf[: got * size], np.uint8).reshape(got, oh, ow)
                peaks.append(m.peaks(m.ncc(_features(frames))))
                n += got
                if job:
                    secs = n / FPS
                    job.update(p0 + (p1 - p0) * (min(1.0, secs / dur) if dur else 0.5),
                               f"{src['title']}: watching for kills, {fmt_ts(secs)} of {fmt_ts(dur)}")
            if len(buf) < size * batch:
                break
    except Cancelled:
        proc.kill()
        raise
    finally:
        proc.wait()
        if job:
            job.proc = None
    if job and job.cancelled:
        raise Cancelled()
    if not n:
        raise KillError(f"{src['title']}: couldn't decode any video frames")
    P = np.concatenate(peaks)
    T = (np.arange(len(P), dtype=np.float32) + 0.5) / FPS
    np.savez_compressed(_cache(sid, mk["id"]), t=T, peaks=P.astype(np.float16))
    if job:
        job.write(f"kill scan {src['title']}: {n} frames in {time.time() - t0:.1f}s")
    return {"t": T, "peaks": P.astype(np.float16)}


def auto_threshold(top: np.ndarray) -> float:
    """Split the best-match scores into 'nothing there' and 'marker showing' (Otsu), never below 0.55."""
    s = top[top > FLOOR]
    if len(s) < 20:
        return 0.6
    hist, edges = np.histogram(s, bins=50, range=(FLOOR, 1.0))
    p = hist / hist.sum()
    mids = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(p)
    mu = np.cumsum(p * mids)
    var = (mu[-1] * w0 - mu) ** 2 / np.maximum(w0 * (1 - w0), 1e-9)
    thr = float(mids[int(np.argmax(var[:-1]))])
    return float(np.clip(thr, 0.55, 0.8))


def kill_times(sc: dict, mk: dict, sensitivity: float | None = None) -> tuple[list[float], float]:
    """A kill = the number of matches on screen going up (a new row in the feed, or the banner popping up)."""
    P = sc["peaks"].astype(np.float32)
    looser = (mk.get("threshold") or 0.0) if sensitivity is None else sensitivity
    thr = auto_threshold(P[:, 0]) - looser
    cnt = (P >= thr).sum(1)
    if len(cnt) >= 3:                                   # 3-frame median: drop single-frame flickers
        c = np.stack([np.r_[cnt[:1], cnt[:-1]], cnt, np.r_[cnt[1:], cnt[-1:]]])
        cnt = np.median(c, 0).astype(int)
    out: list[float] = []
    back = max(1, int(round(0.75 * FPS)))               # a match that blinks out and back within this isn't new
    for i in range(1, len(cnt)):
        new = int(cnt[i]) - int(cnt[max(0, i - back):i].max())
        if cnt[i] > cnt[i - 1] and new > 0:
            out.extend([float(sc["t"][i])] * new)
    if len(cnt) and cnt[0] > 0:
        out[:0] = [float(sc["t"][0])] * int(cnt[0])
    return out, thr


def moments(times: list[float], duration: float) -> list[dict]:
    out: list[dict] = []
    for t in times:
        if out and t - out[-1]["kills"][-1] <= CHAIN and t - out[-1]["kills"][0] <= MAX_LEN - LEAD - TAIL:
            out[-1]["kills"].append(t)
        else:
            out.append({"kills": [t]})
    for m in out:
        a, b = m["kills"][0], m["kills"][-1]
        m["start"], m["end"] = max(0.0, a - LEAD), min(duration or b + TAIL, b + TAIL)
        if m["end"] - m["start"] < 10:
            m["start"] = max(0.0, m["end"] - 10)
    return out


# ------------------------------------------------------------------ main job

def _hype(idx: dict | None, a: float, b: float) -> float:
    if not idx or not len(idx["loud"]):
        return 0.0
    from .clips import _robust_z
    mid = (idx["starts"] + idx["ends"]) / 2
    sel = np.where((mid >= a) & (mid <= b))[0]
    if not len(sel):
        return 0.0
    z = _robust_z(idx["loud"])
    return float(np.sort(z[sel])[-3:].mean())


def _transcript(tr: dict | None, a: float, b: float) -> str:
    if not tr:
        return ""
    lines = [s for s in tr["segments"] if s["end"] > a and s["start"] < b]
    return "\n".join(f"{i} [{fmt_ts(s['start'])}] Stream: {s['text'].strip()}" for i, s in enumerate(lines))


def _what(n: int, span: float) -> str:
    if n == 1:
        return "Kill"
    return f"{NAMES.get(n, f'{n} kills')} in {max(1, round(span))} s"


def find(job: Job, marker_id: str, source_ids: list[str], per_vod: int = 6, min_kills: int = 1,
         use_ai: bool = True) -> dict:
    mk = load_marker(marker_id)
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    can_ai = use_ai and ai.available()
    notes: list[str] = []
    per: dict = {}
    total = 0
    n = len(source_ids)
    for vi, sid in enumerate(source_ids):
        base, span = vi / n, 1 / n
        src = db.source(sid)
        if not src or src["kind"] != "video":
            continue
        try:
            sc = scan(sid, mk, job, base, base + span * 0.85)
        except KillError as exc:
            notes.append(str(exc))
            continue
        dur = float(src.get("duration") or sc["t"][-1])
        times, thr = kill_times(sc, mk)
        ms = [m for m in moments(times, dur) if len(m["kills"]) >= min_kills]
        per[sid] = {"title": src["title"], "kills": len(times), "clips": 0}
        if not times:
            notes.append(f"{src['title']}: didn't spot the kill marker (try a looser sensitivity, or teach it from this VOD).")
            continue
        idx = voice.load_index(sid)
        tr = db.transcript(sid)
        for m in ms:
            k = m["kills"]
            fight = max(1.0, k[-1] - k[0])
            m["hype"] = _hype(idx, m["start"], m["end"])
            speed = min(1.0, (len(k) - 1) / (fight / 4)) if len(k) > 1 else 0.0
            m["score"] = round(0.6 * min(1.0, len(k) / 4) + 0.2 * float(np.clip(m["hype"] / 4, 0, 1)) + 0.2 * speed, 4)
        ms.sort(key=lambda m: -m["score"])
        for ci, m in enumerate(ms[:per_vod]):
            k = m["kills"]
            what = _what(len(k), k[-1] - k[0])
            block = _transcript(tr, m["start"], m["end"])
            title = what
            if can_ai and block:
                job.update(base + span * (0.85 + 0.15 * ci / max(1, per_vod)), f"{src['title']}: titling {what.lower()} at {fmt_ts(k[0])}")
                try:
                    j = json.loads(ai.chat_once(
                        [{"role": "system", "content": TITLE_SYSTEM.format(what=what.lower())},
                         {"role": "user", "content": f"{what} at {fmt_ts(k[0])} in \"{src['title']}\".\n\n{block}"}],
                        max_tokens=80, temperature=0.5, schema=TITLE_SCHEMA))
                    title = (j.get("title") or what).strip().strip('"')[:70]
                except Exception as exc:
                    job.write(f"AI titling failed at {fmt_ts(k[0])}: {exc}")
            why = f"{what} at {fmt_ts(k[0])}" + (", loud reaction" if m["hype"] > 2 else "")
            signals = {"kills": len(k), "kill_times": [round(t - m["start"], 2) for t in k],
                       "hype_z": round(m["hype"], 2), "threshold": round(thr, 3)}
            db.run("INSERT INTO clips(id,run_id,person_id,marker_id,source_id,start,end,score,ai_score,title,why,signals,transcript,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex[:10], run_id, None, marker_id, sid, round(m["start"], 2), round(m["end"], 2),
                    m["score"], None, title, why, json.dumps(signals), block, time.time()))
            total += 1
            per[sid]["clips"] += 1
    job.update(1.0, f"Found {total} kill clip{'s' if total != 1 else ''}")
    return {"run_id": run_id, "clips": total, "per_vod": per, "notes": notes, "ai": can_ai, "person": mk["name"], "kind": "kills"}


def timeline(sid: str, mid: str, bins: int = 260) -> dict:
    """Kill ticks for the VOD strip, from the cached scan only (never starts one)."""
    sc = load_scan(sid, mid)
    if not sc:
        return {"ready": False}
    mk = load_marker(mid)
    times, _thr = kill_times(sc, mk)
    src = db.source(sid) or {}
    dur = float(src.get("duration") or sc["t"][-1])
    return {"ready": True, "duration": dur, "kills": [round(t, 2) for t in times]}
