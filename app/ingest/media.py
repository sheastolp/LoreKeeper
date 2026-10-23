"""Audio/video ingestion: probe, previews, Whisper transcription, keyframe captions."""
from __future__ import annotations

import io
import os
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

from .. import ff
from ..config import DERIVED_DIR, MODELS_DIR, ffmpeg_bin, get_settings, popen_kwargs, resource_root, run_quiet
from ..jobs import Job


def derived(sid: str) -> Path:
    d = DERIVED_DIR / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def fmt_ts(t: float | None) -> str:
    if t is None:
        return ""
    t = max(0, int(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ------------------------------------------------------------------ previews

def grab_frame(path: str, t: float, width: int = 768) -> bytes | None:
    r = run_quiet([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, t):.3f}", "-i", path,
                   "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "3",
                   "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"])
    return r.stdout if r.returncode == 0 and r.stdout else None


def make_thumbnail(sid: str, path: str, info: dict) -> None:
    dur = info.get("duration") or 0
    data = grab_frame(path, min(dur * 0.1, 5.0) if dur else 0, width=480)
    if data:
        (derived(sid) / "thumb.jpg").write_bytes(data)


def make_filmstrip(sid: str, path: str, info: dict, n: int = 10) -> None:
    """10 evenly spaced frames tiled into one strip — the timeline paints clips with it."""
    from PIL import Image
    dur = info.get("duration") or 0
    if not dur:
        return
    tiles = []
    for i in range(n):
        data = grab_frame(path, dur * (i + 0.5) / n, width=320)
        if not data:
            continue
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im = _cover(im, 160, 90)
        tiles.append(im)
    if not tiles:
        return
    strip = Image.new("RGB", (160 * len(tiles), 90))
    for i, im in enumerate(tiles):
        strip.paste(im, (160 * i, 0))
    strip.save(derived(sid) / "filmstrip.jpg", quality=80)


def _cover(im, w, h):
    scale = max(w / im.width, h / im.height)
    im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    left, top = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((left, top, left + w, top + h))


def make_waveform(sid: str, path: str) -> None:
    out = derived(sid) / "waveform.png"
    run_quiet([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-filter_complex",
               "aformat=channel_layouts=mono,showwavespic=s=3000x120:colors=#E8DFC8:scale=sqrt",
               "-frames:v", "1", str(out)])


def make_proxy(sid: str, path: str, info: dict, job: Job, p0: float, p1: float) -> str | None:
    """Make a browser-playable copy only when needed — and remux instead of re-encoding when possible.

    OBS records H.264/AAC into .mkv by default. Chromium can decode both codecs,
    it just won't open the Matroska container, so a stream copy into MP4 is
    instant. Only HEVC/ProRes/10-bit sources pay for a real encode.
    """
    if ff.browser_playable(path, info):
        return None
    d = derived(sid)
    dur = info.get("duration")
    if info.get("has_video"):
        out = d / "proxy.mp4"
        v_ok = info.get("vcodec") in ff.BROWSER_VCODECS and info.get("pix_fmt") in ("yuv420p", "yuvj420p")
        a_ok = (not info.get("has_audio")) or info.get("acodec") in {"aac", "mp3"}
        args = ["-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
        if v_ok and info.get("vcodec") == "h264":
            args += ["-c:v", "copy"]
            label = "Remuxing for preview (no re-encode)"
        else:
            args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "27",
                     "-vf", "scale='min(1280,iw)':-2", "-pix_fmt", "yuv420p"]
            label = "Encoding preview proxy"
        args += ["-c:a", "copy"] if a_ok else ["-c:a", "aac", "-b:a", "160k"]
        args += ["-movflags", "+faststart", str(out)]
    else:
        out = d / "proxy.m4a"
        label = "Converting audio for preview"
        args = ["-i", path, "-vn", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    ff.run(args, job=job, duration=dur, p0=p0, p1=p1, label=label)
    return str(out)


# ------------------------------------------------------------------ whisper

_whisper_lock = threading.Lock()
_whisper_cache: dict = {}


def load_whisper():
    s = get_settings()
    size, device = s["whisper_model"], s["whisper_device"]
    key = (size, device)
    with _whisper_lock:
        if key in _whisper_cache:
            return _whisper_cache[key]
        from faster_whisper import WhisperModel
        root = str(MODELS_DIR / "whisper")
        bundled = resource_root() / "vendor" / "whisper" / size
        if (bundled / "model.bin").exists():      # shipped in the installer: works offline from day one
            size = str(bundled)
        threads = max(1, (os.cpu_count() or 4) - 1)
        if device in ("cuda", "auto"):
            try:
                m = WhisperModel(size, device="cuda", compute_type="float16", download_root=root)
                _whisper_cache[key] = m
                return m
            except Exception:
                if device == "cuda":
                    raise
        m = WhisperModel(size, device="cpu", compute_type="int8", download_root=root, cpu_threads=threads)
        _whisper_cache[key] = m
        return m


def decode_pcm16(path: str) -> np.ndarray:
    """Decode to 16 kHz mono int16 (half the RAM of float32 for long VODs)."""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path,
                           "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                          capture_output=True, **popen_kwargs())
    if proc.returncode != 0:
        raise RuntimeError("Couldn't decode audio: " + proc.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(proc.stdout, dtype=np.int16)


def transcribe(path: str, duration: float | None, job: Job, p0: float, p1: float) -> dict:
    job.update(p0, "Loading Whisper model (first run downloads it)")
    model = load_whisper()
    job.update(p0, "Decoding audio")
    pcm = decode_pcm16(path)
    total = len(pcm) / 16000 or (duration or 1)
    lang = get_settings()["whisper_language"] or None

    window = 3600 if total <= 3600 else 900   # bound float32 RAM on multi-hour streams
    segments: list[dict] = []
    words: list[dict] = []
    detected = None
    for offset in range(0, int(np.ceil(total)), window):
        piece = pcm[offset * 16000:(offset + window) * 16000].astype(np.float32) / 32768.0
        if piece.size < 1600:
            continue
        segs, info = model.transcribe(piece, language=lang, word_timestamps=True, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 500}, beam_size=5)
        detected = detected or info.language
        for seg in segs:
            start, end = seg.start + offset, seg.end + offset
            text = seg.text.strip()
            if not text:
                continue
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
            for w in seg.words or []:
                words.append({"w": w.word, "s": round(w.start + offset, 3), "e": round(w.end + offset, 3),
                              "p": round(w.probability, 3)})
            job.update(p0 + (p1 - p0) * min(1.0, end / total),
                       f"Transcribing {fmt_ts(end)} / {fmt_ts(total)}")
    return {"language": detected or lang or "?", "segments": segments, "words": words}


# ------------------------------------------------------------------ keyframes

def scene_times(path: str, threshold: float = 0.3) -> list[float]:
    """Scene-change candidates, decoding *only keyframes* (-skip_frame nokey).

    Decoding every frame of a 3-hour stream is the slow part of scene detection;
    I-frames alone are ~1-2% of frames and encoders tend to place them on cuts.
    """
    try:
        r = run_quiet([ffmpeg_bin(), "-hide_banner", "-nostdin", "-skip_frame", "nokey", "-i", path,
                       "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo",
                       "-fps_mode", "vfr", "-an", "-f", "null", "-"], timeout=600)
    except subprocess.TimeoutExpired:
        return []
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr.decode(errors="replace"))]


