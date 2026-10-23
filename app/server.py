"""HTTP API + static UI, served on 127.0.0.1 only and locked with a per-launch token."""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from . import ai, engine, models, ollama, qa
from . import search as search_mod
from .config import (APP_VERSION, DERIVED_DIR, IS_WINDOWS, ffmpeg_bin, get_settings, resource_root,
                     update_settings)
from .db import db
from .editing import ops, subtitles, timeline
from .ingest import pipeline
from .ingest.media import derived
from .jobs import jobs
from .search import search

TOKEN = os.environ.get("LOREKEEPER_TOKEN") or secrets.token_urlsafe(24)
WEB = resource_root() / "web"
app = FastAPI(title="Lorekeeper", docs_url=None, redoc_url=None, openapi_url=None)
ALLOWED_HOSTS: set[str] = set()

for ext, mt in ((".mkv", "video/x-matroska"), (".m4a", "audio/mp4"), (".opus", "audio/ogg"),
                (".webm", "video/webm"), (".flac", "audio/flac")):
    mimetypes.add_type(mt, ext)


def set_port(port: int) -> None:
    ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}"})


# ------------------------------------------------------------------ security

@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.headers.get("host", "")
    if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:          # blocks DNS-rebinding tricks
        return Response("Bad host", status_code=403)
    path = request.url.path
    if path in ("/", "/index.html"):
        if request.query_params.get("t") == TOKEN:
            resp = await call_next(request)
            resp.set_cookie("lk", TOKEN, httponly=True, samesite="strict")
            return resp
        if request.cookies.get("lk") == TOKEN:
            return await call_next(request)
        return HTMLResponse("<h3>Open Lorekeeper from its own window.</h3>", status_code=401)
    if path.startswith("/static/"):
        return await call_next(request)
    if request.cookies.get("lk") != TOKEN and request.headers.get("x-lk") != TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------ UI

