"""Twitch VOD import.

yt-dlp does the talking to Twitch (listing a channel's past broadcasts, getting each VOD's
playlist URLs), then Lorekeeper downloads the VOD itself:

  Twitch serves VODs as HLS: a playlist of ~10-second chunks (fragmented MP4 or MPEG-TS).
  We fetch only the chunks that cover the part you asked for, 8 at a time, and stream them in
  order straight into FFmpeg, which remuxes (no re-encode) into a normal MP4. So grabbing hour 3
  of an 8-hour stream downloads ~1/8th of it, and nothing is ever stored twice.
"""
from __future__ import annotations

import queue
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import httpx

from .config import LIBRARY_DIR, ffmpeg_bin, popen_kwargs
from .jobs import Cancelled, Job

QUALITIES = {"1080": 1080, "720": 720, "480": 480, "360": 360}


class TwitchError(RuntimeError):
    pass


class _Quiet:
    """yt-dlp prints errors to stderr even when quiet; we surface them ourselves instead."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ydl(extra: dict | None = None):
    import yt_dlp
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "logger": _Quiet(),
                             **(extra or {})})


def parse_channel(text: str) -> str | None:
    t = text.strip()
    m = re.search(r"twitch\.tv/(?!videos/)([A-Za-z0-9_]{2,25})", t)
    if m:
        return m.group(1).lower()
    return t.lower() if re.fullmatch(r"[A-Za-z0-9_]{2,25}", t) else None


def parse_vod(text: str) -> str | None:
    m = re.search(r"twitch\.tv/videos/(\d+)", text) or re.fullmatch(r"v?(\d{6,})", text.strip())
    return m.group(1) if m else None


def list_vods(channel: str, limit: int = 20) -> list[dict]:
    ch = parse_channel(channel)
    if not ch:
        raise TwitchError("That doesn't look like a Twitch channel name")
    try:
        with _ydl({"extract_flat": "in_playlist", "playlistend": limit}) as y:
            info = y.extract_info(f"https://www.twitch.tv/{ch}/videos?filter=archives&sort=time", download=False)
    except Exception as exc:
        raise TwitchError(f"Couldn't list {ch}'s VODs: {_short(exc)}") from exc
    flat = [e for e in (info.get("entries") or []) if e]
    if not flat:
        return []

    def enrich(e: dict) -> dict:
        out = {"id": str(e.get("id", "")).lstrip("v"), "title": e.get("title") or "Untitled",
               "duration": e.get("duration"), "views": e.get("view_count"), "url": e.get("url"),
               "channel": ch, "date": None, "thumbnail": None}
        try:
            with _ydl() as y:
                d = y.extract_info(e["url"], download=False)
            out["date"] = d.get("upload_date")
            out["thumbnail"] = d.get("thumbnail")
            out["channel_name"] = d.get("uploader")
        except Exception:
            pass
        return out

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(enrich, flat[:limit]))


def _short(exc: Exception) -> str:
    msg = str(exc).replace("ERROR: ", "")
    if "subscriber" in msg.lower() or "sub-only" in msg.lower():
        return "this VOD is subscriber-only"
    return msg.splitlines()[0][:200]


def _pick_format(info: dict, quality: str) -> dict:
    want = QUALITIES.get(str(quality), 720)
    fmts = [f for f in info.get("formats", []) if f.get("height") and f.get("vcodec") != "none"
            and str(f.get("protocol", "")).startswith("m3u8")]
    if not fmts:
        raise TwitchError("No downloadable video streams for this VOD")
    under = [f for f in fmts if f["height"] <= want]
    pool = under or fmts
    return max(pool, key=lambda f: (f["height"], f.get("fps") or 0, f.get("tbr") or 0))


def _playlist(url: str, headers: dict) -> tuple[str | None, list[tuple[float, str]], bool]:
    r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
    r.raise_for_status()
    init, segs, dur = None, [], None
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            init = urljoin(url, m.group(1)) if m else None
        elif line.startswith("#EXTINF:"):
            dur = float(line.split(":", 1)[1].split(",")[0])
        elif line and not line.startswith("#"):
            segs.append((dur or 10.0, urljoin(url, line)))
            dur = None
        elif line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise TwitchError("This VOD is encrypted and can't be downloaded")
    return init, segs, "#EXT-X-ENDLIST" in r.text


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")[:120] or "vod"


def download(job: Job, url_or_id: str, quality: str = "720", start: float | None = None,
             end: float | None = None, workers: int = 8) -> str:
    vid = parse_vod(url_or_id)
    url = f"https://www.twitch.tv/videos/{vid}" if vid else url_or_id
    job.update(0.0, "Asking Twitch for the stream")
    try:
        with _ydl() as y:
            info = y.extract_info(url, download=False)
    except Exception as exc:
        raise TwitchError(_short(exc)) from exc
    if info.get("is_live"):
        raise TwitchError("That channel is live right now; wait for the VOD to finish")
    fmt = _pick_format(info, quality)
    headers = {k: v for k, v in (fmt.get("http_headers") or {}).items() if k.lower() in ("user-agent", "accept-language")}
    init, segs, finished = _playlist(fmt["url"], headers)
    if not segs:
        raise TwitchError("The VOD playlist is empty")

    # pick the chunks that cover [start, end]
    t, chosen, first_t = 0.0, [], None
    for d, u in segs:
        if (start is None or t + d > start) and (end is None or t < end):
            chosen.append(u)
            first_t = t if first_t is None else first_t
        t += d
    total_secs = t
    if not chosen:
        raise TwitchError("That time range is outside the VOD")
    channel = info.get("uploader") or info.get("channel") or "twitch"
    date = info.get("upload_date") or time.strftime("%Y%m%d")
    rng = "" if start is None and end is None else f" ({_hms(first_t or 0)}-{_hms(min(end or total_secs, total_secs))})"
    out_dir = LIBRARY_DIR / "twitch" / _safe(channel)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{date[:4]}-{date[4:6]}-{date[6:]} {_safe(info.get('title') or 'VOD')} [{vid or info.get('id')}]{rng} {fmt['height']}p.mp4"
    part = out.with_suffix(".part.mp4")

    # FFmpeg reads the chunks in order from stdin and remuxes; downloads run ahead in parallel
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0", "-map", "0:v:0?", "-map", "0:a:0?",
           "-c", "copy", "-movflags", "+faststart", str(part)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs())
    job.proc = proc
    client = httpx.Client(headers=headers, timeout=httpx.Timeout(20, read=60), follow_redirects=True,
                          limits=httpx.Limits(max_connections=workers + 2))
    got_bytes = [0]
    t0 = time.time()

    def fetch(u: str) -> bytes:
        for attempt in range(5):
            if job.cancelled:
                raise Cancelled()
            try:
                r = client.get(u)
                if r.status_code == 403 and "-muted" not in u:   # some chunks only exist as -muted after DMCA muting
                    r = client.get(u.replace(".mp4", "-muted.mp4").replace(".ts", "-muted.ts"))
                r.raise_for_status()
                got_bytes[0] += len(r.content)
                return r.content
            except Cancelled:
                raise
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
        return b""

    try:
        if init:
            proc.stdin.write(fetch(init))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending: queue.Queue = queue.Queue()
            it = iter(chosen)
            for u in [next(it, None) for _ in range(workers * 2)]:
                if u:
                    pending.put(pool.submit(fetch, u))
            done = 0
            while not pending.empty():
                fut = pending.get()
                data = fut.result()
                proc.stdin.write(data)
                done += 1
                nxt = next(it, None)
                if nxt:
                    pending.put(pool.submit(fetch, nxt))
                el = time.time() - t0
                rate = got_bytes[0] / max(0.1, el)
                eta = (len(chosen) - done) * (el / done) if done else 0
                job.update(0.02 + 0.93 * done / len(chosen),
                           f"Downloading {fmt['height']}p: chunk {done}/{len(chosen)}, "
                           f"{got_bytes[0] / 1024 ** 2:.0f} MB at {rate / 1024 ** 2:.1f} MB/s, {_eta(eta)} left")
        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            raise TwitchError("FFmpeg couldn't assemble the VOD: " + proc.stderr.read().decode(errors="replace")[-300:])
    except BaseException:
        try:
            proc.kill()
        except Exception:
            pass
        part.unlink(missing_ok=True)
        raise
    finally:
        client.close()
        job.proc = None
    part.replace(out)
    job.update(0.97, "Adding to your library")
    return str(out)


def _hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600}h{t % 3600 // 60:02d}m" if t >= 3600 else f"{t // 60}m{t % 60:02d}s"


def _eta(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}m {sec % 60:02d}s" if sec >= 60 else f"{sec}s"
