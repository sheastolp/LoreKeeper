"""Hand corrections to a Whisper transcript.

Editing a line rewrites the segment text *and* its word timings, because the words drive burned-in
captions, subtitle exports and Studio's transcript cuts. Words that survive the edit keep Whisper's
timestamps; new or changed words share out the time of what they replaced. The search chunks and
clip transcripts that quote the line are updated too, and changed chunks are re-embedded in the background.
"""
from __future__ import annotations

import difflib
import re
import threading

from .db import db


class TranscriptError(Exception):
    pass


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def _merge(words: list[dict]) -> list[dict]:
    """Whisper splits some words into sub-tokens; a new word starts with a leading space."""
    out: list[dict] = []
    for w in words:
        if out and w["w"] and not w["w"][:1].isspace():
            out[-1] = {**out[-1], "w": out[-1]["w"] + w["w"], "e": w["e"]}
        else:
            out.append(dict(w))
    return out


def _spread(tokens: list[str], a: float, b: float) -> list[dict]:
    """Share [a, b] between tokens by length (at least 60 ms each)."""
    b = max(b, a + 0.06 * len(tokens))
    total = sum(len(t) for t in tokens) or 1
    out, t = [], a
    for tok in tokens:
        d = (b - a) * len(tok) / total
        out.append({"w": " " + tok, "s": round(t, 3), "e": round(t + d, 3)})
        t += d
    return out


def realign(old: list[dict], text: str, seg_start: float, seg_end: float) -> list[dict]:
    old = _merge(old)
    new = text.split()
    sm = difflib.SequenceMatcher(a=[_norm(w["w"]) for w in old], b=[_norm(t) for t in new], autojunk=False)
    out: list[dict] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                out.append({**old[i], "w": " " + new[j]})
        elif op in ("replace", "insert") and j2 > j1:
            if op == "replace":
                a, b = old[i1]["s"], old[i2 - 1]["e"]
            else:
                a = out[-1]["e"] if out else seg_start
                b = old[i1]["s"] if i1 < len(old) else seg_end
            out.extend(_spread(new[j1:j2], a, b))
    return out


def edit(sid: str, edits: list[dict]) -> dict:
    """edits: [{i, start, text}] — `start` guards against editing a stale copy of the transcript."""
    tr = db.transcript(sid)
    if not tr:
        raise TranscriptError("No transcript to edit")
    segs, words = tr["segments"], tr["words"]
    changed: list[tuple[str, str]] = []
    for ed in edits:
        i = int(ed["i"])
        if not 0 <= i < len(segs) or abs(segs[i]["start"] - float(ed.get("start", segs[i]["start"]))) > 0.01:
            raise TranscriptError("The transcript changed since you opened it. Reload and try again.")
        text = re.sub(r"\s+", " ", str(ed.get("text", ""))).strip()
        seg = segs[i]
        if text == seg["text"].strip():
            continue
        # this segment's words: those whose middle falls inside it (neighbours can overlap by a few ms)
        idx = [k for k, w in enumerate(words) if seg["start"] - 0.05 <= (w["s"] + w["e"]) / 2 <= seg["end"] + 0.05]
        lo, hi = (idx[0], idx[-1] + 1) if idx else (_insert_at(words, seg["start"]),) * 2
        words[lo:hi] = realign(words[lo:hi], text, seg["start"], seg["end"])
        changed.append((seg["text"].strip(), text))
        seg["text"] = text
        seg["edited"] = True
    if not changed:
        return tr
    db.save_transcript(sid, tr["language"], segs, words)
    _update_chunks(sid, segs)
    _update_clips(sid, changed)
    return tr


def _insert_at(words: list[dict], t: float) -> int:
    return next((k for k, w in enumerate(words) if w["s"] >= t), len(words))


def _update_chunks(sid: str, segs: list[dict]) -> None:
    """Transcript chunks are whole segments between their start and end; rebuild the ones whose text moved."""
    stale = []
    for r in db.q("SELECT id, text, start, end FROM chunks WHERE source_id=? AND kind='transcript'", (sid,)):
        inside = [s["text"].strip() for s in segs if s["start"] >= r["start"] - 0.01 and s["end"] <= r["end"] + 0.01]
        text = " ".join(inside).strip()
        if text and text != r["text"]:
            db.run("UPDATE chunks SET text=?, embedding=NULL WHERE id=?", (text, r["id"]))
            stale.append((r["id"], text))
    if stale:
        db.version += 1
        threading.Thread(target=_reembed, args=(sid, stale), daemon=True, name="reembed-edit").start()


def _reembed(sid: str, rows: list[tuple[int, str]]) -> None:
    try:
        from . import ai
        src = db.source(sid) or {}
        title = (src.get("meta") or {}).get("doc_title") or src.get("title")
        vecs = ai.embed([t for _, t in rows], titles=[title] * len(rows))
        db.set_embeddings([i for i, _ in rows], vecs)
    except Exception:
        pass        # keyword search already sees the new text; Settings → AI → rebuild fixes the vectors later


def _update_clips(sid: str, changed: list[tuple[str, str]]) -> None:
    """Clip transcripts are snapshots ('3 [01:02] Dave: text  (LOUD)'); swap the corrected lines in."""
    for r in db.q("SELECT id, transcript FROM clips WHERE source_id=? AND transcript IS NOT NULL", (sid,)):
        lines = r["transcript"].split("\n")
        hit = False
        for k, line in enumerate(lines):
            for old, new in changed:
                if f": {old}" in line:
                    lines[k] = line.replace(f": {old}", f": {new}", 1)
                    hit = True
                    break
        if hit:
            db.run("UPDATE clips SET transcript=? WHERE id=?", ("\n".join(lines), r["id"]))
