"""Multi-track timeline → one FFmpeg filter graph.

Model: video tracks are composited bottom-up over a black canvas (V1 = main,
V2 = overlay / picture-in-picture). Every clip is its own input, opened with
input seeking (-ss/-t) so FFmpeg only decodes the part you use. Each clip is
conformed (scale/pad/fps), faded with alpha, shifted to its timeline position
with setpts, and overlaid with eof_action=pass so the layer below shows through
when the clip ends. Audio from every clip is delayed into place and summed
with amix (normalize=0 keeps your levels honest), then a limiter catches peaks.
"""
from __future__ import annotations

from pathlib import Path

from .. import ff
from ..config import TMP_DIR
from ..db import db
from . import watermark
from .ops import AAC, AUDIO_CODEC_FOR_EXT, Ctx, export_path

PIP_POS = {
    "tl": ("{m}", "{m}"), "tr": ("W-w-{m}", "{m}"), "bl": ("{m}", "H-h-{m}"),
    "br": ("W-w-{m}", "H-h-{m}"), "center": ("(W-w)/2", "(H-h)/2"),
}
PRESETS = {
    "final": {"scale": 1.0, "x264": ["-preset", "medium", "-crf", "20"], "ab": "192k"},
    "fast": {"scale": 1.0, "x264": ["-preset", "veryfast", "-crf", "22"], "ab": "192k"},
    "draft": {"scale": 0.5, "x264": ["-preset", "ultrafast", "-crf", "28"], "ab": "128k"},
}


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def clip_len(c: dict) -> float:
    return max(0.0, float(c["out"]) - float(c["in"]))


def project_duration(data: dict) -> float:
    end = 0.0
    for t in data.get("tracks", []):
        if t.get("hidden"):
            continue
        for c in t.get("clips", []):
            end = max(end, float(c["start"]) + clip_len(c))
    return end


