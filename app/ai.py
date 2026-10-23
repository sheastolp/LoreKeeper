"""One front door for every AI call: the built-in llama.cpp engine (default) or an Ollama server.

Callers use chat_stream / chat_once / describe_image / embed / status and never care which
backend answered. The built-in path talks to llama-server's OpenAI-compatible API.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Iterator

import httpx
import numpy as np

from . import engine, models, ollama
from .config import get_settings


class AIError(RuntimeError):
    pass


def provider() -> str:
    return "ollama" if get_settings().get("provider") == "ollama" else "builtin"


# ------------------------------------------------------------------ embedding prompts
# Embedding models are trained with task prefixes; using them puts queries and passages
# in the matching halves of the vector space. EmbeddingGemma also takes the document title.

def _embed_family(name: str) -> str:
    n = name.lower()
    if "embeddinggemma" in n or "embedding-gemma" in n:
        return "gemma"
    if "nomic-embed" in n:
        return "nomic"
    if any(k in n for k in ("mxbai-embed", "snowflake-arctic-embed", "bge-")):
        return "bge"
    return ""


def format_for_embedding(texts: list[str], model_name: str, is_query: bool,
                         titles: list[str | None] | None = None) -> list[str]:
    fam = _embed_family(model_name)
    out = []
    for i, t in enumerate(texts):
        t = t[:6000]
        if fam == "gemma":
            if is_query:
                out.append(f"task: question answering | query: {t}")
            else:
                title = (titles[i] if titles else None) or "none"
                out.append(f"title: {title} | text: {t}")
        elif fam == "nomic":
            out.append(("search_query: " if is_query else "search_document: ") + t)
        elif fam == "bge" and is_query:
            out.append("Represent this sentence for searching relevant passages: " + t)
        else:
            out.append(t)
    return out


def embed_model_id() -> str:
    """Identifies the vector space; vectors from different models must never be mixed."""
    if provider() == "ollama":
        return "ollama:" + get_settings()["embed_model"]
    e = models.resolve()["embed"]
    return "local:" + (e["file"] if e else "none")


def embed(texts: list[str], is_query: bool = False, titles: list[str | None] | None = None,
          batch: int = 16) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    if provider() == "ollama":
        name = get_settings()["embed_model"]
        prepared = format_for_embedding(texts, name, is_query, titles)
        return ollama.embed_raw(prepared)
    e = models.resolve()["embed"]
    if not e:
        raise AIError("No search (embedding) model found. Reinstall Lorekeeper or add one in Settings → AI.")
    srv = engine.embed.ensure(engine.embed_args(), e["path"], None, timeout=120)
    prepared = format_for_embedding(texts, e["file"], is_query, titles)
    vecs: list[list[float]] = []
    for i in range(0, len(prepared), batch):
        try:
            r = httpx.post(srv.url + "/v1/embeddings", headers=srv.headers, json={"input": prepared[i:i + batch]},
                           timeout=httpx.Timeout(10, read=600))
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AIError(f"Embedding failed: {_err_text(exc.response)}") from exc
        except httpx.HTTPError as exc:
            raise AIError(f"Embedding failed: {exc}") from exc
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        vecs.extend(d["embedding"] for d in data)
        srv.last_used = time.time()
    arr = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


# ------------------------------------------------------------------ chat

def _chat_server(need_vision: bool = False) -> engine.Server:
    r = models.resolve()
    if not r["chat"]:
        raise AIError("No AI model installed yet. Open Settings → AI to download one (or add a GGUF you already have).")
    if need_vision and not r["mmproj"]:
        raise AIError(f"{r['chat']['name']} can't see images: its vision file (mmproj-*.gguf) wasn't found next to it.")
    return engine.chat.ensure(engine.chat_args(), r["chat"]["path"], r["mmproj"], timeout=600)


def _err_text(resp: httpx.Response) -> str:
    try:
        resp.read()
        j = resp.json()
        e = j.get("error")
        return (e.get("message") if isinstance(e, dict) else e) or resp.text[:200]
    except Exception:
        return f"HTTP {resp.status_code}"


def chat_stream(messages: list[dict], max_tokens: int = 1500) -> Iterator[dict]:
    """Yields {"type":"token","text":...} chunks, then one {"type":"stats",...}."""
    if provider() == "ollama":
        yield from ollama.chat_stream(messages)
        return
    s = get_settings()
    srv = _chat_server()
    body = {"messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": float(s["temperature"]),
            "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": True}
    filt = ollama.ThinkFilter()
    timings: dict = {}
    model_name = Path(srv.model_path or "").stem
    try:
        with httpx.stream("POST", srv.url + "/v1/chat/completions", headers=srv.headers, json=body,
                          timeout=httpx.Timeout(10, read=900)) as r:
            if r.status_code >= 400:
                raise AIError(f"The AI engine refused the request: {_err_text(r)}")
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                msg = json.loads(data)
                if msg.get("timings"):
                    timings = msg["timings"]
                for ch in msg.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        clean = filt.feed(piece)
                        if clean:
                            yield {"type": "token", "text": clean}
                srv.last_used = time.time()
    except httpx.HTTPError as exc:
        raise AIError(f"Lost contact with the AI engine: {exc}") from exc
    tail = filt.flush()
    if tail:
        yield {"type": "token", "text": tail}
    info = srv.info()
    yield {
        "type": "stats", "model": model_name,
        "tokens": timings.get("predicted_n"), "prompt_tokens": timings.get("prompt_n"),
        "cached_tokens": timings.get("cache_n"),
        "tok_per_s": round(timings["predicted_per_second"], 1) if timings.get("predicted_per_second") else None,
        "prompt_tok_per_s": round(timings["prompt_per_second"], 1) if timings.get("prompt_per_second") else None,
        "load_s": 0, "backend": _backend_label(info),
    }


def _backend_label(info: dict) -> str:
    if info.get("devices"):
        d = info["devices"][0]
        off = info.get("offload")
        layers = f", {off[0]}/{off[1]} layers on GPU" if off else ""
        return f"{d['name']} ({info['backend']}{layers})"
    return "CPU"


def chat_once(messages: list[dict], images_jpeg: list[bytes] | None = None, max_tokens: int = 400,
              temperature: float = 0.2) -> str:
    if provider() == "ollama":
        b64 = [base64.b64encode(b).decode() for b in images_jpeg] if images_jpeg else None
        model = get_settings()["vision_model"] if images_jpeg else None
        return ollama.chat_once(messages, model=model, images_b64=b64)
    srv = _chat_server(need_vision=bool(images_jpeg))
    msgs = [dict(m) for m in messages]
    if images_jpeg:
        parts = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
                 for b in images_jpeg]
        msgs[-1]["content"] = parts + [{"type": "text", "text": msgs[-1]["content"]}]
    body = {"messages": msgs, "stream": False, "max_tokens": max_tokens, "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        r = httpx.post(srv.url + "/v1/chat/completions", headers=srv.headers, json=body,
                       timeout=httpx.Timeout(10, read=900))
    except httpx.HTTPError as exc:
        raise AIError(f"Lost contact with the AI engine: {exc}") from exc
    if r.status_code >= 400:
        raise AIError(f"The AI engine refused the request: {_err_text(r)}")
    srv.last_used = time.time()
    text = r.json()["choices"][0]["message"].get("content") or ""
    f = ollama.ThinkFilter()
    return (f.feed(text) + f.flush()).strip()


def describe_image(jpeg: bytes, prompt: str) -> str:
    return chat_once([{"role": "user", "content": prompt}], images_jpeg=[jpeg], max_tokens=400)


# ------------------------------------------------------------------ status

def warm_up() -> None:
    """Load models in the background at launch so the first question doesn't wait."""
    if provider() != "builtin":
        return
    r = models.resolve()
    for fn in ((lambda: engine.embed.ensure(engine.embed_args(), r["embed"]["path"], None)) if r["embed"] else None,
               (lambda: _chat_server()) if r["chat"] else None):
        if fn:
            try:
                fn()
            except Exception:
                pass


