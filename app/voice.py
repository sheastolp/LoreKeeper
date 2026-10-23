"""Voiceprints: figure out *who* is talking, and when.

Pipeline for one VOD (done once, cached in SQLite):
  Whisper word timestamps → speech regions → 2 s windows every 1 s
  → 80-dim log-mel filterbank (Kaldi-style, mean-normalised)
  → CAM++ speaker network (3D-Speaker, VoxCeleb) → 512-number voiceprint per window

A *person* is the average voiceprint of a few seconds of them talking (plus the raw
sample windows). Finding them in any analysed VOD is then just a matrix multiply:
cosine similarity of every window against the person, smoothed over time.

On 2-second windows of real speech (LibriSpeech, 16 speakers) this model separates
voices with ~2.7% equal error rate. Stream audio is harder (game sound and music sit
under every voice and make *everyone* look alike), so matching adds two tricks:
  • background normalisation: subtract the average voiceprint of the windows that sound
    least like the person, which cancels what all windows share (room, mic, game audio)
  • self-adaptation: the most confident matches in *this* VOD join the person's samples,
    so a voice enrolled from a clean clip still locks on through stream noise
  • a per-VOD threshold from a two-group mixture fit, so it works for a guest who talks 10%
    of the time and for the streamer who talks 90% of it
On a test VOD built from real recorded speech over game noise and music, that took window-level
error from ~20% to ~2% for both the streamer and a guest.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from .config import MODELS_DIR, resource_root
from .db import db
from .jobs import Job

SR = 16000
WIN, HOP = 2.0, 1.0
MODEL = {
    "repo": "csukuangfj/speaker-embedding-models",
    "file": "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
    "size": 29596978,
    "sha256": "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b",
}
MODEL_ID = "campplus-en-vox"


class VoiceError(RuntimeError):
    pass


# ------------------------------------------------------------------ model

def model_path() -> Path:
    bundled = resource_root() / "vendor" / "voice" / MODEL["file"]
    if bundled.exists():
        return bundled
    local = MODELS_DIR / "voice" / MODEL["file"]
    if not local.exists():
        from .models import download_file   # dev runs without fetch_vendor: fetch once
        local.parent.mkdir(parents=True, exist_ok=True)
        download_file(MODEL, local.parent)
    return local


_sess_lock = threading.Lock()
_sess = None
_meta: dict = {}


def session():
    global _sess, _meta
    with _sess_lock:
        if _sess is None:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2)))
            so.log_severity_level = 3
            _sess = ort.InferenceSession(str(model_path()), so, providers=["CPUExecutionProvider"])
            _meta = dict(_sess.get_modelmeta().custom_metadata_map)
        return _sess


def fbank(wave: np.ndarray) -> np.ndarray:
    """Kaldi-compatible 80-bin log-mel filterbank, then per-utterance mean normalisation."""
    import kaldi_native_fbank as knf
    session()
    scale = 1.0 if _meta.get("normalize_samples", "1") == "1" else 32768.0
    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0
    opts.frame_opts.samp_freq = SR
    opts.frame_opts.snip_edges = False
    opts.mel_opts.num_bins = 80
    f = knf.OnlineFbank(opts)
    f.accept_waveform(SR, (wave * scale).astype(np.float32).tolist())
    f.input_finished()
    feats = np.array([f.get_frame(i) for i in range(f.num_frames_ready)], dtype=np.float32)
    return feats - feats.mean(axis=0, keepdims=True)


def embed_segments(pcm: np.ndarray, spans: list[tuple[float, float]], batch: int = 16,
                   progress=None) -> np.ndarray:
    """L2-normalised voiceprints for each (start, end) span of 16 kHz int16 PCM."""
    sess = session()
    iname = sess.get_inputs()[0].name
    out = np.zeros((len(spans), int(_meta.get("output_dim", 512))), dtype=np.float32)
    # batch spans of equal length together (almost all are exactly WIN seconds)
    groups: dict[int, list[int]] = {}
    for i, (a, b) in enumerate(spans):
        groups.setdefault(int(round((b - a) * SR)), []).append(i)
    done = 0
    for n_samp, idxs in groups.items():
        for k in range(0, len(idxs), batch):
            chunk = idxs[k:k + batch]
            feats = []
            for i in chunk:
                a = int(spans[i][0] * SR)
                seg = pcm[a:a + n_samp].astype(np.float32) / 32768.0
                if len(seg) < n_samp:
                    seg = np.pad(seg, (0, n_samp - len(seg)))
                feats.append(fbank(seg))
            e = sess.run(None, {iname: np.stack(feats)})[0]
            out[chunk] = e / np.maximum(np.linalg.norm(e, axis=1, keepdims=True), 1e-9)
            done += len(chunk)
            if progress:
                progress(done / len(spans))
    return out


# ------------------------------------------------------------------ speech regions → windows

def speech_regions(words: list[dict], duration: float, gap: float = 0.35) -> list[tuple[float, float]]:
    regions: list[list[float]] = []
    for w in words:
        s, e = float(w["s"]), float(w["e"])
        if e <= s:
            continue
        if regions and s - regions[-1][1] <= gap:
            regions[-1][1] = max(regions[-1][1], e)
        else:
            regions.append([s, e])
    return [(max(0.0, a - 0.1), min(duration, b + 0.15)) for a, b in regions]


def energy_regions(pcm: np.ndarray, frame: float = 0.25) -> list[tuple[float, float]]:
    """Fallback when there's no transcript: frames clearly above the noise floor."""
    n = int(frame * SR)
    if len(pcm) < n:
        return []
    frames = pcm[: len(pcm) // n * n].astype(np.float32).reshape(-1, n) / 32768.0
    db_ = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    thr = np.percentile(db_, 20) + 8
    active = db_ > thr
    regions, start = [], None
    for i, on in enumerate(active):
        if on and start is None:
            start = i
        elif not on and start is not None:
            regions.append((start * frame, i * frame))
            start = None
    if start is not None:
        regions.append((start * frame, len(active) * frame))
    merged: list[list[float]] = []
    for a, b in regions:
        if merged and a - merged[-1][1] < 0.5:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= 0.8]


