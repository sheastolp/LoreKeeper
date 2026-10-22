"""Chunking: split text into overlapping, sentence-aligned windows.

~900 characters (~220 tokens) is a sweet spot for small local models: big
enough to hold a complete thought, small enough that 8 of them fit in an 8k
context with room left for the answer.
"""
from __future__ import annotations

import re

TARGET = 900
OVERLAP = 150
_SENT = re.compile(r"(?<=[.!?。！？])\s+|\n{2,}")


def split_text(text: str, target: int = TARGET, overlap: int = OVERLAP) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return []
    if len(text) <= target:
        return [text]
    pieces = [p.strip() for p in _SENT.split(text) if p and p.strip()]
    # break up monster "sentences" (tables, code, minified junk)
    units: list[str] = []
    for p in pieces:
        while len(p) > target * 1.5:
            cut = p.rfind(" ", 0, target)
            cut = cut if cut > target // 2 else target
            units.append(p[:cut])
            p = p[cut:].lstrip()
        if p:
            units.append(p)

    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for u in units:
        if size + len(u) > target and cur:
            chunks.append(" ".join(cur))
            # carry the tail sentences forward as overlap
            carry, csize = [], 0
            for prev in reversed(cur):
                if csize + len(prev) > overlap:
                    break
                carry.insert(0, prev)
                csize += len(prev)
            cur, size = carry, csize
        cur.append(u)
        size += len(u) + 1
    if cur:
        tail = " ".join(cur)
        if chunks and len(tail) < target * 0.25:
            chunks[-1] = chunks[-1] + " " + tail
        else:
            chunks.append(tail)
    return chunks


def chunk_pages(pages: list[str], kind: str = "text") -> list[dict]:
    """Chunk per page so citations can say 'p. 12'."""
    out = []
    for i, page in enumerate(pages, 1):
        for piece in split_text(page):
            out.append({"kind": kind, "text": piece, "page": i})
    return out


def chunk_transcript(segments: list[dict], max_chars: int = 800, max_secs: float = 60.0) -> list[dict]:
    """Group Whisper segments into time windows that keep start/end timestamps."""
    out: list[dict] = []
    cur: list[dict] = []
    for seg in segments:
        if cur and (sum(len(s["text"]) for s in cur) + len(seg["text"]) > max_chars
                    or seg["end"] - cur[0]["start"] > max_secs):
            out.append(_window(cur))
            cur = cur[-1:] if len(cur) > 2 else []   # one segment of overlap
        cur.append(seg)
    if cur:
        out.append(_window(cur))
    return out


def _window(segs: list[dict]) -> dict:
    return {
        "kind": "transcript",
        "text": " ".join(s["text"].strip() for s in segs).strip(),
        "start": segs[0]["start"],
        "end": segs[-1]["end"],
    }
