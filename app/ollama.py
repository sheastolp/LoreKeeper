"""Ollama client: chat streaming, embeddings, vision, model management.

Notes worth knowing:
* Thinking models (qwen3, deepseek-r1) may ignore think:false, so every stream
  goes through ThinkFilter, which strips <think>...</think> even when the tag is
  split across two network chunks.
* nomic-embed-text and friends were trained with task prefixes. Embedding a
  question with "search_query:" and passages with "search_document:" lands
  them in the matching halves of the vector space; skipping it costs recall.
"""
from __future__ import annotations

import base64
import json
from typing import Iterator

import httpx
import numpy as np

from .config import get_settings


class OllamaError(RuntimeError):
    pass


def _url(path: str) -> str:
    return get_settings()["ollama_url"].rstrip("/") + path


def _wrap(exc: Exception, what: str) -> OllamaError:
    base = get_settings()["ollama_url"]
    if isinstance(exc, httpx.ConnectError):
        return OllamaError(f"Can't reach Ollama at {base}. Is it running? (Settings → Ollama URL)")
    if isinstance(exc, httpx.HTTPStatusError):
        body = ""
        try:
            body = exc.response.json().get("error", "")
        except Exception:
            body = exc.response.text[:200]
        if "not found" in body.lower():
            return OllamaError(f"{what}: model not installed — {body}. Pull it from Settings.")
        return OllamaError(f"{what}: {body or exc}")
    if isinstance(exc, httpx.TimeoutException):
        return OllamaError(f"{what}: Ollama timed out (model still loading? big image?)")
    return OllamaError(f"{what}: {exc}")


# ------------------------------------------------------------------ models

def list_models() -> list[dict]:
    try:
        r = httpx.get(_url("/api/tags"), timeout=5)
        r.raise_for_status()
    except Exception as exc:
        raise _wrap(exc, "List models") from exc
    out = []
    for m in r.json().get("models", []):
        details = m.get("details") or {}
        out.append({
            "name": m.get("name"),
            "size": m.get("size"),
            "family": details.get("family"),
            "params": details.get("parameter_size"),
            "quant": details.get("quantization_level"),
        })
    return out


def version() -> str | None:
    try:
        return httpx.get(_url("/api/version"), timeout=3).json().get("version")
    except Exception:
        return None


def pull_model(name: str, on_progress=None) -> None:
    try:
        with httpx.stream("POST", _url("/api/pull"), json={"model": name, "stream": True},
                          timeout=httpx.Timeout(30, read=None)) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise OllamaError(msg["error"])
                if on_progress:
                    total, done = msg.get("total"), msg.get("completed")
                    frac = (done / total) if total and done else None
                    on_progress(frac, msg.get("status", ""))
    except OllamaError:
        raise
    except Exception as exc:
        raise _wrap(exc, f"Pull {name}") from exc


# ------------------------------------------------------------------ embeddings

_PREFIXES = {
    # model-name fragment: (document prefix, query prefix)
    "nomic-embed": ("search_document: ", "search_query: "),
    "mxbai-embed": ("", "Represent this sentence for searching relevant passages: "),
    "snowflake-arctic-embed": ("", "Represent this sentence for searching relevant passages: "),
    "bge-": ("", "Represent this sentence for searching relevant passages: "),
}


def _prefix(model: str, is_query: bool) -> str:
    for frag, (doc, query) in _PREFIXES.items():
        if frag in model:
            return query if is_query else doc
    return ""


def embed(texts: list[str], is_query: bool = False, batch: int = 24) -> np.ndarray:
    """Return L2-normalised float32 vectors, shape (n, dim)."""
    model = get_settings()["embed_model"]
    pre = _prefix(model, is_query)
    vecs: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = [pre + t[:6000] for t in texts[i:i + batch]]
        try:
            r = httpx.post(_url("/api/embed"), json={"model": model, "input": part, "truncate": True},
                           timeout=httpx.Timeout(30, read=300))
            if r.status_code == 404 and "model" not in r.text.lower():
                vecs.extend(_embed_legacy(model, part))   # Ollama < 0.3
                continue
            r.raise_for_status()
            vecs.extend(r.json()["embeddings"])
        except OllamaError:
            raise
        except Exception as exc:
            raise _wrap(exc, f"Embed ({model})") from exc
    arr = np.asarray(vecs, dtype=np.float32)
    if arr.size == 0:
        return arr.reshape(0, 0)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def _embed_legacy(model: str, texts: list[str]) -> list[list[float]]:
    out = []
    for t in texts:
        r = httpx.post(_url("/api/embeddings"), json={"model": model, "prompt": t}, timeout=120)
        r.raise_for_status()
        out.append(r.json()["embedding"])
    return out