def make_windows(regions: list[tuple[float, float]]) -> list[tuple[float, float]]:
    wins = []
    for a, b in regions:
        length = b - a
        if length < 1.0:
            continue
        if length <= WIN:
            wins.append((round(a, 3), round(b, 3)))
            continue
        t = a
        while t + WIN <= b + 1e-6:
            wins.append((round(t, 3), round(t + WIN, 3)))
            t += HOP
        if b - (t - HOP + WIN) > 0.5:            # cover the tail
            wins.append((round(b - WIN, 3), round(b, 3)))
    return wins


def window_loudness(pcm: np.ndarray, spans: list[tuple[float, float]]) -> np.ndarray:
    out = np.zeros(len(spans), dtype=np.float32)
    for i, (a, b) in enumerate(spans):
        seg = pcm[int(a * SR):int(b * SR)].astype(np.float32) / 32768.0
        out[i] = 20 * np.log10(np.sqrt((seg ** 2).mean()) + 1e-9) if len(seg) else -90
    return out


# ------------------------------------------------------------------ per-VOD index

def has_index(sid: str) -> bool:
    return db.one("SELECT 1 FROM voice_index WHERE source_id=? AND model=?", (sid, MODEL_ID)) is not None


def build_index(sid: str, job: Job | None = None, p0: float = 0.0, p1: float = 1.0) -> dict:
    from .ingest.media import decode_pcm16, fmt_ts
    src = db.source(sid)
    if not src or src["kind"] not in ("video", "audio") or not src.get("path"):
        raise VoiceError("Voiceprints need an audio or video item")
    if job:
        job.update(p0, "Voiceprints: decoding audio")
    pcm = decode_pcm16(src["path"])
    duration = len(pcm) / SR
    tr = db.transcript(sid)
    regions = speech_regions(tr["words"], duration) if tr and tr["words"] else energy_regions(pcm)
    spans = make_windows(regions)
    t0 = time.time()

    def prog(frac):
        if job:
            job.update(p0 + (p1 - p0) * frac,
                       f"Voiceprints: {int(frac * len(spans))}/{len(spans)} windows of {fmt_ts(duration)} of audio")

    embs = embed_segments(pcm, spans, progress=prog) if spans else np.zeros((0, 512), np.float32)
    loud = window_loudness(pcm, spans)
    starts = np.array([a for a, _ in spans], dtype=np.float32)
    ends = np.array([b for _, b in spans], dtype=np.float32)
    db.run("INSERT OR REPLACE INTO voice_index(source_id,model,dim,n,starts,ends,loud,embs,created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?)",
           (sid, MODEL_ID, int(embs.shape[1]), len(spans), starts.tobytes(), ends.tobytes(), loud.tobytes(),
            embs.astype(np.float16).tobytes(), time.time()))
    if job:
        job.write(f"voiceprints: {len(spans)} windows in {time.time() - t0:.1f}s")
    db.merge_meta(sid, {"voice_windows": len(spans)})
    return load_index(sid)


