"""Quick edits, cleanup and segment cuts — each op is a small FFmpeg recipe.

Every op runs through Ctx, which can either execute or just *record* the
commands. That's how the UI shows the exact FFmpeg command under each tool
before you press Render.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import ff
from ..config import TMP_DIR, ffmpeg_bin, get_settings
from ..db import db
from ..jobs import Job

VIDEO_CONTAINERS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_CODEC_FOR_EXT = {
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".ogg": ["-c:a", "libopus", "-b:a", "128k"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
}
H264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "192k"]


class Ctx:
    def __init__(self, job: Job | None = None, dry: bool = False):
        self.job, self.dry = job, dry
        self.commands: list[str] = []
        self.outputs: list[str] = []
        self.explain = ""

    def ffmpeg(self, args: list[str], duration: float | None = None, label: str = "",
               p0: float = 0.0, p1: float = 1.0) -> None:
        self.commands.append(ff.pretty_cmd([ffmpeg_bin(), *args]))
        if not self.dry:
            ff.run(args, job=self.job, duration=duration, p0=p0, p1=p1, label=label)

    def update(self, p: float, msg: str) -> None:
        if self.job and not self.dry:
            self.job.update(p, msg)


# ------------------------------------------------------------------ helpers

def _src(sid: str) -> dict:
    s = db.source(sid)
    if not s:
        raise ValueError("Source not found")
    if s["kind"] not in ("video", "audio"):
        raise ValueError(f"'{s['title']}' isn't audio or video")
    if not s.get("path") or not Path(s["path"]).exists():
        raise FileNotFoundError(f"Original file missing: {s.get('path')}")
    return s


def _info(s: dict) -> dict:
    m = s["meta"]
    if "has_audio" not in m:
        m = {**m, **ff.probe(s["path"])}
    return m


def export_path(stem: str, label: str, ext: str) -> str:
    out_dir = Path(get_settings()["export_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip() or "export"
    base = out_dir / f"{safe} - {label} {time.strftime('%H%M%S')}{ext}"
    n = 2
    while base.exists():
        base = out_dir / f"{safe} - {label} {time.strftime('%H%M%S')} ({n}){ext}"
        n += 1
    return str(base)


def _same_ext(s: dict, info: dict) -> str:
    ext = Path(s["path"]).suffix.lower()
    if info.get("has_video"):
        return ext if ext in VIDEO_CONTAINERS else ".mp4"
    return ext if ext in AUDIO_CODEC_FOR_EXT else ".m4a"


def _codecs(info: dict, ext: str) -> list[str]:
    if info.get("has_video") and ext in VIDEO_CONTAINERS:
        if ext == ".webm":
            return ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1", "-deadline", "good",
                    "-cpu-used", "4", "-c:a", "libopus", "-b:a", "160k"]
        return H264 + AAC + (["-movflags", "+faststart"] if ext in (".mp4", ".mov", ".m4v") else [])
    return AUDIO_CODEC_FOR_EXT.get(ext, AAC)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ------------------------------------------------------------------ basic cuts

def trim(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    dur = info.get("duration") or 0
    a = _clamp(float(p.get("start", 0)), 0, dur)
    b = _clamp(float(p.get("end", dur)), a + 0.05, dur) if dur else float(p.get("end", 0))
    ext = _same_ext(s, info)
    out = export_path(s["title"], "trim", ext)
    if p.get("precise", True):
        ctx.explain = ("Precise mode re-encodes, so the cut lands on the exact frame you picked. "
                       "Input seeking (-ss before -i) jumps to the nearest keyframe, then decodes forward to your in-point.")
        ctx.ffmpeg(["-ss", f"{a:.3f}", "-i", s["path"], "-t", f"{b - a:.3f}", "-map", "0:v:0?", "-map", "0:a:0?",
                    *_codecs(info, ext), out], duration=b - a, label="Trimming")
    else:
        ctx.explain = ("Fast mode stream-copies: no re-encode, no quality loss, done in seconds. Catch: compressed video "
                       "can only start on a keyframe (the start of a GOP), so the in-point may snap up to a few seconds early.")
        ctx.ffmpeg(["-ss", f"{a:.3f}", "-i", s["path"], "-t", f"{b - a:.3f}", "-map", "0", "-c", "copy",
                    "-avoid_negative_ts", "make_zero", out], duration=b - a, label="Copying")
    ctx.outputs.append(out)


def split(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    dur = info.get("duration") or 0
    at = _clamp(float(p["at"]), 0.05, max(0.05, dur - 0.05))
    ext = _same_ext(s, info)
    codecs = _codecs(info, ext) if p.get("precise", True) else ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    o1, o2 = export_path(s["title"], "part 1", ext), export_path(s["title"], "part 2", ext)
    maps = ["-map", "0:v:0?", "-map", "0:a:0?"]
    ctx.explain = "Two passes over the same file: [0 → split] and [split → end]. Same keyframe rules as Trim apply in fast mode."
    ctx.ffmpeg(["-i", s["path"], "-t", f"{at:.3f}", *maps, *codecs, o1], duration=at, label="Part 1", p1=0.5)
    ctx.ffmpeg(["-ss", f"{at:.3f}", "-i", s["path"], *maps, *codecs, o2], duration=dur - at, label="Part 2", p0=0.5)
    ctx.outputs += [o1, o2]


def merge(ctx: Ctx, p: dict) -> None:
    ids = p.get("source_ids") or []
    if len(ids) < 2:
        raise ValueError("Pick at least two clips to merge")
    srcs = [_src(i) for i in ids]
    infos = [_info(s) for s in srcs]
    any_video = any(i.get("has_video") for i in infos)
    total = sum(i.get("duration") or 0 for i in infos)
    args: list[str] = []
    for s in srcs:
        args += ["-i", s["path"]]
    graph, pads = [], []
    if any_video:
        first = next(i for i in infos if i.get("has_video"))
        W, H = int(first.get("width") or 1920), int(first.get("height") or 1080)
        W, H = W - W % 2, H - H % 2
        F = first.get("fps") or 30
        for n, info in enumerate(infos):
            d = info.get("duration") or 1
            if info.get("has_video"):
                graph.append(f"[{n}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={F},format=yuv420p[v{n}]")
            else:
                graph.append(f"color=c=black:s={W}x{H}:r={F}:d={d:.3f},format=yuv420p[v{n}]")
            graph.append(_audio_pad(n, info, d))
            pads.append(f"[v{n}][a{n}]")
        graph.append(f"{''.join(pads)}concat=n={len(srcs)}:v=1:a=1[v][a]")
        ext = ".mp4"
        out = export_path(srcs[0]["title"], "merged", ext)
        ctx.ffmpeg([*args, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
                    *H264, *AAC, "-movflags", "+faststart", out], duration=total, label="Merging")
        ctx.explain = (f"Every clip is conformed to {W}x{H} @ {F:g} fps (letterboxed, never stretched) and 48 kHz stereo, "
                       "then the concat filter stitches them. Conforming first is what lets you mix phone clips, "
                       "OBS recordings and silent clips without the merge choking.")
    else:
        for n, info in enumerate(infos):
            graph.append(_audio_pad(n, info, info.get("duration") or 1))
            pads.append(f"[a{n}]")
        graph.append(f"{''.join(pads)}concat=n={len(srcs)}:v=0:a=1[a]")
        ext = _same_ext(srcs[0], infos[0])
        out = export_path(srcs[0]["title"], "merged", ext)
        ctx.ffmpeg([*args, "-filter_complex", ";".join(graph), "-map", "[a]", *_codecs(infos[0], ext), out],
                   duration=total, label="Merging")
        ctx.explain = "Each track is resampled to 48 kHz stereo float, then concatenated sample-accurately."
    ctx.outputs.append(out)


def _audio_pad(n: int, info: dict, d: float) -> str:
    if info.get("has_audio"):
        return f"[{n}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]"
    return f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.3f},aformat=sample_fmts=fltp[a{n}]"


def _atempo_chain(f: float) -> str:
    parts = []
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def speed(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    f = _clamp(float(p.get("factor", 1.5)), 0.25, 4.0)
    ext = _same_ext(s, info)
    out = export_path(s["title"], f"{f:g}x", ext)
    args = ["-i", s["path"]]
    fl = []
    if info.get("has_video"):
        fl.append(f"[0:v:0]setpts=PTS/{f:.4f}[v]")
    if info.get("has_audio"):
        fl.append(f"[0:a:0]{_atempo_chain(f)}[a]")
    args += ["-filter_complex", ";".join(fl)]
    if info.get("has_video"):
        args += ["-map", "[v]"]
    if info.get("has_audio"):
        args += ["-map", "[a]"]
    ctx.explain = ("Video: setpts rescales every frame's timestamp. Audio: atempo uses WSOLA time-stretching, "
                   "so speech speeds up without the chipmunk pitch shift (chained because one atempo covers 0.5–2×).")
    ctx.ffmpeg([*args, *_codecs(info, ext), out], duration=(info.get("duration") or 0) / f, label="Retiming")
    ctx.outputs.append(out)


def volume(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    db_gain = _clamp(float(p.get("gain_db", 3)), -40, 30)
    ext = _same_ext(s, info)
    out = export_path(s["title"], f"vol {db_gain:+g}dB", ext)
    vcopy = ["-map", "0:v:0?", "-c:v", "copy"] if info.get("has_video") else []
    acodec = AUDIO_CODEC_FOR_EXT.get(ext, AAC) if not info.get("has_video") else (
        ["-c:a", "libopus", "-b:a", "160k"] if ext == ".webm" else AAC)
    ctx.explain = (f"{db_gain:+g} dB multiplies the waveform by {10 ** (db_gain / 20):.2f}×. Decibels are logarithmic: "
                   "+6 dB ≈ double the amplitude, +10 dB ≈ 'twice as loud' to human ears. Video is stream-copied untouched.")
    ctx.ffmpeg(["-i", s["path"], *vcopy, "-map", "0:a:0", "-af", f"volume={db_gain}dB,alimiter=limit=0.97",
                *acodec, out], duration=info.get("duration"), label="Adjusting volume")
    ctx.outputs.append(out)


CONVERT = {
    "mp4": (".mp4", "video"), "mov": (".mov", "video"), "mkv": (".mkv", "video"), "webm": (".webm", "video"),
    "gif": (".gif", "gif"), "mp3": (".mp3", "audio"), "wav": (".wav", "audio"), "flac": (".flac", "audio"),
    "m4a": (".m4a", "audio"), "ogg": (".ogg", "audio"),
}
QUALITY_CRF = {"high": 18, "balanced": 23, "small": 28}


def convert(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    fmt = p.get("format", "mp4")
    if fmt not in CONVERT:
        raise ValueError(f"Unknown format {fmt}")
    ext, family = CONVERT[fmt]
    height = int(p.get("height") or 0)
    crf = QUALITY_CRF.get(p.get("quality", "balanced"), 23)
    out = export_path(s["title"], fmt, ext)
    dur = info.get("duration")
    if family == "audio" or not info.get("has_video"):
        if family != "audio":
            raise ValueError("That file has no video stream — pick an audio format")
        ctx.explain = {
            "mp3": "MP3 VBR -q:a 2 (~190 kbps): the universally playable choice.",
            "wav": "Uncompressed 16-bit PCM — biggest files, zero generation loss, ideal for further editing.",
            "flac": "Lossless compression: bit-identical to WAV at roughly 50–60% of the size.",
            "m4a": "AAC in an MP4 audio container — what phones and Apple gear prefer.",
            "ogg": "Opus at 128 kbps — the most efficient codec here; transparent for most listeners.",
        }[fmt]
        ctx.ffmpeg(["-i", s["path"], "-vn", "-map", "0:a:0", *AUDIO_CODEC_FOR_EXT[ext], out], duration=dur,
                   label=f"Converting to {fmt.upper()}")
    elif family == "gif":
        h = height or min(int(info.get("height") or 480), 480)
        ctx.explain = ("GIFs only get 256 colours per frame, so this builds a custom palette from your clip first "
                       "(palettegen) and then maps frames onto it with ordered dithering (paletteuse). "
                       "That's the difference between a crisp GIF and a banded mess.")
        ctx.ffmpeg(["-i", s["path"], "-t", "60", "-filter_complex",
                    f"[0:v:0]fps=15,scale=-2:{h}:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[pal];"
                    f"[s1][pal]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0", out],
                   duration=min(dur or 60, 60), label="Building GIF")
    else:
        vf = []
        if height and info.get("height") and height < int(info["height"]):
            vf = ["-vf", f"scale=-2:{height}:flags=lanczos"]
        if fmt == "webm":
            vcod = ["-c:v", "libvpx-vp9", "-crf", str(crf + 10), "-b:v", "0", "-row-mt", "1",
                    "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p"]
            acod = ["-c:a", "libopus", "-b:a", "160k"]
            ctx.explain = "VP9 + Opus in WebM: royalty-free, great for the web, slower to encode than H.264."
        else:
            vcod = ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p"]
            acod = AAC
            ctx.explain = (f"H.264 at CRF {crf}. CRF (constant rate factor) targets constant *perceived* quality and "
                           "lets bitrate float: calm scenes get fewer bits, action gets more. Lower CRF = better/bigger; "
                           "every +6 roughly halves the file size.")
        extra = ["-movflags", "+faststart"] if ext in (".mp4", ".mov") else []
        ctx.ffmpeg(["-i", s["path"], "-map", "0:v:0", "-map", "0:a:0?", *vf, *vcod, *acod, *extra, out],
                   duration=dur, label=f"Converting to {fmt.upper()}")
    ctx.outputs.append(out)


# ------------------------------------------------------------------ cleanup

def extract_audio(ctx: Ctx, p: dict) -> None:
    p = {**p, "format": p.get("format", "mp3")}
    s = _src(p["source_id"])
    if not _info(s).get("has_audio"):
        raise ValueError("No audio stream in this file")
    convert(ctx, p)
    ctx.explain = "Demux the audio stream, drop the video (-vn), encode. " + ctx.explain


DENOISE = {
    "light": "highpass=f=70,afftdn=nr=8:nf=-35:tn=1",
    "medium": "highpass=f=80,afftdn=nr=14:nf=-30:tn=1,lowpass=f=14000",
    "strong": "highpass=f=90,afftdn=nr=22:nf=-25:tn=1,lowpass=f=11000",
}


def denoise(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    level = p.get("strength", "medium")
    chain = DENOISE.get(level, DENOISE["medium"])
    ext = _same_ext(s, info)
    out = export_path(s["title"], f"denoised {level}", ext)
    vcopy = ["-map", "0:v:0?", "-c:v", "copy"] if info.get("has_video") else []
    acod = AAC if info.get("has_video") else AUDIO_CODEC_FOR_EXT.get(ext, AAC)
    ctx.explain = ("High-pass strips rumble (desk bumps, fans, AC hum harmonics). afftdn is an FFT denoiser: "
                   "it learns the noise floor's spectrum and subtracts it per frequency bin, with tn=1 tracking "
                   "the floor as it drifts. Push 'strong' too far and voices go watery — that's spectral subtraction artefacts.")
    ctx.ffmpeg(["-i", s["path"], *vcopy, "-map", "0:a:0", "-af", chain, *acod, out],
               duration=info.get("duration"), label="Denoising")
    ctx.outputs.append(out)


LOUDNESS = {"streaming": -14.0, "podcast": -16.0, "broadcast": -23.0}


def normalize(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    target = p.get("target", "streaming")
    I = LOUDNESS.get(target, -14.0)
    TP, LRA = -1.5, 11.0
    ext = _same_ext(s, info)
    out = export_path(s["title"], f"normalized {int(I)}LUFS", ext)
    ctx.update(0.02, "Measuring loudness (pass 1)")
    if ctx.dry:
        m = {"input_i": "<measured>", "input_tp": "<measured>", "input_lra": "<measured>",
             "input_thresh": "<measured>", "target_offset": "<measured>"}
        ctx.commands.append(ff.pretty_cmd([ffmpeg_bin(), "-i", s["path"], "-af",
                                           f"loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json", "-f", "null", "-"]))
    else:
        err = ff.capture(["-i", s["path"], "-map", "0:a:0", "-af",
                          f"loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json", "-f", "null", "-"])
        m = json.loads(err[err.rfind("{"): err.rfind("}") + 1])
        if ctx.job:
            ctx.job.write(f"measured: {m['input_i']} LUFS integrated, {m['input_tp']} dBTP peak, LRA {m['input_lra']}")
    af = (f"loudnorm=I={I}:TP={TP}:LRA={LRA}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    vcopy = ["-map", "0:v:0?", "-c:v", "copy"] if info.get("has_video") else []
    acod = AAC if info.get("has_video") else AUDIO_CODEC_FOR_EXT.get(ext, AAC)
    ctx.explain = (f"Two-pass EBU R128. Pass 1 measures integrated loudness (LUFS — loudness weighted by how human hearing "
                   f"works, gated so silence doesn't drag the average down). Pass 2 applies one linear gain to hit {I:g} LUFS "
                   f"with true peaks under {TP} dBTP. 'Linear' matters: a single static gain keeps your dynamics, instead of "
                   f"a compressor pumping. Targets: YouTube/Twitch ≈ -14, podcasts -16, broadcast TV -23.")
    ctx.ffmpeg(["-i", s["path"], *vcopy, "-map", "0:a:0", "-af", af, "-ar", "48000", *acod, out],
               duration=info.get("duration"), label="Normalizing (pass 2)", p0=0.3)
    ctx.outputs.append(out)


# ------------------------------------------------------------------ segment cuts

def keep_from_removals(removals: list[tuple[float, float]], duration: float, min_keep: float = 0.08) -> list[tuple[float, float]]:
    removals = sorted((max(0, a), min(duration, b)) for a, b in removals if b > a)
    merged: list[list[float]] = []
    for a, b in removals:
        if merged and a <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    keeps, cur = [], 0.0
    for a, b in merged:
        if a - cur >= min_keep:
            keeps.append((round(cur, 3), round(a, 3)))
        cur = b
    if duration - cur >= min_keep:
        keeps.append((round(cur, 3), round(duration, 3)))
    return keeps


def render_segments(ctx: Ctx, s: dict, info: dict, keeps: list[tuple[float, float]], label: str) -> str:
    """Stitch kept ranges with select/aselect (streams; never buffers the whole file).

    Drift-proofing: video is forced to a constant integer frame rate G, audio is resampled to
    48 kHz and re-chunked into frames of exactly 1/G s (48000/G samples), and every cut is snapped
    to that shared 1/G grid. Each segment then keeps the same number of video and audio frames,
    so a hundred cuts can't add up to lip-sync drift.
    """
    has_v, has_a = info.get("has_video"), info.get("has_audio")
    G = int(round(info.get("fps") or 30)) if has_v else 100
    if G <= 0 or G > 240:
        G = 30
    nsamp = 48000 // G if 48000 % G == 0 else 64
    snapped: list[tuple[float, float]] = []
    for a, b in keeps:
        a2, b2 = round(a * G) / G, round(b * G) / G
        if b2 - a2 >= 1 / G:
            snapped.append((a2, b2))
    if not snapped:
        raise ValueError("Nothing left to keep after the cuts")
    half = 0.5 / G
    expr = "+".join(f"gte(t,{a - half:.6f})*lt(t,{b - half:.6f})" for a, b in snapped)
    ext = _same_ext(s, info)
    out = export_path(s["title"], label, ext)
    kept = sum(b - a for a, b in snapped)
    fl = []
    if has_v:
        fl.append(f"[0:v:0]fps={G},select='{expr}',setpts=N/{G}/TB[v]")
    if has_a:
        fl.append(f"[0:a:0]aresample=48000,asetnsamples=n={nsamp}:p=0,aselect='{expr}',asetpts=N/SR/TB[a]")
    graph = ";".join(fl)
    args = ["-i", s["path"]]
    script = None
    if len(graph) > 6000:   # Windows caps a command line at 32,767 chars: hand FFmpeg a file instead
        script = TMP_DIR / f"graph_{s['id']}_{int(time.time())}.txt"
        if not ctx.dry:
            script.write_text(graph, "utf-8")
        args += ff.filter_script_args(str(script))
    else:
        args += ["-filter_complex", graph]
    if has_v:
        args += ["-map", "[v]"]
    if has_a:
        args += ["-map", "[a]"]
    ctx.ffmpeg([*args, *_codecs(info, ext), out], duration=kept, label=f"Rendering {len(snapped)} segments", p0=0.1)
    if script and not ctx.dry:
        script.unlink(missing_ok=True)
    return out


def cut_silences(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    if not info.get("has_audio"):
        raise ValueError("No audio to analyse")
    thr = float(p.get("threshold_db", -35))
    min_sil = _clamp(float(p.get("min_silence", 0.8)), 0.2, 10)
    pad = _clamp(float(p.get("padding", 0.15)), 0, 1)
    dur = info.get("duration") or 0
    ctx.update(0.02, "Listening for dead air")
    detect = ["-i", s["path"], "-map", "0:a:0", "-af", f"silencedetect=noise={thr}dB:d={min_sil}", "-f", "null", "-"]
    if ctx.dry:
        ctx.commands.append(ff.pretty_cmd([ffmpeg_bin(), *detect]))
        keeps = [(0.0, dur)]
    else:
        err = ff.capture(detect)
        starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", err)]
        ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", err)]
        if len(ends) < len(starts):
            ends.append(dur)
        removals = [(a + pad, b - pad) for a, b in zip(starts, ends) if (b - pad) - (a + pad) > 0.05]
        keeps = keep_from_removals(removals, dur)
        removed = dur - sum(b - a for a, b in keeps)
        if ctx.job:
            ctx.job.write(f"{len(removals)} silent stretches, {removed:.1f}s of dead air removed")
        if not removals:
            raise ValueError(f"No silences longer than {min_sil}s below {thr} dB — try a higher threshold (e.g. -30)")
    ctx.explain = (f"silencedetect flags every stretch quieter than {thr:g} dBFS for at least {min_sil:g}s. "
                   f"Each one is shortened by {pad:g}s of breathing room on both sides (so words don't get clipped), "
                   "and the keepers are stitched with select/aselect: a streaming filter that never buffers the whole "
                   "file, which is why it works on 3-hour VODs.")
    ctx.outputs.append(render_segments(ctx, s, info, keeps, "tightened"))


def transcript_cut(ctx: Ctx, p: dict) -> None:
    s = _src(p["source_id"])
    info = _info(s)
    tr = db.transcript(s["id"])
    if not tr:
        raise ValueError("No transcript for this file yet")
    words = tr["words"]
    deleted = sorted(set(int(i) for i in p.get("deleted", []) if 0 <= int(i) < len(words)))
    if not deleted:
        raise ValueError("Strike out some words first")
    keeps = transcript_keeps(words, deleted, info.get("duration") or words[-1]["e"])
    ctx.explain = ("Whisper gives every word a start/end time. Each run of struck-out words becomes a cut from just "
                   "after the previous kept word to just before the next one, so the gap (breath, 'um') goes too. "
                   "Frames are re-timed to a constant rate first (fps filter) so audio and video can't drift apart.")
    ctx.outputs.append(render_segments(ctx, s, info, keeps, "transcript edit"))


def transcript_keeps(words: list[dict], deleted: list[int], duration: float, pad: float = 0.04) -> list[tuple[float, float]]:
    dset = set(deleted)
    removals = []
    i = 0
    n = len(words)
    while i < n:
        if i not in dset:
            i += 1
            continue
        j = i
        while j + 1 < n and (j + 1) in dset:
            j += 1
        prev_end = words[i - 1]["e"] if i > 0 else 0.0
        next_start = words[j + 1]["s"] if j + 1 < n else duration
        a = max(prev_end + pad, words[i]["s"] - 0.25) if i > 0 else 0.0
        b = min(next_start - pad, words[j]["e"] + 0.25) if j + 1 < n else duration
        if j + 1 < n:
            b = max(b, words[j]["e"])
        removals.append((a, b))
        i = j + 1
    return keep_from_removals(removals, duration)


OPS = {
    "trim": trim, "split": split, "merge": merge, "speed": speed, "volume": volume, "convert": convert,
    "extract_audio": extract_audio, "denoise": denoise, "normalize": normalize,
    "cut_silences": cut_silences, "transcript_cut": transcript_cut,
}
