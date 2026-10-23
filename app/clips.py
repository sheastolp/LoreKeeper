"""Clip finder: a person's best moments across VODs.

1. Where are they?    voiceprints → per-window match scores → the moments they're talking
2. What's worth it?   slide clip-sized windows over each VOD and score the ones they're in:
                        their share of the talking, loudness spikes (hype), speech pace, and
                        excitement in the transcript (!, ?, laughs)
3. Is it a clip?      the best candidates go to the local model with a speaker-labelled transcript;
                        it scores 1–10, writes a title and picks the tightest setup→payoff span
4. Review             clips land in the review grid; you export each as 16:9 or vertical
"""
from __future__ import annotations

import json
import re
import time
import uuid

import numpy as np

from . import ai, voice
from .db import db
from .ingest.media import fmt_ts
from .jobs import Job

LENGTHS = {"short": (12, 20, 30), "medium": (25, 40, 60), "long": (50, 75, 120)}   # min, target, max seconds
LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao|lmfao|rofl|hehe+|laugh\w*|bruh|omg|wtf|no way|let's go+|holy)\b", re.I)

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "title": {"type": "string", "maxLength": 70},
        "why": {"type": "string", "maxLength": 160},
        "start_line": {"type": "integer", "minimum": 0},
        "end_line": {"type": "integer", "minimum": 0},
    },
    "required": ["score", "title", "why", "start_line", "end_line"],
}

SYSTEM = """You are a sharp short-form video editor who cuts Twitch streams into viral clips (TikTok, YouTube Shorts, Twitch clips).
You'll get a numbered, speaker-labelled transcript of one moment from a stream. Judge it as a clip that features {name}.

Great clips: funny lines and comebacks, big reactions, hype or clutch moments, rage, wholesome or surprising moments,
a story with a payoff, a quotable one-liner. LOUD marks shouting or big reactions.
Weak clips: logistics, menus and settings, reading chat, silence, mumbling, half a thought with no payoff,
or moments where {name} barely speaks.

Reply with JSON:
- score: 1-10 (7+ only if you'd actually post it)
- title: a punchy Shorts-style title, under 60 characters, no hashtags, no quotes, no emoji
- why: one short sentence on what makes it work (or not)
- start_line / end_line: the tightest span of line numbers that still has the setup and the payoff"""


# ------------------------------------------------------------------ helpers

def _lines_for(tr: dict, a: float, b: float) -> list[dict]:
    return [s for s in tr["segments"] if s["end"] > a and s["start"] < b]


def _speaker_of(seg: dict, idx: dict, hit: np.ndarray) -> bool:
    """Is this transcript segment mostly the person?"""
    mid = (idx["starts"] + idx["ends"]) / 2
    sel = np.where((mid >= seg["start"]) & (mid <= seg["end"]))[0]
    if not len(sel):
        return False
    return float(hit[sel].mean()) >= 0.5


def _robust_z(x: np.ndarray) -> np.ndarray:
    if not len(x):
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826 or 1.0
    return (x - med) / mad


