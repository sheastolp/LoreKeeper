"""Model registry for the built-in engine: catalog, local files, downloads, imports.

Where GGUF files can live:
  bundled   <install>/vendor/models        shipped in the installer (search embedding model)
  library   %LOCALAPPDATA%/Lorekeeper/models/gguf   downloads from the catalog
  imported  anywhere — paths you pointed Lorekeeper at (nothing is copied)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from . import gguf
from .config import DATA, MODELS_DIR, get_settings, resource_root, update_settings
from .jobs import Job, jobs

GGUF_DIR = MODELS_DIR / "gguf"
GGUF_DIR.mkdir(parents=True, exist_ok=True)
IMPORTS_PATH = MODELS_DIR / "imported.json"
HF = "https://huggingface.co"

# Every file is pinned by size and SHA-256 (Hugging Face's LFS oid), so a truncated or
# tampered download never gets loaded.
CATALOG = [
    {
        "id": "gemma-4-e4b",
        "name": "Gemma 4 E4B",
        "blurb": "Best answers. Reads images, charts, screenshots and scanned pages. Wants 12 GB+ of RAM, "
                 "or a GPU with 6 GB+.",
        "min_ram_gb": 12,
        "files": [
            {"repo": "ggml-org/gemma-4-E4B-it-GGUF", "file": "gemma-4-E4B-it-Q4_0.gguf", "size": 4590807392,
             "sha256": "a555b900214b477d8880e7832e0b8925e139b0159640036b09fe472b6f2097f2", "role": "chat"},
            {"repo": "ggml-org/gemma-4-E4B-it-GGUF", "file": "mmproj-gemma-4-E4B-it-Q8_0.gguf", "size": 559874816,
             "sha256": "197f49a93027f9843772bd24a6a9e0be2a32a788de5a3def330e9c585d86edd1", "role": "mmproj"},
        ],
    },
    {
        "id": "gemma-4-e2b",
        "name": "Gemma 4 E2B",
        "blurb": "Lighter and faster, same vision skills. Runs well on 8 GB of RAM or a 4 GB GPU.",
        "min_ram_gb": 6,
        "files": [
            {"repo": "ggml-org/gemma-4-E2B-it-GGUF", "file": "gemma-4-E2B-it-Q4_0.gguf", "size": 2841481184,
             "sha256": "8e30dff3ac4c8434c49a7036fa15564bdbb6044e42bf04550bf1a096ad7e6a52", "role": "chat"},
            {"repo": "ggml-org/gemma-4-E2B-it-GGUF", "file": "mmproj-gemma-4-E2B-it-Q8_0.gguf", "size": 557368064,
             "sha256": "9406f99c16d68cda4f1f0552192dcc99021ea1fc6d2fd50b1dc3ccf30d04b292", "role": "mmproj"},
        ],
    },
]
EMBED_DEFAULT = {"repo": "ggml-org/embeddinggemma-2-GGUF", "file": "embeddinggemma-2-Q8_0.gguf", "size": 309855456,
                 "sha256": "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135", "role": "embedding"}


def preset(pid: str) -> dict | None:
    return next((p for p in CATALOG if p["id"] == pid), None)


def bundled_dir() -> Path:
    return resource_root() / "vendor" / "models"


# ------------------------------------------------------------------ hardware

def total_ram_gb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMSTAT()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / 1024 ** 3, 1)
        with open("/proc/meminfo") as fh:
            kb = int(re.search(r"MemTotal:\s+(\d+)", fh.read()).group(1))
            return round(kb / 1024 ** 2, 1)
    except Exception:
        return None


def recommended_preset() -> str:
    ram = total_ram_gb()
    return "gemma-4-e4b" if ram is None or ram >= 15 else "gemma-4-e2b"


# ------------------------------------------------------------------ local files

_cache_lock = threading.Lock()
_desc_cache: dict[tuple, dict] = {}


def _describe(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, int(st.st_mtime))
    with _cache_lock:
        if key in _desc_cache:
            return _desc_cache[key]
    try:
        d = gguf.describe(path)
    except Exception as exc:
        d = {"path": str(path), "file": path.name, "size": st.st_size, "role": "invalid", "error": str(exc)}
    with _cache_lock:
        _desc_cache[key] = d
    return d


def _imports() -> list[str]:
    try:
        return json.loads(IMPORTS_PATH.read_text("utf-8"))
    except Exception:
        return []


def _save_imports(paths: list[str]) -> None:
    IMPORTS_PATH.write_text(json.dumps(sorted(set(paths)), indent=2), "utf-8")


def local_models() -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    sources = [(bundled_dir(), "bundled"), (GGUF_DIR, "library")]
    for folder, origin in sources:
        if folder.exists():
            for p in sorted(folder.glob("*.gguf")):
                d = _describe(p)
                if d and str(p) not in seen:
                    seen.add(str(p))
                    out.append({**d, "origin": origin})
    for raw in _imports():
        p = Path(raw)
        if str(p) in seen:
            continue
        seen.add(str(p))
        if not p.exists():
            out.append({"path": str(p), "file": p.name, "role": "missing", "origin": "imported", "size": 0})
            continue
        d = _describe(p)
        if d:
            out.append({**d, "origin": "imported"})
    return out


def find_mmproj(model_path: str, models: list[dict] | None = None) -> str | None:
    """Pair a chat model with its vision projector: catalog pairing, then same-folder name match."""
    models = models if models is not None else local_models()
    mp = Path(model_path)
    for p in CATALOG:
        files = {f["role"]: f["file"] for f in p["files"]}
        if files.get("chat") == mp.name:
            cand = mp.parent / files["mmproj"]
            if cand.exists():
                return str(cand)
    projs = [m for m in models if m.get("role") == "mmproj" and Path(m["path"]).parent == mp.parent]
    if not projs:
        return None
    base = re.sub(r"[-_.](i?q\d.*|bf16|f16|f32)$", "", mp.stem, flags=re.I).lower()
    for m in projs:
        if base and base in Path(m["path"]).stem.lower():
            return m["path"]
    return projs[0]["path"] if len(projs) == 1 else None


def resolve() -> dict:
    """Which files the engine should load right now, from settings + what's on disk."""
    s = get_settings()
    models = local_models()
    chats = [m for m in models if m.get("role") == "chat"]
    embeds = [m for m in models if m.get("role") == "embedding"]

    chat = next((m for m in chats if m["path"] == s.get("local_chat_model")), None)
    if not chat and chats:
        # prefer catalog models in catalog order, else the first chat model found
        order = [f["file"] for p in CATALOG for f in p["files"] if f["role"] == "chat"]
        chats.sort(key=lambda m: order.index(m["file"]) if m["file"] in order else 99)
        chat = chats[0]
    embed = next((m for m in embeds if m["path"] == s.get("local_embed_model")), None)
    if not embed and embeds:
        embed = next((m for m in embeds if m["file"] == EMBED_DEFAULT["file"]), embeds[0])
    mmproj = find_mmproj(chat["path"], models) if chat else None
    return {"chat": chat, "mmproj": mmproj, "embed": embed}


