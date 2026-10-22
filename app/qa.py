"""Grounded Q&A: retrieve → build a cited context → stream a geeky, structured answer."""
from __future__ import annotations

import time
from typing import Iterator

from . import ollama
from .config import get_settings
from .ingest.media import fmt_ts
from .search import search

SYSTEM = """You are Lorekeeper, a sharp, enthusiastic, deeply nerdy analyst. You answer questions using ONLY the numbered sources the user's library provides.

Write your answer in Markdown with exactly these four sections, in this order:

## The Answer
Two to four sentences that directly answer the question. Lead with the conclusion.

## The How
The mechanism, process or steps — how it works or how it happened. Be concrete: numbers, names, sequences. Use a short list when there are steps.

## The Why
The reasons behind it — causes, motivations, trade-offs, design rationale. Explain why it works this way and why it matters.

## Deep Cut
One genuinely insightful, geeky observation: a non-obvious connection between sources, an implication, an edge case, or a fun nerdy parallel. Keep it to two or three sentences.

Rules:
- Cite every factual claim with its source tag like [S2] or [S1][S4], placed right after the claim.
- If the sources don't contain the answer, say so plainly in The Answer. You may then add clearly labelled general knowledge, marked "(general knowledge, not from your library)".
- Never invent quotes, timestamps, page numbers or sources.
- When a source is a video or audio transcript, mention the timestamp when it helps.
- Be vivid and fun, but accuracy beats flair."""


def _label(hit: dict) -> str:
    kind = hit["source_kind"]
    where = ""
    if hit.get("start") is not None and kind in ("video", "audio"):
        where = f", {fmt_ts(hit['start'])}"
        if hit.get("end") and hit["end"] - hit["start"] > 1:
            where += f"–{fmt_ts(hit['end'])}"
        if hit["kind"] == "frame":
            where += ", on-screen visuals"
    elif hit.get("page"):
        where = f", p. {hit['page']}"
    return f'"{hit["title"]}" ({kind}{where})'


def build_context(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(f"[S{i}] {_label(h)}\n{h['text'].strip()}")
    return "\n\n---\n\n".join(blocks)


def retrieval_query(question: str, history: list[dict]) -> str:
    """Query rewriting, the cheap way: short follow-ups ('why?', 'and the second one?')
    borrow the previous question so retrieval still knows the topic."""
    prev = next((h["content"] for h in reversed(history or []) if h.get("role") == "user"), "")
    if prev and (len(question.split()) < 7 or any(w in question.lower().split() for w in
                                                    ("it", "that", "this", "they", "those", "he", "she", "them"))):
        return f"{prev} {question}"
    return question


def answer_stream(question: str, history: list[dict] | None = None,
                  source_ids: list[str] | None = None) -> Iterator[dict]:
    s = get_settings()
    t0 = time.time()
    rq = retrieval_query(question, history or [])
    found = search(rq, k=int(s["top_k"]), source_ids=source_ids)
    hits = found["hits"]
    tel = found["telemetry"]
    tel["retrieval_query"] = rq
    tel["retrieval_ms"] = round((time.time() - t0) * 1000)

    yield {"type": "sources", "hits": [_public(h, i) for i, h in enumerate(hits, 1)], "telemetry": tel}

    if not hits:
        yield {"type": "token", "text": (
            "## The Answer\nI couldn't find anything about that in your library yet. "
            "Add files or links that cover it (or widen the scope), then ask again.\n\n"
            "## The How\nEvery question runs two searches over your indexed chunks: a BM25 keyword "
            "search and a vector similarity search. Both came back empty here.\n\n"
            "## The Why\nLorekeeper only answers from what you've given it, so it can't make things up.\n\n"
            "## Deep Cut\nIf you just added something, it may still be indexing — check the Library tab.")}
        yield {"type": "done", "stats": None}
        return

    messages = [{"role": "system", "content": SYSTEM}]
    for h in (history or [])[-6:]:
        content = h.get("content", "")
        if h.get("role") == "assistant":
            content = content[:1500]
        messages.append({"role": h["role"], "content": content})
    messages.append({"role": "user", "content": (
        f"SOURCES FROM MY LIBRARY:\n\n{build_context(hits)}\n\n"
        f"QUESTION: {question}\n\n"
        "Answer with the four sections (The Answer, The How, The Why, Deep Cut) and cite sources as [S#].")})

    first = None
    for ev in ollama.chat_stream(messages):
        if ev["type"] == "token" and first is None:
            first = time.time()
            ev["ttft_ms"] = round((first - t0) * 1000)
        if ev["type"] == "stats":
            ev["ttft_ms"] = round(((first or time.time()) - t0) * 1000)
            yield {"type": "done", "stats": ev}
            return
        yield ev
    yield {"type": "done", "stats": None}


def _public(h: dict, i: int) -> dict:
    return {
        "tag": f"S{i}", "chunk_id": h["id"], "source_id": h["source_id"], "title": h["title"],
        "source_kind": h["source_kind"], "kind": h["kind"], "start": h.get("start"), "end": h.get("end"),
        "page": h.get("page"), "text": h["text"][:1200], "rrf": h["rrf"], "bm25_rank": h["bm25_rank"],
        "dense_rank": h["dense_rank"], "cosine": h["cosine"], "label": _label(h),
    }
