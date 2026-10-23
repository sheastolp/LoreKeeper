"""The small "where to find us" tag in a corner of every finished video.

It's a signpost, not copy protection: small, legible, out of the way. It is one drawtext filter
whose size and position are expressions on the frame's own w/h, so the same string is right for
a 480p GIF, a 1080p cut, a 4K timeline render or a portrait phone clip, with no need to know the
output size up front.

Portrait frames (taller than wide) always get the tag at the top: on TikTok, Shorts and Reels the
bottom fifth is covered by captions, the channel name and the like/share buttons, so a bottom
corner would disappear behind the app.

The text is passed with textfile= (plus expansion=none) rather than text=, so whatever the user
types needs no escaping: no fights with ':' or ',' or '%'. Font and text sit in the temp folder
and FFmpeg runs from there, so the filter only names short relative paths (Windows drive letters
in filter arguments are an escaping minefield).
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from ..config import TMP_DIR, get_settings, resource_root

FONT_FILE = "BarlowSemiCondensed-SemiBold.ttf"
CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
# font height as a share of the frame's short side: 1080p -> 22 / 28 / 37 px
SIZES = {"tiny": 0.020, "small": 0.026, "medium": 0.034}
MIN_PX = 12            # floor so tiny sources and GIFs stay readable
PORTRAIT = "gt(h,w*1.2)"


def config(overrides: dict | None = None) -> dict | None:
    """The active watermark settings, or None when it's switched off (or the text is blank)."""
    s = {**get_settings(), **(overrides or {})}
    text = " ".join(str(s.get("watermark_text") or "").split())
    if not s.get("watermark") or not text:
        return None
    corner = s.get("watermark_corner") if s.get("watermark_corner") in CORNERS else "bottom-right"
    size = s.get("watermark_size") if s.get("watermark_size") in SIZES else "small"
    return {"text": text[:80], "corner": corner, "size": size}


def _assets(text: str, folder: Path) -> str:
    """Put the font and the text file where FFmpeg will run; returns the text file's name."""
    fonts = folder / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    src = resource_root() / "assets" / "fonts" / FONT_FILE
    dst = fonts / FONT_FILE
    if src.exists() and (not dst.exists() or dst.stat().st_size != src.stat().st_size):
        shutil.copyfile(src, dst)
    name = f"wm_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]}.txt"
    p = folder / name
    if not p.exists() or p.read_text("utf-8") != text:
        p.write_text(text, "utf-8")      # no trailing newline: drawtext would draw it as a second line
    return name


def drawtext(textfile: str, corner: str, size: str) -> str:
    """The filter itself (no pad labels), ready to chain with commas."""
    k = SIZES.get(size, SIZES["small"])
    m = "min(w,h)*0.03"                              # landscape margin: 32 px at 1080p
    side = f"if({PORTRAIT},w*0.05,{m})"
    left, top = corner.endswith("left"), corner.startswith("top")
    x = side if left else f"w-tw-{side}"
    # portrait: always the top band, just under the phone's status bar and the app's top tabs
    y = f"if({PORTRAIT},h*0.075,{m})" if top else f"if({PORTRAIT},h*0.075,h-th-{m})"
    return ("drawtext="
            f"fontfile=fonts/{FONT_FILE}:textfile={textfile}:expansion=none:"
            f"fontsize='max({MIN_PX},min(w,h)*{k})':fontcolor=white@0.92:"
            "borderw=1:bordercolor=black@0.45:shadowx=1:shadowy=2:shadowcolor=black@0.55:"
            f"x='{x}':y='{y}'")


def vf(ctx=None, overrides: dict | None = None, folder: Path | None = None) -> str | None:
    """drawtext for the current settings, or None when off. Marks ctx so the explanation says so."""
    cfg = config(overrides)
    if not cfg:
        return None
    name = _assets(cfg["text"], folder or TMP_DIR)
    if ctx is not None:
        ctx.watermark = cfg
    return drawtext(name, cfg["corner"], cfg["size"])


def active() -> bool:
    return config() is not None


def note(cfg: dict) -> str:
    where = cfg["corner"].replace("-", " ")
    return (f"Watermark: '{cfg['text']}' is drawn small in the {where} corner by FFmpeg's drawtext filter "
            "(top corner on portrait video, clear of the TikTok/Shorts buttons). Turn it off or move it in Settings.")


# ---------------------------------------------------------------- settings preview

_cache: dict[tuple, bytes] = {}


def preview(layout: str, overrides: dict, background: Path | None = None) -> bytes:
    """A JPEG of one frame with the real drawtext applied, so Settings shows exactly what renders produce.

    Drawn at 960x540 / 540x960: the filter sizes itself from the frame, so this is a faithful
    scaled-down copy of a 1080p render or a 1080x1920 vertical clip."""
    import io
    import subprocess

    from PIL import Image

    from ..config import ffmpeg_bin, popen_kwargs

    W, H = (540, 960) if layout == "vertical" else (960, 540)
    cfg = config({**overrides, "watermark": True})
    key = (layout, str(background), cfg and tuple(sorted(cfg.items())))
    if key in _cache:
        return _cache[key]
    wm = vf(overrides={**overrides, "watermark": True})
    fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    chain = fit + (f",{wm}" if wm else "")
    if background and background.exists():
        inputs = [["-i", str(background)]]
    else:
        inputs = [["-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0=0x4a3624:c1=0x1b3448:x0=0:y0=0:x1={W}:y1={H}:d=1:seed=7"],
                  ["-f", "lavfi", "-i", f"color=c=0x2a2c34:s={W}x{H}:d=1"]]   # very old FFmpeg: no gradients source
    err = b""
    for inp in inputs:
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", *inp, "-vf", chain,
                            "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"],
                           capture_output=True, cwd=str(TMP_DIR), **popen_kwargs())
        if r.returncode == 0 and r.stdout:
            buf = io.BytesIO()
            Image.open(io.BytesIO(r.stdout)).convert("RGB").save(buf, "JPEG", quality=88)
            if len(_cache) > 24:
                _cache.clear()
            _cache[key] = buf.getvalue()
            return _cache[key]
        err = r.stderr
    raise RuntimeError(f"Preview failed: {err.decode(errors='replace')[-300:]}")
