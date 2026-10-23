"""Paths, settings and platform helpers.

Everything Lorekeeper writes lives under one data folder:
  Windows: %LOCALAPPDATA%\\Lorekeeper
  Other:   ~/.lorekeeper
Override with the LOREKEEPER_HOME environment variable.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

APP_NAME = "Lorekeeper"
APP_VERSION = "1.2.0"
IS_WINDOWS = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)


def resource_root() -> Path:
    """Where bundled read-only resources (web/, vendor/) live."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    env = os.environ.get("LOREKEEPER_HOME")
    if env:
        base = Path(env)
    elif IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
    else:
        base = Path.home() / ".lorekeeper"
    for sub in ("library", "derived", "models", "logs", "tmp"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


DATA = data_dir()
LIBRARY_DIR = DATA / "library"      # files uploaded through drag-and-drop
DERIVED_DIR = DATA / "derived"      # thumbnails, waveforms, frames, proxies
MODELS_DIR = DATA / "models"        # Whisper weights
LOG_DIR = DATA / "logs"
TMP_DIR = DATA / "tmp"
DB_PATH = DATA / "lorekeeper.db"
SETTINGS_PATH = DATA / "settings.json"


def default_export_dir() -> str:
    videos = Path.home() / "Videos"
    base = videos if videos.exists() else Path.home()
    return str(base / "Lorekeeper Exports")


DEFAULTS = {
    # which brain answers: the bundled llama.cpp engine, or an Ollama server you run yourself
    "provider": "builtin",
    "local_chat_model": "",          # path to a chat GGUF ("" = pick automatically)
    "local_embed_model": "",         # path to an embedding GGUF ("" = bundled EmbeddingGemma 2)
    "gpu_mode": "auto",              # auto = llama.cpp fits as many layers as VRAM allows; cpu = no GPU
    "threads": 0,                    # 0 = llama.cpp decides
    "idle_sleep_min": 10,            # unload the model after this many idle minutes (0 = never)
    "ollama_url": "http://127.0.0.1:11434",
    "chat_model": "llama3.2:3b",     # Ollama model names (used when provider = ollama)
    "vision_model": "gemma3:4b",
    "embed_model": "nomic-embed-text",
    "num_ctx": 8192,
    "temperature": 0.4,
    "top_k": 8,
    "whisper_model": "base",
    "whisper_device": "cpu",
    "whisper_language": "",          # blank = auto-detect
    "max_keyframes": 16,
    "caption_frames": True,
    "ocr_scanned_pdfs": True,
    "export_dir": default_export_dir(),
}

_lock = threading.Lock()
_settings: dict | None = None


def get_settings() -> dict:
    global _settings
    with _lock:
        if _settings is None:
            data = {}
            if SETTINGS_PATH.exists():
                try:
                    data = json.loads(SETTINGS_PATH.read_text("utf-8"))
                except Exception:
                    data = {}
            _settings = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
        return dict(_settings)


def update_settings(patch: dict) -> dict:
    global _settings
    current = get_settings()
    for key, value in patch.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
            else:
                value = str(value).strip()
        except (TypeError, ValueError):
            continue
        current[key] = value
    current["ollama_url"] = current["ollama_url"].rstrip("/") or DEFAULTS["ollama_url"]
    with _lock:
        _settings = current
        SETTINGS_PATH.write_text(json.dumps(current, indent=2), "utf-8")
    return dict(current)


# ---------------------------------------------------------------- binaries

def _find_binary(name: str) -> str:
    exe = name + (".exe" if IS_WINDOWS else "")
    candidates = [
        resource_root() / "vendor" / "ffmpeg" / exe,
        Path(sys.executable).parent / "vendor" / "ffmpeg" / exe,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"{name} not found. The installer bundles it; when running from source, "
        f"install FFmpeg and make sure '{name}' is on your PATH."
    )


def ffmpeg_bin() -> str:
    return _find_binary("ffmpeg")


def ffprobe_bin() -> str:
    return _find_binary("ffprobe")


def popen_kwargs() -> dict:
    """Hide the console window FFmpeg would otherwise flash on Windows."""
    if IS_WINDOWS:
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def run_quiet(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, **popen_kwargs(), **kw)