def load_index(sid: str) -> dict | None:
    r = db.one("SELECT * FROM voice_index WHERE source_id=? AND model=?", (sid, MODEL_ID))
    if not r:
        return None
    n, d = r["n"], r["dim"]
    return {
        "source_id": sid,
        "starts": np.frombuffer(r["starts"], dtype=np.float32),
        "ends": np.frombuffer(r["ends"], dtype=np.float32),
        "loud": np.frombuffer(r["loud"], dtype=np.float32),
        "embs": np.frombuffer(r["embs"], dtype=np.float16).reshape(n, d).astype(np.float32),
    }


def ensure_index(sid: str, job: Job | None = None, p0: float = 0.0, p1: float = 1.0) -> dict:
    return load_index(sid) or build_index(sid, job, p0, p1)


# ------------------------------------------------------------------ people

def _windows_in(index: dict, a: float, b: float) -> np.ndarray:
    mid = (index["starts"] + index["ends"]) / 2
    return np.where((mid >= a) & (mid <= b))[0]


def create_person(name: str, samples: list[dict] | None = None, embs: np.ndarray | None = None,
                  job: Job | None = None) -> dict:
    """samples: [{source_id, start, end}] stretches where only this person is talking."""
    from .ingest.media import decode_pcm16
    vecs: list[np.ndarray] = []
    seconds = 0.0
    sources: set[str] = set()
    for i, smp in enumerate(samples or []):
        sid, a, b = smp["source_id"], float(smp["start"]), float(smp["end"])
        sources.add(sid)
        seconds += max(0.0, b - a)
        idx = ensure_index(sid, job)
        hit = _windows_in(idx, a, b)
        if len(hit):
            vecs.append(idx["embs"][hit])
        elif b - a >= 0.8:               # too short for an indexed window: embed it directly
            src = db.source(sid)
            pcm = decode_pcm16(src["path"])
            vecs.append(embed_segments(pcm, [(a, b)]))
    if embs is not None and len(embs):
        vecs.append(embs)
    if not vecs:
        raise VoiceError("Those samples are too short. Pick at least a few seconds of them talking.")
    m = np.vstack(vecs)
    centroid = m.mean(axis=0)
    centroid /= np.linalg.norm(centroid)
    if len(m) > 300:
        keep = np.random.default_rng(0).choice(len(m), 300, replace=False)
        m = m[keep]
    pid = uuid.uuid4().hex[:10]
    thr = 0.0          # sensitivity offset; the actual threshold is worked out per VOD
    if seconds == 0 and embs is not None:
        seconds = float(len(embs)) * HOP
    db.run("INSERT INTO people(id,name,model,centroid,samples,n_samples,seconds,sources,threshold,created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)",
           (pid, name.strip()[:60] or "Someone", MODEL_ID, centroid.astype(np.float32).tobytes(),
            m.astype(np.float16).tobytes(), len(m), round(seconds, 1), json.dumps(sorted(sources)), thr, time.time()))
    return person(pid)


