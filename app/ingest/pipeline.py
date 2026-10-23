"""Turns any added item into searchable chunks.

document → text per page → chunks (scanned pages OCR'd by the vision model)
url      → fetched; HTML is cleaned, PDFs/media/images are downloaded and re-routed
image    → vision description + visible text
audio    → Whisper transcript (word timestamps) → time-windowed chunks
video    → audio path + keyframe captions from the vision model
then     → every chunk embedded for semantic search (FTS5 keyword index is automatic)
"""
from __future__ import annotations

import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .. import ai, ff
from ..config import LIBRARY_DIR, get_settings
from ..db import db
from ..jobs import Job, jobs
from . import chunker, documents, images, media

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif", ".amr"}


def kind_for(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in images.IMAGE_EXT:
        return "image"
    if ext in documents.DOC_EXT:
        return "document"
    return None


# ------------------------------------------------------------------ entry points

def add_path(path: str, owned: bool = False) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    kind = kind_for(p)
    if not kind:
        raise ValueError(f"Unsupported file type: {p.suffix or p.name}")
    if kind == "video":
        # .webm / .ogg can be audio-only; probe to be sure
        try:
            if not ff.probe(p).get("has_video"):
                kind = "audio"
        except Exception:
            pass
    sid = db.add_source(kind, p.stem if kind != "document" else p.name, path=str(p), owned=owned, size=p.stat().st_size)
    queue(sid)
    return sid


def add_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    host = urlparse(url).netloc or url
    sid = db.add_source("url", host, url=url)
    queue(sid)
    return sid


def queue(sid: str) -> Job:
    src = db.source(sid)
    db.update_source(sid, status="queued", progress=0, stage="Waiting in line", error=None)

    def fail(job: Job, exc: BaseException):
        db.update_source(sid, status="error", error=str(exc), stage=None)

    return jobs.submit("ingest", f"Index {src['title'] if src else sid}", lambda j: ingest(j, sid),
                       lane="ingest", on_error=fail)


# ------------------------------------------------------------------ main pipeline

class _Progress:
    """Mirrors job progress onto the source row so the library cards update live."""

    def __init__(self, job: Job, sid: str):
        self.job, self.sid, self.last = job, sid, 0.0
        self._orig = job.update

        def update(progress=None, message=None):
            self._orig(progress, message)
            now = time.time()
            if now - self.last > 0.4 or (progress is not None and progress >= 1):
                self.last = now
                db.update_source(sid, progress=job.progress, stage=job.message)
        job.update = update  # type: ignore[method-assign]


def ingest(job: Job, sid: str) -> dict:
    _Progress(job, sid)
    src = db.source(sid)
    if not src:
        raise RuntimeError("Source vanished")
    db.update_source(sid, status="processing", error=None)
    warnings: list[str] = []

    if src["kind"] == "url":
        src = _fetch_url(job, sid, src, warnings)
        if src["kind"] == "url":            # it was a web page: chunks already built
            chunks = src.pop("_chunks")
            return _finish(job, sid, chunks, warnings)

    path = src["path"]
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"File is missing: {path}")
    kind = src["kind"]
    if kind == "document":
        chunks = _ingest_document(job, sid, Path(path), warnings)
    elif kind == "image":
        chunks = _ingest_image(job, sid, Path(path), warnings)
    elif kind in ("audio", "video"):
        chunks = _ingest_media(job, sid, path, kind, warnings)
    else:
        raise ValueError(f"Unknown kind {kind}")
    return _finish(job, sid, chunks, warnings)


def _finish(job: Job, sid: str, chunks: list[dict], warnings: list[str]) -> dict:
    chunks = [c for c in chunks if c.get("text", "").strip()]
    if not chunks:
        warnings.append("No text could be extracted, so there is nothing to search in this item.")
    db.replace_chunks(sid, chunks)
    embed_source(job, sid, warnings, p0=0.92)
    db.update_source(sid, status="ready", progress=1, stage=None, warnings=warnings)
    return {"chunks": len(chunks), "warnings": warnings}