def render_project(ctx: Ctx, p: dict) -> None:
    proj = db.project(p["project_id"])
    if not proj:
        raise ValueError("Project not found")
    data = proj["data"]
    preset = PRESETS.get(p.get("preset", "final"), PRESETS["final"])
    st = data.get("settings", {})
    W = _even(int(st.get("width", 1920)) * preset["scale"])
    H = _even(int(st.get("height", 1080)) * preset["scale"])
    F = float(st.get("fps", 30))
    total = project_duration(data)
    if total <= 0:
        raise ValueError("The timeline is empty — drag clips onto a track first")

    tracks = [t for t in data.get("tracks", []) if not t.get("hidden")]
    video_tracks = [t for t in tracks if t["type"] == "video"]
    audio_tracks = [t for t in tracks if t["type"] == "audio"]
    has_visual = any(t.get("clips") for t in video_tracks)

    args: list[str] = []
    graph: list[str] = []
    audio_pads: list[str] = []
    n_in = 0
    sources: dict[str, dict] = {}

    def src(sid: str) -> dict:
        if sid not in sources:
            s = db.source(sid)
            if not s or not s.get("path") or not Path(s["path"]).exists():
                raise FileNotFoundError(f"A clip's source file is missing ({s['title'] if s else sid})")
            if "has_audio" not in s["meta"] and s["kind"] in ("video", "audio"):
                s["meta"].update(ff.probe(s["path"]))
            sources[sid] = s
        return sources[sid]

    def add_audio(n: int, c: dict, start: float, dur: float, label: str) -> None:
        vol = float(c.get("volume", 1.0))
        fi, fo = min(float(c.get("fade_in", 0)), dur / 2), min(float(c.get("fade_out", 0)), dur / 2)
        chain = [f"[{n}:a:0]aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo",
                 "asetpts=PTS-STARTPTS", f"volume={vol:.3f}"]
        if fi > 0:
            chain.append(f"afade=t=in:st=0:d={fi:.3f}")
        if fo > 0:
            chain.append(f"afade=t=out:st={dur - fo:.3f}:d={fo:.3f}")
        ms = int(round(start * 1000))
        if ms > 0:
            chain.append(f"adelay={ms}|{ms}")
        graph.append(",".join(chain) + f"[{label}]")
        audio_pads.append(f"[{label}]")

    # ---- video layers, bottom (last in list) to top
    last = "base"
    if has_visual:
        graph.append(f"color=c=black:s={W}x{H}:r={F:g}:d={total:.3f},format=yuv420p[base]")
    k = 0
    for track in reversed(video_tracks):
        is_overlay = track["id"] != "v1"
        for c in sorted(track.get("clips", []), key=lambda c: c["start"]):
            s = src(c["source_id"])
            dur, start = clip_len(c), float(c["start"])
            if dur <= 0.01:
                continue
            if s["kind"] == "image":
                args += ["-loop", "1", "-framerate", f"{F:g}", "-t", f"{dur:.3f}", "-i", s["path"]]
            else:
                args += ["-ss", f"{float(c['in']):.3f}", "-t", f"{dur:.3f}", "-i", s["path"]]
            n = n_in
            n_in += 1
            pip = c.get("pip") or {}
            scale = float(pip.get("scale", 0.3 if is_overlay else 1.0))
            full = scale >= 0.999
            if s["kind"] == "audio":
                # audio file dropped on a video track: contributes sound only
                if not track.get("muted") and not c.get("muted"):
                    add_audio(n, c, start, dur, f"va{k}")
                k += 1
                continue
            if full:
                v = (f"[{n}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black@0")
            else:
                v = f"[{n}:v:0]scale={_even(W * scale)}:-2"
            v += f",setsar=1,fps={F:g},format=yuva420p,setpts=PTS-STARTPTS"
            fi, fo = min(float(c.get("fade_in", 0)), dur / 2), min(float(c.get("fade_out", 0)), dur / 2)
            if fi > 0:
                v += f",fade=t=in:st=0:d={fi:.3f}:alpha=1"
            if fo > 0:
                v += f",fade=t=out:st={dur - fo:.3f}:d={fo:.3f}:alpha=1"
            v += f",setpts=PTS+{start:.3f}/TB[v{k}]"
            graph.append(v)
            if full:
                x, y = "0", "0"
            else:
                m = _even(W * 0.025)
                x, y = (e.format(m=m) for e in PIP_POS.get(pip.get("pos", "br"), PIP_POS["br"]))
            graph.append(f"[{last}][v{k}]overlay=x={x}:y={y}:eof_action=pass:"
                         f"enable='between(t,{start:.3f},{start + dur:.3f})'[o{k}]")
            last = f"o{k}"
            if (s["meta"].get("has_audio") and not track.get("muted") and not c.get("muted")):
                add_audio(n, c, start, dur, f"va{k}")
            k += 1

    # ---- audio tracks
    for track in audio_tracks:
        if track.get("muted"):
            continue
        for c in track.get("clips", []):
            s = src(c["source_id"])
            if not s["meta"].get("has_audio"):
                continue
            dur, start = clip_len(c), float(c["start"])
            if dur <= 0.01:
                continue
            args += ["-ss", f"{float(c['in']):.3f}", "-t", f"{dur:.3f}", "-i", s["path"]]
            add_audio(n_in, c, start, dur, f"aa{k}")
            n_in += 1
            k += 1

    # corner watermark goes on last, over every layer (expressions size it to the canvas, so draft renders match)
    wm = watermark.vf(ctx) if has_visual else None
    if wm:
        graph.append(f"[{last}]{wm}[wm]")
        last = "wm"

    graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={total:.3f}[sil]")
    audio_pads.append("[sil]")
    graph.append(f"{''.join(audio_pads)}amix=inputs={len(audio_pads)}:duration=longest:"
                 f"dropout_transition=0:normalize=0,alimiter=limit=0.95:level=false[aout]")

    full_graph = ";".join(graph)
    script = None
    if len(full_graph) > 6000:
        script = TMP_DIR / f"timeline_{proj['id']}.txt"
        if not ctx.dry:
            script.write_text(full_graph, "utf-8")
        gargs = ff.filter_script_args(str(script))
    else:
        gargs = ["-filter_complex", full_graph]

    name = proj["name"] or "timeline"
    if has_visual:
        out = export_path(name, "render" if preset is PRESETS["final"] else p.get("preset", "draft"), ".mp4")
        tail = ["-map", f"[{last}]", "-map", "[aout]", "-c:v", "libx264", *preset["x264"], "-pix_fmt", "yuv420p",
                "-r", f"{F:g}", "-c:a", "aac", "-b:a", preset["ab"], "-movflags", "+faststart"]
    else:
        ext = "." + (p.get("audio_format") or "m4a")
        ext = ext if ext in AUDIO_CODEC_FOR_EXT else ".m4a"
        out = export_path(name, "mixdown", ext)
        tail = ["-map", "[aout]", *AUDIO_CODEC_FOR_EXT.get(ext, AAC)]
    ctx.explain = (f"{n_in} inputs → one filter graph ({len(graph)} filter chains, {len(full_graph):,} chars). "
                   f"Canvas {W}x{H} @ {F:g} fps, {total:.1f}s. Each clip is input-seeked so only the used part is "
                   "decoded; video layers stack with overlay (eof_action=pass lets lower tracks show through), "
                   "audio is placed with adelay and summed by amix without auto-normalisation, then a "
                   "limiter stops the sum from clipping.")
    ctx.ffmpeg([*args, *gargs, *tail, "-t", f"{total:.3f}", out], duration=total, label="Rendering timeline")
    if script and not ctx.dry:
        script.unlink(missing_ok=True)
    ctx.outputs.append(out)