@app.get("/")
def index():
    html = (WEB / "index.html").read_text("utf-8").replace("{{VERSION}}", APP_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/static/{name:path}")
def static(name: str):
    p = (WEB / name).resolve()
    if WEB.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ status & settings

@app.get("/api/status")
def status():
    try:
        ffmpeg_bin()
        ff_ok = True
    except Exception:
        ff_ok = False
    try:
        stale = search_mod.stale_sources()
    except Exception:
        stale = 0
    return {
        "version": APP_VERSION, "ffmpeg": ff_ok, "settings": get_settings(), "ai": ai.status(),
        "stale_vectors": stale, "desktop": bool(os.environ.get("LOREKEEPER_DESKTOP")),
    }


ENGINE_KEYS = {"provider", "local_chat_model", "local_embed_model", "gpu_mode", "threads", "idle_sleep_min", "num_ctx"}


@app.get("/api/settings")
def get_s():
    return get_settings()


@app.post("/api/settings")
def set_s(patch: dict = Body(...)):
    before = get_settings()
    after = update_settings(patch)
    if any(before.get(k) != after.get(k) for k in ENGINE_KEYS):
        _rewarm()
    return after


def _rewarm():
    """Engine settings changed: reload in the background so the next question doesn't wait."""
    def work():
        if ai.provider() == "ollama":
            engine.stop_all()      # free the built-in engine's memory when Ollama takes over
        else:
            ai.warm_up()
    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ built-in AI engine

@app.get("/api/ai/models")
def ai_models():
    r = models.resolve()
    return {
        "local": models.local_models(), "catalog": models.catalog_view(),
        "selected": {"chat": r["chat"]["path"] if r["chat"] else None, "mmproj": r["mmproj"],
                     "embed": r["embed"]["path"] if r["embed"] else None},
        "ram_gb": models.total_ram_gb(), "recommended": models.recommended_preset(),
        "engine_version": engine.engine_version(), "downloads": models.active_downloads(),
    }


@app.post("/api/ai/download")
def ai_download(body: dict = Body(...)):
    try:
        return models.start_download(body.get("preset") or models.recommended_preset()).to_dict()
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/ai/import")
def ai_import(body: dict = Body(...)):
    paths = [p for p in (body.get("paths") or []) if p]
    if not paths:
        raise HTTPException(400, "Pick a .gguf file first")
    res = models.import_paths(paths)
    chats = [a for a in res["added"] if a["role"] == "chat"]
    if chats and body.get("use", True):
        update_settings({"local_chat_model": chats[0]["path"], "provider": "builtin"})
        _rewarm()
    return res


@app.post("/api/ai/forget")
def ai_forget(body: dict = Body(...)):
    path = body.get("path") or ""
    if get_settings().get("local_chat_model") == path:
        update_settings({"local_chat_model": ""})
    if get_settings().get("local_embed_model") == path:
        update_settings({"local_embed_model": ""})
    if engine.chat.model_path == path or engine.embed.model_path == path:
        engine.stop_all()
    models.forget(path, delete_file=bool(body.get("delete")))
    return {"ok": True}


@app.post("/api/ai/use")
def ai_use(body: dict = Body(...)):
    patch = {}
    if "chat" in body:
        patch["local_chat_model"] = body["chat"] or ""
    if "embed" in body:
        patch["local_embed_model"] = body["embed"] or ""
    if body.get("builtin"):
        patch["provider"] = "builtin"
    return set_s(patch)


@app.post("/api/ai/restart")
def ai_restart():
    engine.stop_all()
    _rewarm()
    return {"ok": True}


@app.post("/api/ai/unload")
def ai_unload():
    engine.stop_all()
    return {"ok": True}


@app.get("/api/ai/log")
def ai_log(server: str = "chat"):
    srv = engine.embed if server == "embed" else engine.chat
    return {"lines": list(srv.log)[-300:], "info": srv.info()}


@app.post("/api/models/pull")
def pull(body: dict = Body(...)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Model name required")

    def work(job):
        ollama.pull_model(name, lambda frac, msg: job.update(frac if frac is not None else job.progress, msg))
        return {"model": name}

    return jobs.submit("pull", f"Pull {name}", work).to_dict()


@app.get("/api/ollama/models")
def ollama_models():
    try:
        return {"models": ollama.list_models(), "version": ollama.version()}
    except Exception as exc:
        return {"models": [], "error": str(exc)}


def boot() -> None:
    """Runs once at launch: honour the installer's model choice, then warm the engine up."""
    choice = models.consume_installer_choice()          # "auto", a preset id, or "none"
    if choice == "auto":
        choice = models.recommended_preset()
    if choice and models.preset(choice) and not models.installed(models.preset(choice)) and not models.resolve()["chat"]:
        models.start_download(choice)
    threading.Thread(target=ai.warm_up, daemon=True, name="warmup").start()


# ------------------------------------------------------------------ library

@app.get("/api/sources")
def sources():
    return sources_payload()


def sources_payload():
    out = []
    for s in db.sources():
        d = DERIVED_DIR / s["id"]
        s["has_thumb"] = (d / "thumb.jpg").exists()
        s["has_filmstrip"] = (d / "filmstrip.jpg").exists()
        s["has_waveform"] = (d / "waveform.png").exists()
        s["meta"].pop("description", None)
        out.append(s)
    return out


@app.get("/api/sources/{sid}")
def source_detail(sid: str):
    s = db.source(sid)
    if not s:
        raise HTTPException(404)
    s["chunks"] = db.chunks_for(sid)
    tr = db.transcript(sid)
    s["transcript"] = {"language": tr["language"], "segments": tr["segments"], "n_words": len(tr["words"])} if tr else None
    fd = derived(sid) / "frames"
    s["frames"] = sorted(p.name for p in fd.glob("*.jpg")) if fd.exists() else []
    return s


@app.post("/api/sources/upload")
async def upload(request: Request):
    name = unquote(request.query_params.get("name") or "upload.bin")
    name = Path(name).name
    if not pipeline.kind_for(Path(name)):
        raise HTTPException(400, f"Unsupported file type: {Path(name).suffix or name}")
    dest = pipeline.upload_dest(name)
    try:
        with open(dest, "wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
        sid = pipeline.register_upload(dest, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return {"id": sid}


@app.post("/api/sources/paths")
def add_paths(body: dict = Body(...)):
    added, errors = [], []
    for p in body.get("paths", []):
        try:
            added.append(pipeline.add_path(p))
        except Exception as exc:
            errors.append(f"{Path(p).name}: {exc}")
    return {"added": added, "errors": errors}


@app.post("/api/sources/url")
def add_url(body: dict = Body(...)):
    urls = [u.strip() for u in (body.get("url") or "").split() if u.strip()]
    if not urls:
        raise HTTPException(400, "Paste a link first")
    return {"added": [pipeline.add_url(u) for u in urls]}


@app.patch("/api/sources/{sid}")
def rename(sid: str, body: dict = Body(...)):
    if body.get("title"):
        db.update_source(sid, title=str(body["title"])[:200])
    return db.source(sid)


@app.post("/api/sources/{sid}/reindex")
def reindex(sid: str):
    if not db.source(sid):
        raise HTTPException(404)
    return pipeline.queue(sid).to_dict()


@app.delete("/api/sources/{sid}")
def delete_source(sid: str):
    s = db.source(sid)
    if not s:
        raise HTTPException(404)
    db.delete_source(sid)
    import shutil
    shutil.rmtree(DERIVED_DIR / sid, ignore_errors=True)
    if s["owned"] and s.get("path"):
        Path(s["path"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/reembed")
def reembed():
    def work(job):
        ids = [s["id"] for s in db.sources() if s["status"] == "ready"]
        warnings: list[str] = []
        for i, sid in enumerate(ids):
            job.update(i / max(1, len(ids)), f"Embedding {i + 1}/{len(ids)}")
            pipeline.embed_source(job, sid, warnings)
        return {"sources": len(ids), "warnings": warnings[:5]}
    return jobs.submit("reembed", "Rebuild semantic index", work, lane="ingest").to_dict()


@app.get("/api/sources/{sid}/transcript")
def transcript(sid: str):
    tr = db.transcript(sid)
    if not tr:
        raise HTTPException(404, "No transcript")
    return tr


@app.post("/api/sources/{sid}/subtitles")
def export_subs(sid: str, body: dict = Body(...)):
    s = db.source(sid)
    tr = db.transcript(sid)
    if not s or not tr:
        raise HTTPException(404, "No transcript")
    fmt = "vtt" if body.get("format") == "vtt" else "srt"
    dur = s.get("duration") or (tr["words"][-1]["e"] if tr["words"] else 0)
    cues = subtitles.build_cues(tr["words"], body.get("deleted") or [], dur)
    text = subtitles.render(cues, fmt)
    label = "captions (edited)" if body.get("deleted") else "captions"
    out = ops.export_path(s["title"], label, "." + fmt)
    Path(out).write_text(text, "utf-8")
    return {"path": out, "cues": len(cues)}


# ------------------------------------------------------------------ media files

def _file(p: Path | None, cache: bool = True):
    if not p or not p.exists():
        raise HTTPException(404)
    headers = {"Cache-Control": "max-age=3600"} if cache else {}
    return FileResponse(p, headers=headers)


@app.get("/media/{sid}")
def media(sid: str):
    s = db.source(sid)
    if not s:
        raise HTTPException(404)
    proxy = s["meta"].get("proxy")
    p = Path(proxy) if proxy and Path(proxy).exists() else (Path(s["path"]) if s.get("path") else None)
    return _file(p, cache=False)


@app.get("/thumb/{sid}")
def thumb(sid: str):
    return _file(derived(sid) / "thumb.jpg")


@app.get("/filmstrip/{sid}")
def filmstrip(sid: str):
    return _file(derived(sid) / "filmstrip.jpg")


@app.get("/waveform/{sid}")
def waveform(sid: str):
    return _file(derived(sid) / "waveform.png")


@app.get("/frames/{sid}/{name}")
def frame(sid: str, name: str):
    return _file(derived(sid) / "frames" / Path(name).name)


# ------------------------------------------------------------------ ask

@app.post("/api/ask")
def ask(body: dict = Body(...)):
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "Ask something")
    history = body.get("history") or []
    scope = body.get("source_ids") or None

    def gen():
        try:
            for ev in qa.answer_stream(question, history, scope):
                yield json.dumps(ev) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "error": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


@app.get("/api/search")
def raw_search(q: str, k: int = 12):
    return search(q, k=k)


# ------------------------------------------------------------------ editing

@app.post("/api/edit/{op}")
def edit(op: str, body: dict = Body(...)):
    fn = ops.OPS.get(op)
    if op == "render_project":
        fn = timeline.render_project
    if not fn:
        raise HTTPException(404, f"Unknown operation {op}")
    params = body.get("params") or {}
    if body.get("dry"):
        ctx = ops.Ctx(dry=True)
        try:
            fn(ctx, params)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        return {"commands": ctx.commands, "explain": ctx.explain}
    title = body.get("title") or op.replace("_", " ").title()

    def work(job):
        ctx = ops.Ctx(job=job)
        fn(ctx, params)
        job.write(ctx.explain)
        return {"outputs": ctx.outputs, "commands": ctx.commands, "explain": ctx.explain}

    return jobs.submit("render", title, work, lane="render").to_dict()


@app.get("/api/jobs")
def list_jobs():
    return jobs.list()


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404)
    j.cancel()
    return j.to_dict()


@app.get("/api/exports")
def exports():
    d = Path(get_settings()["export_dir"])
    if not d.exists():
        return []
    files = [p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"path": str(p), "name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in files[:60]]


@app.get("/exports/file")
def export_file(path: str):
    p = Path(path).resolve()
    d = Path(get_settings()["export_dir"]).resolve()
    if d not in p.parents:
        raise HTTPException(403)
    return _file(p, cache=False)


@app.post("/api/exports/add")
def export_add(body: dict = Body(...)):
    return {"id": pipeline.add_path(body["path"])}


@app.post("/api/reveal")
def reveal(body: dict = Body(...)):
    p = Path(body.get("path") or get_settings()["export_dir"])
    try:
        if IS_WINDOWS:
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                p.mkdir(parents=True, exist_ok=True)
                os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return {"ok": True}


# ------------------------------------------------------------------ timeline projects

def blank_project() -> dict:
    return {
        "settings": {"width": 1920, "height": 1080, "fps": 30},
        "tracks": [
            {"id": "v2", "type": "video", "name": "V2 Overlay", "clips": []},
            {"id": "v1", "type": "video", "name": "V1 Main", "clips": []},
            {"id": "a1", "type": "audio", "name": "A1 Voice", "clips": []},
            {"id": "a2", "type": "audio", "name": "A2 Music", "clips": []},
        ],
    }


@app.get("/api/projects")
def projects():
    return db.projects()


@app.post("/api/projects")
def new_project(body: dict = Body(default={})):
    pid = db.save_project(None, body.get("name") or f"Edit {time.strftime('%b %d %H:%M')}", blank_project())
    return db.project(pid)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = db.project(pid)
    if not p:
        raise HTTPException(404)
    return p


@app.put("/api/projects/{pid}")
def put_project(pid: str, body: dict = Body(...)):
    db.save_project(pid, body.get("name") or "Untitled", body["data"])
    return {"ok": True, "updated_at": time.time()}


@app.delete("/api/projects/{pid}")
def del_project(pid: str):
    db.delete_project(pid)
    return {"ok": True}
