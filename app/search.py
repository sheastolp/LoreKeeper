"""Hybrid retrieval: BM25 (SQLite FTS5) + dense vectors, fused with Reciprocal Rank Fusion.

Why both? Keyword search nails exact names, numbers and jargon ("RTX 4070",
"Fireball") that embeddings blur together; embeddings catch paraphrases
("how do I cast it" ≈ "spellcasting procedure") that keywords miss. RRF merges
the two ranked lists using only rank positions — score = Σ 1/(k + rank) — so
nobody has to calibrate BM25 scores against cosine similarities.
"""
from __future__ import annotations

import re
import threading

import numpy as np

from . import ollama
from .db import db

RRF_K = 60
_STOP = set("""a an and are as at be but by can do does for from had has have how i if in into is it its
me my of on or our so than that the their them then there these they this to was we were what when where
which who why will with would you your about tell explain show give""".split())

_cache_lock = threading.Lock()
_cache: dict = {"version": -1, "ids": None, "mat": None, "src": None}


def _vectors():
    with _cache_lock:
        if _cache["version"] != db.version:
            rows = db.q("SELECT id, source_id, embedding FROM chunks WHERE embedding IS NOT NULL")
            if rows:
                dims = {len(r["embedding"]) for r in rows}
                dim = max(dims, key=lambda d: sum(1 for r in rows if len(r["embedding"]) == d))
                rows = [r for r in rows if len(r["embedding"]) == dim]   # ignore stale model vectors
                _cache["ids"] = np.array([r["id"] for r in rows])
                _cache["src"] = np.array([r["source_id"] for r in rows])
                _cache["mat"] = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
            else:
                _cache["ids"] = _cache["mat"] = _cache["src"] = None
            _cache["version"] = db.version
        return _cache["ids"], _cache["mat"], _cache["src"]


def fts_query(text: str) -> str | None:
    terms = [t for t in re.findall(r"[\w']+", text.lower()) if t not in _STOP and len(t) > 1]
    if not terms:
        terms = re.findall(r"[\w']+", text.lower())
    if not terms:
        return None
    seen, out = set(), []
    for t in terms:
        t = t.replace("'", "")
        if t and t not in seen:
            seen.add(t)
            out.append(f'"{t}"*' if len(t) > 3 else f'"{t}"')
    return " OR ".join(out[:24])


def search(query: str, k: int = 8, source_ids: list[str] | None = None,
           per_source: int = 4, pool: int = 50) -> dict:
    scope = set(source_ids) if source_ids else None
    telemetry: dict = {"query": query, "rrf_k": RRF_K, "dense": False, "sparse": False}

    # ---- sparse (BM25)
    sparse_rank: dict[int, int] = {}
    fq = fts_query(query)
    if fq:
        try:
            rows = db.q(
                "SELECT c.id, c.source_id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid "
                "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?", (fq, pool * 3))
            rank = 0
            for r in rows:
                if scope and r["source_id"] not in scope:
                    continue
                rank += 1
                sparse_rank[r["id"]] = rank
                if rank >= pool:
                    break
            telemetry["sparse"] = True
        except Exception as exc:
            telemetry["sparse_error"] = str(exc)
    telemetry["fts_query"] = fq

    # ---- dense (cosine on normalised vectors = dot product)
    dense_rank: dict[int, int] = {}
    dense_sim: dict[int, float] = {}
    ids, mat, src = _vectors()
    if ids is not None and len(ids):
        try:
            qv = ollama.embed([query], is_query=True)[0]
            if qv.shape[0] == mat.shape[1]:
                sims = mat @ qv
                if scope:
                    mask = np.isin(src, list(scope))
                    sims = np.where(mask, sims, -np.inf)
                top = np.argsort(-sims)[:pool]
                r = 0
                for idx in top:
                    if not np.isfinite(sims[idx]):
                        break
                    r += 1
                    dense_rank[int(ids[idx])] = r
                    dense_sim[int(ids[idx])] = float(sims[idx])
                telemetry["dense"] = True
                telemetry["dim"] = int(mat.shape[1])
            else:
                telemetry["dense_error"] = "Embedding model changed — re-index the library"
        except Exception as exc:
            telemetry["dense_error"] = str(exc)
    telemetry["vectors"] = 0 if ids is None else int(len(ids))

    # ---- reciprocal rank fusion
    fused: dict[int, float] = {}
    for cid, r in sparse_rank.items():
        fused[cid] = fused.get(cid, 0) + 1 / (RRF_K + r)
    for cid, r in dense_rank.items():
        fused[cid] = fused.get(cid, 0) + 1 / (RRF_K + r)
    ordered = sorted(fused, key=fused.get, reverse=True)

    if not ordered:
        return {"hits": [], "telemetry": telemetry}

    placeholders = ",".join("?" * min(len(ordered), 200))
    rows = {r["id"]: dict(r) for r in db.q(
        f"SELECT c.id, c.source_id, c.kind, c.text, c.start, c.end, c.page, s.title, s.kind AS source_kind "
        f"FROM chunks c JOIN sources s ON s.id=c.source_id WHERE c.id IN ({placeholders})",
        ordered[:200])}

    hits, per = [], {}
    for cid in ordered:
        row = rows.get(cid)
        if not row:
            continue
        if per.get(row["source_id"], 0) >= per_source:   # diversity: don't let one file hog the context
            continue
        per[row["source_id"]] = per.get(row["source_id"], 0) + 1
        row.update({
            "rrf": round(fused[cid], 5),
            "bm25_rank": sparse_rank.get(cid),
            "dense_rank": dense_rank.get(cid),
            "cosine": round(dense_sim[cid], 4) if cid in dense_sim else None,
        })
        hits.append(row)
        if len(hits) >= k:
            break
    telemetry["candidates"] = len(fused)
    return {"hits": hits, "telemetry": telemetry}