def pick_keyframes(duration: float, scenes: list[float], n: int) -> list[float]:
    """Greedy max-min spacing over scene cuts + a uniform grid, so frames cover the whole video."""
    if duration <= 0 or n <= 0:
        return []
    grid = [duration * (i + 0.5) / n for i in range(n)]
    pool = sorted(set([round(t, 2) for t in scenes if 0.5 < t < duration - 0.5] + [round(g, 2) for g in grid]))
    chosen: list[float] = [grid[0]]
    while len(chosen) < n and pool:
        best = max(pool, key=lambda t: min(abs(t - c) for c in chosen))
        if min(abs(best - c) for c in chosen) < 2.0:
            break
        chosen.append(best)
        pool.remove(best)
    return sorted(chosen)


FRAME_PROMPT = (
    "This is a still frame from a video at timestamp {ts}. Describe it for a searchable index: "
    "the setting, people or characters, objects, actions and mood. Then transcribe ANY visible text "
    "(captions, slides, UI, code, signs, chat overlays) exactly as written, prefixed with 'Text:'. "
    "Be factual and specific, under 120 words. No preamble."
)


def caption_keyframes(sid: str, path: str, duration: float, job: Job, p0: float, p1: float,
                      warnings: list[str]) -> list[dict]:
    from .. import ai
    s = get_settings()
    n = min(int(s["max_keyframes"]), max(3, int(duration // 20) + 3))
    if n <= 0:
        return []
    job.update(p0, "Finding scene changes")
    times = pick_keyframes(duration, scene_times(path), n)
    frames_dir = derived(sid) / "frames"
    frames_dir.mkdir(exist_ok=True)
    chunks = []
    for i, t in enumerate(times):
        job.update(p0 + (p1 - p0) * i / max(1, len(times)), f"Looking at frame {i + 1}/{len(times)} ({fmt_ts(t)})")
        jpg = grab_frame(path, t)
        if not jpg:
            continue
        (frames_dir / f"{int(t * 1000):09d}.jpg").write_bytes(jpg)
        try:
            desc = ai.describe_image(jpg, FRAME_PROMPT.format(ts=fmt_ts(t)))
        except Exception as exc:  # vision model missing/unreachable: keep the transcript, note it
            warnings.append(f"Frame captions skipped: {exc}")
            break
        if desc:
            chunks.append({"kind": "frame", "text": f"[On screen at {fmt_ts(t)}] {desc}", "start": t, "end": t})
    return chunks
