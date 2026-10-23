"""Export a clip as 16:9 or vertical 9:16, with word-by-word captions burned in.

Vertical has two looks:
  crop  — cut a 9:16 slice out of the frame (focus slider picks where: facecam left, game centre…)
  blur  — keep the whole 16:9 frame in the middle over a blurred, zoomed copy of itself

Captions are an ASS subtitle file rendered by libass: 1–3 words at a time, the word being
spoken lit up in gold, timed from Whisper's word timestamps. The font ships with the app and is
loaded via `fontsdir`, so it renders the same on every PC.
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from ..config import TMP_DIR, resource_root
from ..db import db
from . import watermark
from .ops import AAC, Ctx, _clamp, _info, _src, export_path

FONT_NAME = "Anton"
GOLD, WHITE, BLACK = "&H005CB6DD", "&H00FFFFFF", "&H00000000"


def _fonts_dir() -> Path:
    """Copy the caption font next to the temp files so the filter can use a short relative path."""
    d = TMP_DIR / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    src = resource_root() / "assets" / "fonts" / "Anton-Regular.ttf"
    dst = d / src.name
    if src.exists() and (not dst.exists() or dst.stat().st_size != src.stat().st_size):
        shutil.copyfile(src, dst)
    return d


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(round(t * 100)), 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("{", "(").replace("}", ")").replace("\\", "/")).strip()


def chunk_words(words: list[dict], max_words: int = 3, max_chars: int = 18) -> list[list[dict]]:
    """Group words into 1-3 word caption chunks. Whisper marks the start of a new word with a leading
    space; pieces without one ("'all" in "y'all") are glued to the previous word, never split off."""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        raw = w["w"]
        txt = raw.strip()
        if not txt:
            continue
        continuation = bool(cur) and not raw[:1].isspace()
        if cur and not continuation:
            gap = w["s"] - cur[-1]["e"] > 0.5
            n_words = sum(1 for x in cur if x["w"][:1].isspace()) or 1
            size = len("".join(x["w"] for x in cur).strip()) + len(txt) + 1
            if n_words >= max_words or size > max_chars or gap:
                chunks.append(cur)
                cur = []
        cur.append(w)
        if txt[-1:] in ".?!,":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def merge_tokens(words: list[dict]) -> list[dict]:
    """Whisper sometimes splits a word into pieces (" y" + "'all"); rejoin them so a word lights up whole."""
    out: list[dict] = []
    for w in words:
        if out and w["w"] and not w["w"][:1].isspace():
            out[-1] = {"w": out[-1]["w"] + w["w"], "s": out[-1]["s"], "e": w["e"]}
        else:
            out.append(dict(w))
    return out


def build_ass(words: list[dict], start: float, end: float, width: int, height: int,
              vertical: bool, title: str | None, layout_mode: str) -> str:
    size = 118 if vertical else max(40, int(height * 0.075))
    outline = 7 if vertical else max(3, int(height * 0.005))
    margin_v = (420 if layout_mode == "blur" else 520) if vertical else int(height * 0.08)
    title_size = 84
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{FONT_NAME},{size},{WHITE},{WHITE},{BLACK},&H64000000,0,0,0,0,100,100,1,0,1,{outline},2,2,60,60,{margin_v},1",
        f"Style: Title,{FONT_NAME},{title_size},{WHITE},{WHITE},{BLACK},&H64000000,0,0,0,0,100,100,1,0,1,5,2,8,70,70,{230 if layout_mode == 'blur' else 160},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    dur = end - start
    if title and vertical:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Title,,0,0,0,,{_clean(title).upper()}")
    inside = merge_tokens([w for w in words if w["e"] > start and w["s"] < end])
    chunks = chunk_words(inside)
    for ci, chunk in enumerate(chunks):
        # keep Whisper's own spacing (leading space = new word) so contractions stay whole
        pieces = [(" " if j and w["w"][:1].isspace() else "", _clean(w["w"]).upper()) for j, w in enumerate(chunk)]
        nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else end
        for i, w in enumerate(chunk):
            a = max(w["s"], start) - start
            b = (chunk[i + 1]["s"] if i + 1 < len(chunk) else min(chunk[-1]["e"] + 0.35, nxt)) - start
            b = min(b, dur)
            if b <= a:
                continue
            body = "".join(sp + (f"{{\\c{GOLD}}}{t}{{\\c{WHITE}}}" if j == i else t) for j, (sp, t) in enumerate(pieces))
            lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{body}")
    return "\n".join(lines) + "\n"


def export_clip(ctx: Ctx, p: dict) -> None:
    clip = None
    if p.get("clip_id"):
        from .. import clips
        clip = clips.get_clip(p["clip_id"])
        sid, start, end = clip["source_id"], float(clip["start"]), float(clip["end"])
        title = p.get("title") or clip["title"]
        person = clip.get("person") or ""
    else:
        sid, start, end = p["source_id"], float(p["start"]), float(p["end"])
        title, person = p.get("title") or "", p.get("person") or ""
    s = _src(sid)
    info = _info(s)
    if not info.get("has_video"):
        raise ValueError("Clips need a video source")
    dur = end - start
    if dur < 1:
        raise ValueError("Clip is too short")
    layout = p.get("layout", "landscape")
    vertical = layout == "vertical"
    mode = p.get("vertical_mode", "crop")
    focus = _clamp(float(p.get("focus", 0.5)), 0.0, 1.0)
    W, H = int(info.get("width") or 1920), int(info.get("height") or 1080)
    fps = info.get("fps") or 30
    fps = 60 if fps > 50 else (30 if fps > 26 else fps)

    if vertical:
        OW, OH = 1080, 1920
        if mode == "blur" or W / H < 9 / 16:
            graph = ("[0:v:0]split=2[bg][fg];"
                     f"[bg]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},boxblur=24:3,"
                     "eq=brightness=-0.10:saturation=1.15[bgb];"
                     f"[fg]scale={OW}:-2:flags=lanczos[fgs];[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1")
            mode = "blur"
        else:
            cw = int(round(H * 9 / 16 / 2)) * 2
            x = int(_clamp(focus * W - cw / 2, 0, W - cw))
            graph = f"[0:v:0]crop={cw}:{H}:{x}:0,scale={OW}:{OH}:flags=lanczos,setsar=1"
        explain = (f"Vertical 1080×1920, {'blurred fill' if mode == 'blur' else f'crop focused at {int(focus * 100)}% across'}. ")
    else:
        OW, OH = W - W % 2, H - H % 2
        graph = f"[0:v:0]scale={OW}:{OH},setsar=1"
        explain = f"Straight 16:9 cut at {OW}×{OH}. "

    cwd = None
    captions = bool(p.get("captions", True))
    tr = db.transcript(sid) if captions else None
    if captions and tr and tr["words"]:
        _fonts_dir()
        name = f"clip_{uuid.uuid4().hex[:8]}.ass"
        ass = build_ass(tr["words"], start, end, OW, OH, vertical,
                        title if (vertical and p.get("show_title", mode == "blur")) else None, mode)
        if not ctx.dry:
            (TMP_DIR / name).write_text(ass, "utf-8")
        graph += f",subtitles={name}:fontsdir=fonts"
        cwd = str(TMP_DIR)
        explain += ("Captions: libass renders an .ass file built from Whisper's word timestamps, 1–3 words "
                    "at a time with the spoken word lit up; the bundled Anton font is loaded from fontsdir. ")
    wm = watermark.vf(ctx)
    if wm:
        graph += f",{wm}"      # above the captions; on vertical it sits in the top band, clear of the app buttons
    graph += f",fps={fps},format=yuv420p[v]"
    stem = f"{person} - {title}" if person else (title or s["title"])
    out = export_path(stem[:90], "vertical" if vertical else "16x9", ".mp4")
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", s["path"], "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            *AAC, "-ar", "48000", "-movflags", "+faststart", out]
    ctx.explain = explain + "Input seeking (-ss before -i) means only these seconds of the VOD get decoded."
    ctx.ffmpeg(args, duration=dur, label="Exporting clip", cwd=cwd)
    if cwd and not ctx.dry:
        for f in Path(cwd).glob("clip_*.ass"):
            try:
                f.unlink()
            except Exception:
                pass
    ctx.outputs.append(out)
    if clip and not ctx.dry:
        from .. import clips
        clips.add_export(clip["id"], out, layout)
