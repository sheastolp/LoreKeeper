"""Lorekeeper entry point: local API server + native window.

  python -m app             desktop window (pywebview / WebView2)
  python -m app --browser   open in your normal browser instead (handy for dev)
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser

from .config import APP_NAME, APP_VERSION, LOG_DIR, TMP_DIR


def _fix_windowed_streams() -> None:
    # A --windowed PyInstaller build has no console: sys.stdout is None and
    # uvicorn's logger crashes on .isatty(). Route everything to a log file.
    if sys.stdout is None or sys.stderr is None:
        log = open(LOG_DIR / "lorekeeper.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cleanup_tmp() -> None:
    for p in TMP_DIR.glob("*"):
        try:
            if time.time() - p.stat().st_mtime > 86400:
                p.unlink()
        except Exception:
            pass


def start_server(port: int):
    import uvicorn
    from .server import app, set_port
    set_port(port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                            log_config=None, timeout_keep_alive=30)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name="api")
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server


class DesktopAPI:
    """Functions the page can call through window.pywebview.api.*"""

    def __init__(self):
        self.window = None

    def pick_files(self):
        import webview
        types = ("Everything Lorekeeper reads (*.mp4;*.mkv;*.mov;*.avi;*.webm;*.m4v;*.wmv;*.flv;*.ts;*.mp3;*.wav;"
                 "*.m4a;*.aac;*.flac;*.ogg;*.opus;*.jpg;*.jpeg;*.png;*.webp;*.gif;*.bmp;*.tif;*.tiff;*.pdf;*.docx;"
                 "*.pptx;*.txt;*.md;*.html;*.htm;*.csv;*.json;*.srt;*.vtt)", "All files (*.*)")
        res = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=types)
        return list(res or [])

    def pick_folder(self):
        import webview
        res = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        return res[0] if res else None


def _bind_native_drop(window) -> None:
    """Dropped files: index them where they are instead of copying gigabytes through the page.

    pywebview exposes the real path (pywebviewFullPath) only to Python-side DOM handlers.
    The page waits briefly for 'lk-native-drop'; if it reports 0 files it falls back to uploading.
    """
    import json
    try:
        from webview.dom import DOMEventHandler
    except Exception:
        return

    def on_drop(e):
        files = ((e or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if isinstance(f, dict) and f.get("pywebviewFullPath")]
        added, errors = 0, []
        if paths:
            from .ingest import pipeline
            for p in paths:
                try:
                    pipeline.add_path(p)
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{os.path.basename(p)}: {exc}")
        detail = json.dumps({"handled": len(paths), "added": added, "errors": errors})
        window.evaluate_js(f"window.dispatchEvent(new CustomEvent('lk-native-drop', {{detail: {detail}}}))")

    try:
        window.dom.document.events.drop += DOMEventHandler(on_drop, False, False)
    except Exception:
        logging.exception("native drop binding unavailable; falling back to uploads")


def selftest(out_path: str) -> int:
    """Used by CI against the frozen build: proves every heavy dependency made it into the bundle."""
    import json
    import subprocess
    import traceback
    _fix_windowed_streams()
    report: dict = {}
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            report[name] = fn() or "ok"
        except Exception:
            ok = False
            report[name] = "FAIL " + traceback.format_exc()[-800:]

    def _imports():
        import av, ctranslate2, faster_whisper, numpy, onnxruntime, pypdf, pypdfium2, trafilatura  # noqa: F401
        import docx, pptx, bs4, webview  # noqa: F401
        return f"faster-whisper {faster_whisper.__version__}, ctranslate2 {ctranslate2.__version__}"

    def _ffmpeg():
        from .config import ffmpeg_bin, ffprobe_bin, popen_kwargs
        v = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True, **popen_kwargs()).stdout.splitlines()[0]
        subprocess.run([ffprobe_bin(), "-version"], capture_output=True, check=True, **popen_kwargs())
        return v

    def _whisper():
        import numpy as np
        from faster_whisper import WhisperModel
        from .config import MODELS_DIR
        m = WhisperModel("tiny", device="cpu", compute_type="int8", download_root=str(MODELS_DIR / "whisper"))
        t = np.linspace(0, 2, 32000, dtype=np.float32)
        segs, info = m.transcribe(0.1 * np.sin(2 * np.pi * 440 * t), vad_filter=True, word_timestamps=True)
        list(segs)
        return f"tiny model ran, lang guess {info.language}"

    def _server():
        import httpx
        port = _free_port()
        start_server(port)
        from .server import TOKEN
        r = httpx.get(f"http://127.0.0.1:{port}/api/status", headers={"x-lk": TOKEN, "host": f"127.0.0.1:{port}"}, timeout=10)
        r.raise_for_status()
        idx = httpx.get(f"http://127.0.0.1:{port}/?t={TOKEN}", timeout=10)
        assert "Lorekeeper" in idx.text, "index.html missing from bundle"
        return f"status {r.status_code}, ffmpeg={r.json()['ffmpeg']}"

    for name, fn in (("imports", _imports), ("ffmpeg", _ffmpeg), ("whisper", _whisper), ("server", _server)):
        check(name, fn)
    report["result"] = "PASS" if ok else "FAIL"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.json"
        os._exit(selftest(out))
    _fix_windowed_streams()
    logging.basicConfig(filename=str(LOG_DIR / "lorekeeper.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _cleanup_tmp()
    port = int(os.environ.get("LOREKEEPER_PORT") or _free_port())
    browser = "--browser" in sys.argv
    if not browser:
        os.environ["LOREKEEPER_DESKTOP"] = "1"
    server = start_server(port)
    from .server import TOKEN
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    logging.info("%s %s listening on 127.0.0.1:%s", APP_NAME, APP_VERSION, port)

    if browser:
        print(f"{APP_NAME} running at {url}  (Ctrl+C to quit)", flush=True)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
        return

    import webview
    api = DesktopAPI()
    window = webview.create_window(f"{APP_NAME}", url, js_api=api, width=1440, height=900,
                                   min_size=(1024, 680), background_color="#0B0D10", text_select=True)
    api.window = window
    window.events.loaded += lambda: _bind_native_drop(window)
    webview.start(private_mode=False, storage_path=str(LOG_DIR.parent / "webview"))
    server.should_exit = True


if __name__ == "__main__":
    main()