def add_samples(pid: str, samples: list[dict] | None = None, embs: np.ndarray | None = None) -> dict:
    """Teach an existing voiceprint more examples (merges and re-averages)."""
    p = load_person(pid)
    tmp = create_person("tmp", samples, embs)
    t = load_person(tmp["id"])
    db.run("DELETE FROM people WHERE id=?", (tmp["id"],))
    m = np.vstack([p["samples"], t["samples"]])
    if len(m) > 300:
        m = m[np.random.default_rng(1).choice(len(m), 300, replace=False)]
    c = p["centroid"] * p["n_samples"] + t["centroid"] * t["n_samples"]
    c /= np.linalg.norm(c)
    srcs = sorted(set(json.loads(db.one("SELECT sources FROM people WHERE id=?", (pid,))["sources"])) | set(t["sources"]))
    db.run("UPDATE people SET centroid=?, samples=?, n_samples=?, seconds=seconds+?, sources=? WHERE id=?",
           (c.astype(np.float32).tobytes(), m.astype(np.float16).tobytes(), len(m), tmp["seconds"], json.dumps(srcs), pid))
    return person(pid)


def load_person(pid: str) -> dict:
    r = db.one("SELECT * FROM people WHERE id=?", (pid,))
    if not r:
        raise VoiceError("That person doesn't exist any more")
    d = dict(r)
    d["centroid"] = np.frombuffer(r["centroid"], dtype=np.float32)
    d["samples"] = np.frombuffer(r["samples"], dtype=np.float16).reshape(r["n_samples"], -1).astype(np.float32)
    d["sources"] = json.loads(r["sources"])
    return d


def person(pid: str) -> dict:
    r = db.one("SELECT id,name,n_samples,seconds,sources,threshold,created_at FROM people WHERE id=?", (pid,))
    if not r:
        raise VoiceError("Not found")
    d = dict(r)
    d["sources"] = json.loads(d["sources"])
    return d


def people() -> list[dict]:
    return [person(r["id"]) for r in db.q("SELECT id FROM people ORDER BY created_at")]


def update_person(pid: str, name: str | None = None, threshold: float | None = None) -> dict:
    """threshold here is the sensitivity offset: -0.12 (strict) … +0.12 (loose)."""
    if name is not None:
        db.run("UPDATE people SET name=? WHERE id=?", (name.strip()[:60] or "Someone", pid))
    if threshold is not None:
        db.run("UPDATE people SET threshold=? WHERE id=?", (float(np.clip(threshold, -0.12, 0.12)), pid))
    return person(pid)


def delete_person(pid: str) -> None:
    db.run("DELETE FROM people WHERE id=?", (pid,))


# ------------------------------------------------------------------ matching

def _score(E: np.ndarray, c: np.ndarray, S: np.ndarray) -> np.ndarray:
    """Average of 'like their average voice' and 'like their 3 closest samples'. The kNN half keeps
    people recognisable when they sound different (Discord vs. in-room mic, shouting vs. talking)."""
    k = min(3, len(S))
    return ((E @ c) + np.sort(E @ S.T, axis=1)[:, -k:].mean(axis=1)) / 2


def _unit(m: np.ndarray) -> np.ndarray:
    return m / np.maximum(np.linalg.norm(m, axis=-1, keepdims=True), 1e-9)


def _robust_threshold(s: np.ndarray) -> float:
    lo = s[s <= np.quantile(s, 0.6)]
    med = np.median(lo)
    mad = np.median(np.abs(lo - med)) * 1.4826
    return float(np.clip(med + 3.0 * mad, 0.10, 0.40))