def candidates(idx: dict, pres: dict, tr: dict, duration: float, length: str, k: int) -> list[dict]:
    lo, target, hi = LENGTHS.get(length, LENGTHS["medium"])
    starts, ends, hit = idx["starts"], idx["ends"], pres["hit"]
    if not hit.any():
        return []
    mid = (starts + ends) / 2
    loud_z = _robust_z(idx["loud"])
    words = tr["words"]
    w_t = np.array([w["s"] for w in words]) if words else np.zeros(0)
    vod_wps = len(words) / max(1.0, len(starts) * voice.HOP) if words else 1.0
    out = []
    stride = max(4.0, target / 3)
    t = 0.0
    while t < duration - lo:
        a, b = t, min(duration, t + target)
        sel = np.where((mid >= a) & (mid < b))[0]
        t += stride
        if len(sel) < 3:
            continue
        mine = sel[hit[sel]]
        share = len(mine) / len(sel)
        if share < 0.2 or len(mine) < 2:
            continue
        z = np.sort(loud_z[sel])[-3:]
        hype = float(np.clip(z.mean() / 4, 0, 1))
        energy = float((loud_z[sel] > 1.5).mean())
        wsel = np.where((w_t >= a) & (w_t < b))[0] if len(w_t) else []
        text = "".join(words[i]["w"] for i in wsel)
        speech_secs = max(1.0, len(sel) * voice.HOP)
        density = (len(wsel) / speech_secs) / max(0.5, vod_wps)
        excite = text.count("!") + text.count("?") * 0.5 + 2 * len(LAUGH.findall(text))
        excite_n = float(np.clip(excite / (3 * (b - a) / 30), 0, 1))
        heur = (0.35 * min(1.0, share / 0.5) + 0.25 * hype + 0.15 * energy
                + 0.10 * float(np.clip(density / 1.5, 0, 1)) + 0.15 * excite_n)
        out.append({"start": a, "end": b, "heur": round(heur, 4),
                    "signals": {"share": round(share, 2), "their_seconds": round(len(mine) * voice.HOP, 1),
                                "hype_z": round(float(z.mean()), 2), "loud_share": round(energy, 2),
                                "pace": round(float(density), 2), "excitement": round(excite, 1)}})
    # non-maximum suppression: best first, no heavy overlaps
    out.sort(key=lambda c: -c["heur"])
    picked: list[dict] = []
    for c in out:
        if all(min(c["end"], p["end"]) - max(c["start"], p["start"]) < 0.3 * target for p in picked):
            picked.append(c)
        if len(picked) >= k:
            break
    return picked


def _snap(c: dict, tr: dict, length: str, duration: float) -> tuple[float, float]:
    """Expand to whole transcript sentences so clips don't start or end mid-word."""
    lo, target, hi = LENGTHS.get(length, LENGTHS["medium"])
    lines = _lines_for(tr, c["start"], c["end"])
    a = min([c["start"]] + [s["start"] for s in lines[:1]])
    b = max([c["end"]] + [s["end"] for s in lines[-1:]])
    if b - a > hi:
        b = a + hi
    return max(0.0, a - 0.3), min(duration, b + 0.5)


def _transcript_block(lines: list[dict], idx: dict, pres: dict, name: str) -> tuple[str, list[bool]]:
    loud_z = _robust_z(idx["loud"])
    mid = (idx["starts"] + idx["ends"]) / 2
    rows, who = [], []
    for i, s in enumerate(lines):
        mine = _speaker_of(s, idx, pres["hit"])
        who.append(mine)
        sel = np.where((mid >= s["start"]) & (mid <= s["end"]))[0]
        loud = len(sel) and float(loud_z[sel].max()) > 2.0
        rows.append(f"{i} [{fmt_ts(s['start'])}] {name if mine else 'Other'}: {s['text'].strip()}{'  (LOUD)' if loud else ''}")
    return "\n".join(rows), who


def _fallback_title(lines: list[dict], who: list[bool]) -> str:
    theirs = [l["text"].strip() for l, w in zip(lines, who) if w] or [l["text"].strip() for l in lines]
    best = max(theirs, key=lambda t: (t.count("!"), len(t) if len(t) < 70 else 0)) if theirs else "Moment"
    best = re.sub(r"\s+", " ", best).strip(" .,")
    return (best[:57] + "…") if len(best) > 58 else best


# ------------------------------------------------------------------ main job