def embed_source(job: Job, sid: str, warnings: list[str], p0: float = 0.0) -> None:
    rows = db.chunks_for(sid)
    if not rows:
        return
    job.update(p0, f"Embedding {len(rows)} chunks")
    src = db.source(sid) or {}
    title = (src.get("meta") or {}).get("doc_title") or src.get("title")
    try:
        vecs = ai.embed([r["text"] for r in rows], titles=[title] * len(rows))
        db.set_embeddings([r["id"] for r in rows], vecs)
        space = ai.embed_model_id()
        db.merge_meta(sid, {"embed_model": space.split(":", 1)[1], "embed_space": space, "embed_dim": int(vecs.shape[1])})
    except Exception as exc:
        warnings.append(f"Semantic index skipped ({exc}). Keyword search still works; "
                        f"rebuild the semantic index from Settings → AI once the search model is available.")


# ------------------------------------------------------------------ kinds

def _ingest_document(job: Job, sid: str, path: Path, warnings: list[str]) -> list[dict]:
    job.update(0.05, "Extracting text")
    pages, meta = documents.extract_file(path)
    if meta:
        db.merge_meta(sid, meta)
    chunks: list[dict] = []
    is_pdf = path.suffix.lower() == ".pdf"
    paged = is_pdf or path.suffix.lower() == ".pptx"
    blank = [i for i, p in enumerate(pages) if len(p.strip()) < 25] if is_pdf else []
    if blank and get_settings()["ocr_scanned_pdfs"]:
        limit = 60
        if len(blank) > limit:
            warnings.append(f"{len(blank)} pages have no text layer; OCR'd the first {limit}.")
        for n, idx in enumerate(blank[:limit]):
            job.update(0.1 + 0.75 * n / max(1, min(len(blank), limit)), f"OCR page {idx + 1} with vision model")
            jpg = documents.render_pdf_page(path, idx)
            if not jpg:
                warnings.append("Scanned pages found but PDF rendering is unavailable.")
                break
            try:
                pages[idx] = ai.describe_image(
                    jpg, "Transcribe all text on this document page exactly, preserving reading order. "
                         "Describe any figures briefly in [brackets]. Output only the transcription.")
            except Exception as exc:
                warnings.append(f"OCR skipped: {exc}")
                break
    if paged:
        chunks = chunker.chunk_pages(pages)
    else:
        chunks = [{"kind": "text", "text": t} for t in chunker.split_text("\n\n".join(pages))]
    job.update(0.9, f"{len(chunks)} chunks")
    return chunks


def _ingest_image(job: Job, sid: str, path: Path, warnings: list[str]) -> list[dict]:
    job.update(0.1, "Reading image")
    im = images.load_image(path)
    facts = images.exif_summary(im)
    db.merge_meta(sid, facts)
    thumb = media.derived(sid) / "thumb.jpg"
    thumb.write_bytes(images.to_jpeg(im, max_side=480, quality=82))
    job.update(0.3, "Vision model is looking at it")
    fact_line = f"Image file '{path.name}', {facts['width']}x{facts['height']}px"
    if facts.get("model"):
        fact_line += f", shot on {facts.get('make', '')} {facts['model']}".replace("  ", " ")
    if facts.get("datetime"):
        fact_line += f", taken {facts['datetime']}"
    try:
        desc = ai.describe_image(images.to_jpeg(im), images.IMAGE_PROMPT)
    except Exception as exc:
        warnings.append(f"Image description skipped: {exc}")
        desc = ""
    db.merge_meta(sid, {"description": desc})
    text = fact_line + ".\n" + desc
    return [{"kind": "image", "text": t} for t in chunker.split_text(text)]