def auto_threshold(s: np.ndarray) -> float:
    """Split this VOD's scores into two groups, 'them' and 'not them', with a 2-Gaussian mixture
    (EM in 1-D) and cut where the two are equally likely. Works whether they talk 10% or 90% of
    the time; falls back to a robust 'well above the crowd' rule if the scores aren't two clear groups."""
    s = np.asarray(s, dtype=np.float64)
    if len(s) < 12:
        return _robust_threshold(s) if len(s) else 0.3
    m = np.array([np.quantile(s, 0.2), np.quantile(s, 0.85)])
    sd = np.full(2, s.std() / 2 + 1e-3)
    w = np.array([0.5, 0.5])
    for _ in range(60):
        pdf = w / sd * np.exp(-0.5 * ((s[:, None] - m) / sd) ** 2)
        r = pdf / np.maximum(pdf.sum(axis=1, keepdims=True), 1e-12)
        n = r.sum(axis=0) + 1e-9
        w, m = n / len(s), (r * s[:, None]).sum(axis=0) / n
        sd = np.sqrt((r * (s[:, None] - m) ** 2).sum(axis=0) / n) + 1e-3
    lo, hi = np.argsort(m)
    if m[hi] - m[lo] < 0.12 or w.min() < 0.04:
        return _robust_threshold(s)
    xs = np.linspace(m[lo], m[hi], 400)
    p_lo = w[lo] / sd[lo] * np.exp(-0.5 * ((xs - m[lo]) / sd[lo]) ** 2)
    p_hi = w[hi] / sd[hi] * np.exp(-0.5 * ((xs - m[hi]) / sd[hi]) ** 2)
    return float(np.clip(xs[int(np.argmax(p_hi >= p_lo))], 0.10, 0.50))


def similarity(p: dict, index: dict) -> tuple[np.ndarray, float]:
    """Per-window match score for this person in this VOD, plus the VOD's auto threshold."""
    E = index["embs"]
    if not len(E):
        return np.zeros(0, dtype=np.float32), 0.3
    S, c = p["samples"], p["centroid"]
    s0 = _score(E, c, S)
    mu = E[s0 <= np.median(s0)].mean(axis=0)            # background: the less-them half
    Ec, Sc = _unit(E - mu), _unit(S - mu)
    cc = _unit(Sc.mean(axis=0))
    s = _score(Ec, cc, Sc)
    conf = np.where(s >= max(auto_threshold(s) + 0.1, float(np.quantile(s, 0.9))))[0]
    if len(conf) >= 3:                                   # learn this VOD's version of their voice
        S2 = np.vstack([Sc, Ec[conf]])
        c2 = _unit(cc + Ec[conf].mean(axis=0))
        s = _score(Ec, c2, S2)
    return s.astype(np.float32), auto_threshold(s)


def presence(p: dict, index: dict, sensitivity: float | None = None, fill_gap: float = 2.5,
             min_len: float = 1.5) -> dict:
    """sensitivity: negative = stricter, positive = looser (shifts the auto threshold)."""
    sim, thr = similarity(p, index)
    thr -= float(p.get("threshold", 0.0) if sensitivity is None else sensitivity)
    if not len(sim):
        return {"sim": sim, "hit": np.zeros(0, bool), "segments": [], "threshold": thr}
    smooth = np.convolve(sim, np.ones(3) / 3, mode="same")     # ~3 s of context
    hit = (sim >= thr) | ((smooth >= thr) & (sim >= thr - 0.05))
    segs: list[list[float]] = []
    for i in np.where(hit)[0]:
        a, b = float(index["starts"][i]), float(index["ends"][i])
        if segs and a - segs[-1][1] <= fill_gap:
            segs[-1][1] = max(segs[-1][1], b)
        else:
            segs.append([a, b])
    segs = [s for s in segs if s[1] - s[0] >= min_len]
    return {"sim": sim, "hit": hit, "segments": [(round(a, 2), round(b, 2)) for a, b in segs], "threshold": thr}


# ------------------------------------------------------------------ who's in this VOD?