def find(job: Job, person_id: str, source_ids: list[str], length: str = "medium", per_vod: int = 6,
         sensitivity: float | None = None, use_ai: bool = True) -> dict:
    p = voice.load_person(person_id)
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    can_ai = use_ai and ai.available()
    notes: list[str] = []
    if use_ai and not can_ai:
        notes.append("No AI model available, so clips were ranked by sound and talk-time only and titled from the transcript.")
    total = 0
    per: dict = {}
    n = len(source_ids)
    for vi, sid in enumerate(source_ids):
        base, span = vi / n, 1 / n
        src = db.source(sid)
        if not src or src["kind"] not in ("video", "audio"):
            continue
        tr = db.transcript(sid)
        if not tr or not tr["words"]:
            notes.append(f"{src['title']}: no transcript yet (still indexing?), skipped.")
            continue
        job.update(base, f"{src['title']}: analysing voices")
        idx = voice.ensure_index(sid, job, base, base + span * 0.35)
        pres = voice.presence(p, idx, sensitivity)
        dur = float(src.get("duration") or (idx["ends"].max() if len(idx["ends"]) else 0))
        their = sum(b - a for a, b in pres["segments"])
        per[sid] = {"title": src["title"], "talk_seconds": round(their, 1), "segments": len(pres["segments"]), "clips": 0}
        if not pres["segments"]:
            notes.append(f"{src['title']}: didn't hear {p['name']} (try a looser sensitivity).")
            continue
        cands = candidates(idx, pres, tr, dur, length, k=max(per_vod * 3, 6) if can_ai else per_vod)
        if can_ai:
            cands = cands[: min(len(cands), max(per_vod * 2, per_vod + 4), 18)]
        results = []
        for ci, c in enumerate(cands):
            job.update(base + span * (0.35 + 0.6 * ci / max(1, len(cands))),
                       f"{src['title']}: {'judging' if can_ai else 'scoring'} moment {ci + 1}/{len(cands)} at {fmt_ts(c['start'])}")
            a, b = _snap(c, tr, length, dur)
            lines = _lines_for(tr, a, b)
            if not lines:
                continue
            block, who = _transcript_block(lines, idx, pres, p["name"])
            clip = {"start": a, "end": b, "heur": c["heur"], "signals": c["signals"], "ai": None,
                    "title": _fallback_title(lines, who), "why": None, "transcript": block}
            if can_ai:
                try:
                    raw = ai.chat_once(
                        [{"role": "system", "content": SYSTEM.format(name=p["name"])},
                         {"role": "user", "content": f"Moment from \"{src['title']}\" ({fmt_ts(a)}–{fmt_ts(b)}):\n\n{block}"}],
                        max_tokens=220, temperature=0.3, schema=CLIP_SCHEMA)
                    j = json.loads(raw)
                    sl = int(np.clip(j.get("start_line", 0), 0, len(lines) - 1))
                    el = int(np.clip(j.get("end_line", len(lines) - 1), sl, len(lines) - 1))
                    lo, _t, hi = LENGTHS.get(length, LENGTHS["medium"])
                    # the model picks the core; grow it with neighbouring lines (setup first) up to the minimum length
                    take_before = True
                    while lines[el]["end"] - lines[sl]["start"] < lo and (sl > 0 or el < len(lines) - 1):
                        if sl > 0 and (take_before or el == len(lines) - 1):
                            sl -= 1
                        else:
                            el += 1
                        take_before = not take_before
                    na, nb = max(0.0, lines[sl]["start"] - 0.4), min(dur, lines[el]["end"] + 0.6)
                    if nb - na < lo:                      # still short: pad evenly with surrounding audio
                        pad = (lo - (nb - na)) / 2
                        na, nb = max(0.0, na - pad), min(dur, nb + pad)
                    clip["start"], clip["end"] = na, min(nb, na + hi * 1.3)
                    clip["ai"] = int(np.clip(j.get("score", 5), 1, 10))
                    clip["title"] = (j.get("title") or clip["title"]).strip().strip('"')[:70]
                    clip["why"] = (j.get("why") or "").strip()[:160]
                except Exception as exc:
                    job.write(f"AI judging failed at {fmt_ts(a)}: {exc}")
            if not clip["why"]:
                s = c["signals"]
                bits = [f"{int(s['share'] * 100)}% {p['name']} talking"]
                if s["hype_z"] > 2:
                    bits.append("loud reaction")
                if s["excitement"] >= 3:
                    bits.append("lots of energy in the words")
                clip["why"] = ", ".join(bits)
            clip["score"] = round(0.55 * (clip["ai"] / 10) + 0.45 * clip["heur"], 4) if clip["ai"] else clip["heur"]
            results.append(clip)
        results.sort(key=lambda c: -c["score"])
        for clip in results[:per_vod]:
            db.run("INSERT INTO clips(id,run_id,person_id,source_id,start,end,score,ai_score,title,why,signals,transcript,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex[:10], run_id, person_id, sid, round(clip["start"], 2), round(clip["end"], 2),
                    clip["score"], clip["ai"], clip["title"], clip["why"],
                    json.dumps({**clip["signals"], "threshold": round(pres["threshold"], 3)}), clip["transcript"], time.time()))
            total += 1
            per[sid]["clips"] += 1
    job.update(1.0, f"Found {total} clip{'s' if total != 1 else ''}")
    return {"run_id": run_id, "clips": total, "per_vod": per, "notes": notes, "ai": can_ai, "person": p["name"]}


