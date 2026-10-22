"""Image ingestion: EXIF facts + a vision-model description with OCR."""
from __future__ import annotations

import io
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}

IMAGE_PROMPT = (
    "Describe this image for a searchable knowledge base.\n"
    "1. What it is (photo, screenshot, diagram, chart, meme, map, document scan…).\n"
    "2. Key subjects, objects, people/characters, setting and notable details.\n"
    "3. If it's a chart/diagram: what it shows, axes, trends, and the takeaway.\n"
    "4. Transcribe ALL visible text exactly, prefixed with 'Text:'.\n"
    "Be concrete and factual. No preamble."
)


def load_image(path: Path):
    from PIL import Image, ImageOps
    if path.suffix.lower() == ".heic":
        try:
            import pillow_heif  # optional
            pillow_heif.register_heif_opener()
        except Exception:
            raise RuntimeError("HEIC needs the optional 'pillow-heif' package; convert to JPG instead.")
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im


def exif_summary(im) -> dict:
    out = {"width": im.width, "height": im.height}
    try:
        from PIL import ExifTags
        exif = im.getexif()
        tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
        for key in ("Make", "Model", "DateTime", "Software"):
            if tags.get(key):
                out[key.lower()] = str(tags[key]).strip("\x00 ")
    except Exception:
        pass
    return out


def to_jpeg(im, max_side: int = 1344, quality: int = 88) -> bytes:
    im = im.copy()
    if im.mode not in ("RGB", "L"):
        from PIL import Image
        bg = Image.new("RGB", im.size, (255, 255, 255))
        if im.mode in ("RGBA", "LA") or "transparency" in im.info:
            im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()