def _ingest_media(job: Job, sid: str, path: str, kind: str, warnings: list[str]) -> list[dict]:
    job.update(0.01, "Probing streams")
    info = ff.probe(path)
    if kind == "video" and not info.get("has_video"):
        kind = "audio"
        db.update_source(sid, kind="audio")
    db.update_source(sid, duration=info.get("duration"))
    db.merge_meta(sid, info)
    dur = info.get("duration") or 0

    job.update(0.03, "Making previews")
    if info.get("has_video"):
        media.make_thumbnail(sid, path, info)
        media.make_filmstrip(sid, path, info)
    if info.get("has_audio"):
        media.make_waveform(sid, path)

    proxy = media.make_proxy(sid, path, info, job, 0.05, 0.25)
    db.merge_meta(sid, {"proxy": proxy})

    chunks: list[dict] = []
    if info.get("has_audio"):
        t_end = 0.8 if (kind == "video" and get_settings()["caption_frames"] and int(get_settings()["max_keyframes"]) > 0) else 0.9
        tr = media.transcribe(path, dur, job, 0.26, t_end)
        db.save_transcript(sid, tr["language"], tr["segments"], tr["words"])
        db.merge_meta(sid, {"language": tr["language"], "words": len(tr["words"])})
        chunks += chunker.chunk_transcript(tr["segments"])
        if not tr["segments"]:
            warnings.append("No speech detected.")
    if kind == "video" and get_settings()["caption_frames"] and int(get_settings()["max_keyframes"]) > 0 and dur:
        chunks += media.caption_keyframes(sid, path, dur, job, 0.8, 0.92, warnings)
    chunks.sort(key=lambda c: (c.get("start") or 0))
    return chunks


# ------------------------------------------------------------------ urls

MEDIA_TYPES = ("video/", "audio/", "image/", "application/pdf")


def _fetch_url(job: Job, sid: str, src: dict, warnings: list[str]) -> dict:
    url = src["url"]
    job.update(0.05, "Fetching page")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Lorekeeper/1.0"}
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20, read=120), headers=headers) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype.startswith(MEDIA_TYPES) or _media_ext(str(r.url)):
                return _download(job, sid, r, ctype)
            html = r.read().decode(r.encoding or "utf-8", errors="replace")
    job.update(0.4, "Extracting main content")
    if ctype.startswith("text/plain") or ctype.endswith(("markdown", "json")):
        text, title = html, None
    else:
        text, title = documents.html_to_text(html, url)
    title = (title or urlparse(url).netloc).strip()[:160]
    db.update_source(sid, title=title)
    db.merge_meta(sid, {"chars": len(text)})
    chunks = [{"kind": "text", "text": t} for t in chunker.split_text(text)]
    src = db.source(sid)
    src["_chunks"] = chunks
    return src


def _media_ext(url: str) -> bool:
    ext = Path(urlparse(url).path).suffix.lower()
    return ext in VIDEO_EXT | AUDIO_EXT | images.IMAGE_EXT | {".pdf", ".docx", ".pptx"}


def _download(job: Job, sid: str, r: httpx.Response, ctype: str) -> dict:
    name = Path(urlparse(str(r.url)).path).name or "download"
    if not Path(name).suffix:
        name += mimetypes.guess_extension(ctype) or ".bin"
    dest = LIBRARY_DIR / f"{sid}_{_safe(name)}"
    total = int(r.headers.get("content-length") or 0)
    got = 0
    with open(dest, "wb") as fh:
        for chunk in r.iter_bytes(1 << 20):
            fh.write(chunk)
            got += len(chunk)
            if total:
                job.update(0.05 + 0.2 * got / total, f"Downloading {got >> 20} / {total >> 20} MB")
    kind = kind_for(dest) or "document"
    db.update_source(sid, kind=kind, path=str(dest), owned=1, size=got, title=Path(name).stem)
    return db.source(sid)


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:120]


def upload_dest(filename: str) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S")
    dest = LIBRARY_DIR / f"{stamp}_{_safe(filename)}"
    n = 1
    while dest.exists():
        dest = LIBRARY_DIR / f"{stamp}_{n}_{_safe(filename)}"
        n += 1
    return dest


def register_upload(dest: Path, filename: str) -> str:
    """Index a drag-and-dropped file that was streamed straight into the library folder."""
    try:
        sid = add_path(str(dest), owned=True)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    db.update_source(sid, title=Path(filename).stem if kind_for(dest) != "document" else filename)
    return sid