# ------------------------------------------------------------------ queries

def list_clips(run_id: str | None = None, person_id: str | None = None, status: str | None = None) -> list[dict]:
    q, args = ("SELECT c.*, s.title AS source_title, s.kind AS source_kind, p.name AS person, m.name AS marker "
               "FROM clips c JOIN sources s ON s.id=c.source_id LEFT JOIN people p ON p.id=c.person_id "
               "LEFT JOIN kill_markers m ON m.id=c.marker_id WHERE 1=1"), []
    if run_id:
        q += " AND c.run_id=?"
        args.append(run_id)
    if person_id:
        q += " AND c.person_id=?"
        args.append(person_id)
    if status:
        q += " AND c.status=?"
        args.append(status)
    else:
        q += " AND c.status!='dismissed'"
    q += " ORDER BY c.score DESC LIMIT 500" if run_id else " ORDER BY c.created_at DESC, c.score DESC LIMIT 500"
    out = []
    for r in db.q(q, args):
        d = dict(r)
        d["signals"] = json.loads(d["signals"] or "{}")
        d["exports"] = json.loads(d["exports"] or "[]")
        out.append(d)
    return out


def runs() -> list[dict]:
    rows = db.q("SELECT c.run_id, c.person_id, p.name AS person, c.marker_id, m.name AS marker, COUNT(*) AS n, "
                "MIN(c.created_at) AS created, COUNT(DISTINCT c.source_id) AS vods FROM clips c "
                "LEFT JOIN people p ON p.id=c.person_id LEFT JOIN kill_markers m ON m.id=c.marker_id "
                "GROUP BY c.run_id ORDER BY created DESC LIMIT 50")
    return [dict(r) for r in rows]


def update(cid: str, **fields) -> dict:
    allowed = {k: v for k, v in fields.items() if k in ("title", "start", "end", "status") and v is not None}
    if "start" in allowed or "end" in allowed:
        r = db.one("SELECT start, end FROM clips WHERE id=?", (cid,))
        a = float(allowed.get("start", r["start"]))
        b = float(allowed.get("end", r["end"]))
        if b - a < 1.0:
            raise ValueError("A clip has to be at least a second long")
        allowed["start"], allowed["end"] = round(max(0.0, a), 2), round(b, 2)
    if allowed:
        cols = ", ".join(f"{k}=?" for k in allowed)
        db.run(f"UPDATE clips SET {cols} WHERE id=?", (*allowed.values(), cid))
    return get_clip(cid)


def get_clip(cid: str) -> dict:
    r = db.one("SELECT c.*, s.title AS source_title, s.kind AS source_kind, p.name AS person, m.name AS marker FROM clips c "
               "JOIN sources s ON s.id=c.source_id LEFT JOIN people p ON p.id=c.person_id "
               "LEFT JOIN kill_markers m ON m.id=c.marker_id WHERE c.id=?", (cid,))
    if not r:
        raise ValueError("Clip not found")
    d = dict(r)
    d["signals"] = json.loads(d["signals"] or "{}")
    d["exports"] = json.loads(d["exports"] or "[]")
    return d


def add_export(cid: str, path: str, layout: str) -> None:
    r = db.one("SELECT exports FROM clips WHERE id=?", (cid,))
    if not r:
        return
    ex = json.loads(r["exports"] or "[]")
    ex.append({"path": path, "layout": layout, "at": time.time()})
    db.run("UPDATE clips SET exports=?, status=CASE WHEN status='new' THEN 'kept' ELSE status END WHERE id=?",
           (json.dumps(ex), cid))