def status() -> dict:
    s = get_settings()
    if provider() == "ollama":
        try:
            names = [m["name"] for m in ollama.list_models()]
            ok, err = True, None
        except Exception as exc:
            names, ok, err = [], False, str(exc)

        def have(n: str) -> bool:
            return n in names or (":" not in n and f"{n}:latest" in names)

        missing = [n for n in (s["chat_model"], s["vision_model"], s["embed_model"]) if ok and not have(n)]
        state = "offline" if not ok else ("missing" if missing else "ready")
        return {"provider": "ollama", "state": state, "ready": ok and not missing, "error": err,
                "missing": missing, "ollama_version": ollama.version() if ok else None, "ollama_models": names}
    r = models.resolve()
    chat_info, embed_info = engine.chat.info(), engine.embed.info()
    if not engine.server_binary():
        state = "no-engine"
    elif not r["chat"]:
        state = "no-model"
    elif chat_info["state"] == "error":
        state = "error"
    elif chat_info["state"] in ("loading",):
        state = "loading"
    elif chat_info["state"] == "sleeping":
        state = "sleeping"
    elif chat_info["state"] == "ready":
        state = "ready"
    else:
        state = "idle"            # model present, engine not started yet (starts on first use)
    return {
        "provider": "builtin", "state": state, "ready": bool(r["chat"]) and state not in ("error", "no-engine"),
        "error": chat_info.get("error") if state == "error" else None,
        "chat_model": r["chat"], "mmproj": Path(r["mmproj"]).name if r["mmproj"] else None,
        "vision": bool(r["mmproj"]), "embed_model": r["embed"],
        "engine": {"chat": chat_info, "embed": embed_info, "binary": bool(engine.server_binary()),
                   "label": _backend_label(chat_info) if chat_info["state"] in ("ready", "sleeping") else None},
        "downloads": models.active_downloads(),
        "ram_gb": models.total_ram_gb(),
    }
