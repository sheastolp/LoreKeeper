"""Caption export (SRT / WebVTT) from word timestamps, remapped through any transcript cuts."""
from __future__ import annotations

from bisect import bisect_right

from .ops import transcript_keeps


class TimeMap:
    """Maps original-media time → edited-media time once segments have been cut out."""

    def __init__(self, keeps: list[tuple[float, float]]):
        self.keeps = keeps
        self.starts = [a for a, _ in keeps]
        self.offsets = []
        acc = 0.0
        for a, b in keeps:
            self.offsets.append(acc)
            acc += b - a

    def __call__(self, t: float) -> float | None:
        i = bisect_right(self.starts, t) - 1
        if i < 0:
            return None
        a, b = self.keeps[i]
        if t > b + 1e-3:
            return None
        return self.offsets[i] + (t - a)


def _ts(t: float, vtt: bool) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t * 1000), 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    sep = "." if vtt else ","
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def build_cues(words: list[dict], deleted: list[int] | None, duration: float,
               max_chars: int = 84, max_secs: float = 5.5) -> list[tuple[float, float, str]]:
    dset = set(deleted or [])
    tmap = TimeMap(transcript_keeps(words, sorted(dset), duration)) if dset else None
    cues: list[tuple[float, float, str]] = []
    cur: list[tuple[float, float, str]] = []

    def flush():
        if cur:
            text = "".join(w for _, _, w in cur).strip()
            if text:
                cues.append((cur[0][0], cur[-1][1], text))
            cur.clear()

    for i, w in enumerate(words):
        if i in dset:
            continue
        s, e = w["s"], w["e"]
        if tmap:
            s2, e2 = tmap(s), tmap(e)
            if s2 is None or e2 is None:
                continue
            s, e = s2, e2
        if cur and (len("".join(x[2] for x in cur)) + len(w["w"]) > max_chars
                    or e - cur[0][0] > max_secs or s - cur[-1][1] > 0.8):
            flush()
        cur.append((s, e, w["w"]))
        if w["w"].rstrip().endswith((".", "?", "!")) and len("".join(x[2] for x in cur)) > 20:
            flush()
    flush()
    return cues


def _wrap(text: str, width: int = 42) -> str:
    if len(text) <= width:
        return text
    mid = len(text) // 2
    left, right = text.rfind(" ", 0, mid + 1), text.find(" ", mid)
    cut = left if (right < 0 or (left >= 0 and mid - left <= right - mid)) else right
    return text if cut <= 0 else text[:cut].strip() + "\n" + text[cut:].strip()


def render(cues: list[tuple[float, float, str]], fmt: str = "srt") -> str:
    vtt = fmt == "vtt"
    out = ["WEBVTT", ""] if vtt else []
    for n, (s, e, text) in enumerate(cues, 1):
        if not vtt:
            out.append(str(n))
        out.append(f"{_ts(s, vtt)} --> {_ts(max(e, s + 0.3), vtt)}")
        out.append(_wrap(text))
        out.append("")
    return "\n".join(out)