# ------------------------------------------------------------------ import

def import_paths(paths: list[str]) -> dict:
    added, errors = [], []
    current = _imports()
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files = sorted(p.glob("*.gguf"))
            if not files:
                errors.append(f"{p.name}: no .gguf files in that folder")
            paths_to_add = files
        else:
            paths_to_add = [p]
        for f in paths_to_add:
            if f.suffix.lower() != ".gguf":
                errors.append(f"{f.name}: not a .gguf file")
                continue
            try:
                d = gguf.describe(f)
            except Exception as exc:
                errors.append(f"{f.name}: {exc}")
                continue
            current.append(str(f.resolve()))
            added.append({"file": f.name, "path": str(f.resolve()), "role": d["role"], "name": d["name"]})
    _save_imports(current)
    return {"added": added, "errors": errors}


def forget(path: str, delete_file: bool = False) -> None:
    p = Path(path)
    _save_imports([x for x in _imports() if Path(x) != p])
    if delete_file and p.parent.resolve() == GGUF_DIR.resolve():
        p.unlink(missing_ok=True)
        Path(str(p) + ".part").unlink(missing_ok=True)


# ------------------------------------------------------------------ download

def installed(p: dict, local: list[dict] | None = None) -> bool:
    """True when every file of the preset exists somewhere we know about (download folder or imported)."""
    local = local if local is not None else local_models()
    have = {m["file"] for m in local if m.get("role") not in ("missing", "invalid")}
    return all(f["file"] in have for f in p["files"])