def discover(sid: str, job: Job | None = None, max_speakers: int = 8, min_seconds: float = 8.0) -> list[dict]:
    """Group a VOD's voice windows into speakers (no names yet) so you can just click 'that's Dave'.

    Leader clustering makes tight micro-clusters, size-weighted centroid linkage merges them, then
    every window is re-assigned to the nearest *main* speaker. All in background-normalised space."""
    idx = ensure_index(sid, job, 0.0, 0.9)
    E0 = idx["embs"]
    if len(E0) < 4:
        return []
    E = _unit(E0 - E0.mean(axis=0))          # no target here, so normalise against the whole VOD
    rng = np.random.default_rng(0)
    pick = np.arange(len(E)) if len(E) <= 2500 else np.sort(rng.choice(len(E), 2500, replace=False))
    X = E[pick]
    cents: list[np.ndarray] = []
    members: list[list[int]] = []
    for i, x in enumerate(X):
        if cents:
            sims = np.array([c @ x for c in cents])
            j = int(sims.argmax())
            if sims[j] >= 0.45:
                members[j].append(i)
                cents[j] = _unit(X[members[j]].mean(axis=0))
                continue
        cents.append(x.copy())
        members.append([i])
    C = np.array(cents)
    W = np.array([len(m) for m in members], dtype=np.float32)
    groups = [[k] for k in range(len(C))]
    while len(groups) > 1:
        G = _unit(np.array([(C[g] * W[g, None]).sum(0) for g in groups]))
        S = G @ G.T
        np.fill_diagonal(S, -1)
        a, b = np.unravel_index(int(S.argmax()), S.shape)
        if S[a, b] < 0.25:
            break
        groups[a] += groups[b]
        groups.pop(b)
    F = _unit(np.array([(C[g] * W[g, None]).sum(0) for g in groups]))
    # keep the speakers with real talk time, then re-assign every window among just those
    lab = (E @ F.T).argmax(axis=1)
    main = [k for k in range(len(F)) if (lab == k).sum() * HOP >= min_seconds]
    F = F[main] if main else F[:0]
    S = E @ F.T if len(F) else np.zeros((len(E), 0), dtype=np.float32)
    lab = S.argmax(axis=1) if len(F) else np.full(len(E), -1)
    conf = S.max(axis=1) if len(F) else np.zeros(len(E), dtype=np.float32)
    known = [load_person(p["id"]) for p in people()]
    known_scores = [(p, *similarity(p, idx)) for p in known]
    total = float(len(E)) * HOP
    out = []
    for k in range(len(F)):
        sel = np.where((lab == k) & (conf >= 0.05))[0]
        secs = float(len(sel)) * HOP
        if secs < min_seconds:
            continue
        order = sel[np.argsort(-conf[sel])]
        samples: list[float] = []
        for i in order:                      # the most typical moments, spread across the VOD
            t = float(idx["starts"][i])
            if all(abs(t - u) > 30 for u in samples):
                samples.append(t)
            if len(samples) == 4:
                break
        match = None
        for p, sim, thr in known_scores:
            share = float((sim[sel] >= thr).mean())
            if share >= 0.5 and (match is None or share > match["agreement"]):
                match = {"id": p["id"], "name": p["name"], "agreement": round(share, 2)}
        out.append({"cluster": k, "seconds": round(secs, 1), "share": round(secs / total, 3),
                    "samples": sorted(samples), "windows": sel.tolist(), "match": match})
    if not out and len(E0) * HOP >= min_seconds:
        # nothing separable: a solo recording (normalising against one voice cancels it out)
        allw = np.arange(len(E0))
        sims = E0 @ _unit(E0.mean(axis=0))
        order = allw[np.argsort(-sims)]
        samples: list[float] = []
        for i in order:
            t = float(idx["starts"][i])
            if all(abs(t - u) > 10 for u in samples):
                samples.append(t)
            if len(samples) == 4:
                break
        match = None
        for p, sim, thr in known_scores:
            share = float((sim >= thr).mean())
            if share >= 0.5 and (match is None or share > match["agreement"]):
                match = {"id": p["id"], "name": p["name"], "agreement": round(share, 2)}
        out = [{"cluster": 0, "seconds": round(len(E0) * HOP, 1), "share": 1.0, "samples": sorted(samples),
                "windows": allw.tolist(), "match": match, "solo": True}]
    out.sort(key=lambda c: -c["seconds"])
    out = out[:max_speakers]
    for n, c in enumerate(out, 1):
        c["label"] = f"Voice {n}"
    return out


def person_from_cluster(sid: str, windows: list[int], name: str) -> dict:
    idx = ensure_index(sid)
    sel = np.array([w for w in windows if 0 <= w < len(idx["embs"])], dtype=int)
    if not len(sel):
        raise VoiceError("That voice has no windows")
    p = create_person(name, None, idx["embs"][sel])
    db.run("UPDATE people SET sources=?, seconds=? WHERE id=?", (json.dumps([sid]), float(len(sel)) * HOP, p["id"]))
    return person(p["id"])