# ------------------------------------------------------------------ chat

class ThinkFilter:
    """Streaming filter that drops <think>…</think> spans, even split across chunks."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                if not self.inside:
                    out.append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # keep a tail that might be the start of a tag
            keep = 0
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    keep = k
                    break
            if not self.inside:
                out.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


def chat_stream(messages: list[dict], model: str | None = None,
                options: dict | None = None) -> Iterator[dict]:
    """Yields {"type":"token","text":...} then one {"type":"stats",...}."""
    s = get_settings()
    model = model or s["chat_model"]
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": {"num_ctx": s["num_ctx"], "temperature": s["temperature"], **(options or {})},
    }
    filt = ThinkFilter()
    try:
        with httpx.stream("POST", _url("/api/chat"), json=payload,
                          timeout=httpx.Timeout(30, read=600)) as r:
            if r.status_code >= 400:
                r.read()
                if r.status_code == 400 and "think" in r.text.lower():
                    # older Ollama or a model that rejects the think flag: retry without it
                    payload.pop("think", None)
                    yield from _chat_stream_payload(payload, filt)
                    return
                r.raise_for_status()
            yield from _consume(r, filt)
    except OllamaError:
        raise
    except Exception as exc:
        raise _wrap(exc, f"Chat ({model})") from exc


def _chat_stream_payload(payload: dict, filt: ThinkFilter) -> Iterator[dict]:
    with httpx.stream("POST", _url("/api/chat"), json=payload, timeout=httpx.Timeout(30, read=600)) as r:
        r.raise_for_status()
        yield from _consume(r, filt)


def _consume(r: httpx.Response, filt: ThinkFilter) -> Iterator[dict]:
    for line in r.iter_lines():
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("error"):
            raise OllamaError(msg["error"])
        piece = (msg.get("message") or {}).get("content") or ""
        if piece:
            clean = filt.feed(piece)
            if clean:
                yield {"type": "token", "text": clean}
        if msg.get("done"):
            tail = filt.flush()
            if tail:
                yield {"type": "token", "text": tail}
            ev_n, ev_d = msg.get("eval_count") or 0, msg.get("eval_duration") or 0
            pr_n, pr_d = msg.get("prompt_eval_count") or 0, msg.get("prompt_eval_duration") or 0
            yield {
                "type": "stats",
                "model": msg.get("model"),
                "tokens": ev_n,
                "prompt_tokens": pr_n,
                "tok_per_s": round(ev_n / (ev_d / 1e9), 1) if ev_d else None,
                "prompt_tok_per_s": round(pr_n / (pr_d / 1e9), 1) if pr_d else None,
                "load_s": round((msg.get("load_duration") or 0) / 1e9, 2),
                "total_s": round((msg.get("total_duration") or 0) / 1e9, 2),
            }


def chat_once(messages: list[dict], model: str | None = None, images_b64: list[str] | None = None,
              options: dict | None = None, timeout: float = 300) -> str:
    s = get_settings()
    model = model or s["chat_model"]
    msgs = [dict(m) for m in messages]
    if images_b64:
        msgs[-1]["images"] = images_b64
    payload = {"model": model, "messages": msgs, "stream": False, "think": False,
               "options": {"num_ctx": min(s["num_ctx"], 8192), "temperature": 0.2, **(options or {})}}
    try:
        r = httpx.post(_url("/api/chat"), json=payload, timeout=httpx.Timeout(30, read=timeout))
        if r.status_code == 400 and "think" in r.text.lower():
            payload.pop("think")
            r = httpx.post(_url("/api/chat"), json=payload, timeout=httpx.Timeout(30, read=timeout))
        r.raise_for_status()
    except Exception as exc:
        raise _wrap(exc, f"Chat ({model})") from exc
    text = (r.json().get("message") or {}).get("content", "")
    f = ThinkFilter()
    return (f.feed(text) + f.flush()).strip()


def describe_image(jpeg_bytes: bytes, prompt: str) -> str:
    model = get_settings()["vision_model"]
    b64 = base64.b64encode(jpeg_bytes).decode()
    return chat_once([{"role": "user", "content": prompt}], model=model, images_b64=[b64])