def download_file(spec: dict, dest_dir: Path, job: Job | None = None, p0: float = 0.0, p1: float = 1.0,
                  label: str = "") -> Path:
    """Resumable download (HTTP Range onto a .part file), verified by SHA-256 before it's renamed into place."""
    dest = dest_dir / spec["file"]
    if dest.exists() and dest.stat().st_size == spec["size"]:
        return dest
    part = Path(str(dest) + ".part")
    url = f"{HF}/{spec['repo']}/resolve/main/{spec['file']}"
    total = spec["size"]
    have = part.stat().st_size if part.exists() else 0
    if have > total:
        part.unlink()
        have = 0
    t_start, b_start = time.time(), have
    last_ui = 0.0
    attempts = 0
    while have < total:
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                              timeout=httpx.Timeout(30, read=60)) as r:
                if r.status_code == 200 and have:      # server ignored Range: start over
                    have = 0
                    part.unlink(missing_ok=True)
                elif r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code} from Hugging Face")
                with open(part, "ab") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        have += len(chunk)
                        now = time.time()
                        if job and now - last_ui > 0.5:
                            last_ui = now
                            rate = (have - b_start) / max(0.001, now - t_start)
                            eta = (total - have) / rate if rate > 0 else 0
                            gib = 1024 ** 3   # same units Windows Explorer shows
                            job.update(p0 + (p1 - p0) * have / total,
                                       f"{label}{have / gib:.2f} / {total / gib:.2f} GB, "
                                       f"{rate / 1024 ** 2:.1f} MB/s, {_eta(eta)} left")
            attempts = 0
        except Exception as exc:
            if job and job.cancelled:
                raise
            attempts += 1
            if attempts > 6:
                raise RuntimeError(f"Download keeps failing ({exc}). It will resume where it stopped next time.")
            if job:
                job.write(f"connection dropped at {have / 1e6:.0f} MB ({exc}); resuming in {2 * attempts}s")
            time.sleep(2 * attempts)
    if job:
        job.update(p1, f"{label}checking SHA-256")
    digest = _sha256(part, job)
    if digest != spec["sha256"]:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{spec['file']} failed its SHA-256 check (got {digest[:12]}…). Deleted it; try again.")
    os.replace(part, dest)
    return dest


def _sha256(path: Path, job: Job | None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
            if job and job.cancelled:
                from .jobs import Cancelled
                raise Cancelled()
    return h.hexdigest()


def _eta(sec: float) -> str:
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    if sec >= 60:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec}s"


_active: dict[str, Job] = {}


def start_download(pid: str, make_default: bool = True) -> Job:
    p = preset(pid)
    if not p:
        raise ValueError(f"Unknown model {pid}")
    running = _active.get(pid)
    if running and running.status in ("queued", "running"):
        return running
    total = sum(f["size"] for f in p["files"])

    def work(job: Job):
        done = 0
        for f in p["files"]:
            a = done / total
            b = (done + f["size"]) / total
            label = "Vision file: " if f["role"] == "mmproj" else "Model: "
            download_file(f, GGUF_DIR, job, a, b, label)
            done += f["size"]
        chat_file = next(f["file"] for f in p["files"] if f["role"] == "chat")
        if make_default:
            update_settings({"local_chat_model": str(GGUF_DIR / chat_file), "provider": "builtin"})
        from . import ai   # late import: ai imports this module
        threading.Thread(target=ai.warm_up, daemon=True).start()
        return {"model": p["name"], "path": str(GGUF_DIR / chat_file)}

    job = jobs.submit("download", f"Download {p['name']}", work, lane="misc")
    job.preset = pid  # type: ignore[attr-defined]
    _active[pid] = job
    return job


def active_downloads() -> dict:
    return {pid: j.to_dict() for pid, j in _active.items() if j.status in ("queued", "running", "error")}


def catalog_view() -> list[dict]:
    ram = total_ram_gb()
    rec = recommended_preset()
    local = local_models()
    out = []
    for p in CATALOG:
        part_bytes = sum((GGUF_DIR / (f["file"] + ".part")).stat().st_size
                         for f in p["files"] if (GGUF_DIR / (f["file"] + ".part")).exists())
        out.append({
            "id": p["id"], "name": p["name"], "blurb": p["blurb"],
            "size": sum(f["size"] for f in p["files"]), "installed": installed(p, local),
            "recommended": p["id"] == rec, "fits": ram is None or ram >= p["min_ram_gb"],
            "partial": part_bytes,
            "chat_path": str(GGUF_DIR / next(f["file"] for f in p["files"] if f["role"] == "chat")),
        })
    return out


# ------------------------------------------------------------------ first run (installer choice)

def installer_choice() -> str | None:
    """The installer writes the model picked on its 'AI model' page to install.ini."""
    ini = DATA / "install.ini"
    if not ini.exists():
        return None
    try:
        import configparser
        cp = configparser.ConfigParser()
        cp.read(ini, encoding="utf-8")
        return cp.get("model", "preset", fallback=None) or None
    except Exception:
        return None


def consume_installer_choice() -> str | None:
    choice = installer_choice()
    if choice:
        try:
            (DATA / "install.ini").unlink()
        except Exception:
            pass
    return choice
