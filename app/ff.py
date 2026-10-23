"""FFmpeg / FFprobe plumbing shared by ingestion and editing."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import ffmpeg_bin, ffprobe_bin, popen_kwargs, run_quiet
from .jobs import Cancelled, Job

BROWSER_VCODECS = {"h264", "vp8", "vp9", "av1"}
BROWSER_ACODECS = {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "pcm_f32le", "pcm_s24le"}
BROWSER_VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mov"}
BROWSER_AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".webm"}


def probe(path: str | Path) -> dict:
    r = run_quiet([ffprobe_bin(), "-v", "error", "-print_format", "json",
                   "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe couldn't read the file: {r.stderr.decode(errors='replace')[:300]}")
    data = json.loads(r.stdout or b"{}")
    fmt = data.get("format", {})
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    info = {
        "duration": float(fmt.get("duration") or 0) or None,
        "format": fmt.get("format_name"),
        "bitrate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "has_video": v is not None,
        "has_audio": a is not None,
    }
    if v:
        fps = _fps(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1")
        info.update({
            "vcodec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
            "fps": fps, "pix_fmt": v.get("pix_fmt"),
        })
        if not info["duration"] and v.get("duration"):
            info["duration"] = float(v["duration"])
    if a:
        info.update({
            "acodec": a.get("codec_name"), "sample_rate": int(a.get("sample_rate") or 0) or None,
            "channels": a.get("channels"),
        })
        if not info["duration"] and a.get("duration"):
            info["duration"] = float(a["duration"])
    return info


def _fps(rate: str) -> float | None:
    try:
        n, d = rate.split("/")
        return round(int(n) / int(d), 3) if int(d) else None
    except Exception:
        return None


def browser_playable(path: str | Path, info: dict) -> bool:
    ext = Path(path).suffix.lower()
    if info.get("has_video"):
        ok_v = info.get("vcodec") in BROWSER_VCODECS and ext in BROWSER_VIDEO_EXT
        ok_a = (not info.get("has_audio")) or info.get("acodec") in BROWSER_ACODECS
        return ok_v and ok_a and info.get("pix_fmt") in (None, "yuv420p", "yuvj420p")
    return ext in BROWSER_AUDIO_EXT and info.get("acodec") in BROWSER_ACODECS


def pretty_cmd(args: list[str]) -> str:
    """Readable command line for the UI's 'under the hood' panel (paths shortened to file names)."""
    shown = ["ffmpeg"] + [Path(a).name if _looks_like_path(a) else a for a in args[1:]]
    out = []
    for a in shown:
        if a == "" or re.search(r"[\s'\"();\[\]|&<>,]", a):
            out.append('"' + a.replace('"', '\\"') + '"')
        else:
            out.append(a)
    return " ".join(out)


def _looks_like_path(a: str) -> bool:
    return ("/" in a or "\\" in a) and not a.startswith(("[", "-")) and "=" not in a[:20]


def run(args: list[str], job: Job | None = None, duration: float | None = None,
        p0: float = 0.0, p1: float = 1.0, label: str = "", cwd: str | None = None) -> None:
    """Run ffmpeg with -progress parsing. `args` excludes the binary itself.
    cwd lets filters reference files by short relative names (no Windows drive-letter escaping)."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    if job:
        job.write("$ " + pretty_cmd(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            text=True, encoding="utf-8", errors="replace", **popen_kwargs())
    if job:
        job.proc = proc
    tail: list[str] = []

    import threading

    def drain_err():
        for line in proc.stderr:  # type: ignore[union-attr]
            tail.append(line.rstrip())
            if len(tail) > 40:
                tail.pop(0)

    t = threading.Thread(target=drain_err, daemon=True)
    t.start()
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            key, _, val = line.strip().partition("=")
            if job and duration and key in ("out_time_us", "out_time_ms"):
                try:
                    secs = int(val) / 1e6
                except ValueError:
                    continue
                frac = min(1.0, secs / duration)
                job.update(p0 + (p1 - p0) * frac, label or None)
    except Cancelled:
        proc.kill()
        raise
    proc.wait()
    t.join(timeout=2)
    if job and job.cancelled:
        raise Cancelled()
    if proc.returncode != 0:
        msg = "\n".join(l for l in tail if l.strip())[-1200:]
        if job:
            job.write(msg)
        raise RuntimeError(f"FFmpeg failed: {_last_error(tail)}")


def _last_error(lines: list[str]) -> str:
    for line in reversed(lines):
        s = line.strip()
        if s and not s.startswith(("frame=", "size=")):
            return s[:300]
    return "unknown error"


_version: tuple[int, int] | None = None


def ffmpeg_version() -> tuple[int, int]:
    global _version
    if _version is None:
        try:
            out = run_quiet([ffmpeg_bin(), "-version"]).stdout.decode(errors="replace")
            m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
            _version = (int(m.group(1)), int(m.group(2))) if m else (99, 0)   # git builds: assume new
        except Exception:
            _version = (6, 0)
    return _version


def filter_script_args(path: str) -> list[str]:
    """-filter_complex_script was deprecated in 7.1 in favour of the -/option file syntax."""
    return ["-/filter_complex", path] if ffmpeg_version() >= (7, 1) else ["-filter_complex_script", path]


def capture(args: list[str]) -> str:
    """Run ffmpeg and return stderr (used by analysis filters like loudnorm/silencedetect)."""
    r = run_quiet([ffmpeg_bin(), "-hide_banner", "-nostdin", *args])
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stderr.decode(errors="replace")
