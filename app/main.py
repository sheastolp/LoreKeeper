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
from pathlib import Path

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


def _shutdown_engine() -> None:
    try:
        from . import engine
        engine.stop_all()
    except Exception:
        pass


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

    def pick_models(self):
        import webview
        res = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                             file_types=("GGUF models (*.gguf)", "All files (*.*)"))
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
        import kaldi_native_fbank, yt_dlp  # noqa: F401
        import docx, pptx, bs4, webview  # noqa: F401
        return f"faster-whisper {faster_whisper.__version__}, ctranslate2 {ctranslate2.__version__}"

    def _ffmpeg():
        from .config import ffmpeg_bin, ffprobe_bin, popen_kwargs
        v = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True, **popen_kwargs()).stdout.splitlines()[0]
        subprocess.run([ffprobe_bin(), "-version"], capture_output=True, check=True, **popen_kwargs())
        return v

    def _whisper():
        import numpy as np
        from .ingest.media import load_whisper
        from .config import resource_root
        bundled = (resource_root() / "vendor" / "whisper" / "base" / "model.bin").exists()
        m = load_whisper()            # default size is base, which ships in the bundle
        t = np.linspace(0, 2, 32000, dtype=np.float32)
        segs, info = m.transcribe(0.1 * np.sin(2 * np.pi * 440 * t), vad_filter=True, word_timestamps=True)
        list(segs)
        if getattr(sys, "frozen", False) and not bundled:
            raise RuntimeError("Whisper base is missing from vendor/whisper/base")
        return f"whisper ran (bundled base: {bundled}), lang guess {info.language}"

    def _engine_embed():
        from . import ai, engine, models
        if not engine.server_binary():
            raise RuntimeError("llama-server not found in vendor/llama")
        r = models.resolve()
        if not r["embed"]:
            raise RuntimeError("bundled embedding model not found in vendor/models")
        v = ai.embed(["Cold iron is anathema to the fey.", "Frost nova cooldown is eight seconds."])
        q = ai.embed(["what hurts fey creatures?"], is_query=True)
        sims = (v @ q[0]).round(3).tolist()
        if v.shape[1] < 64 or sims[0] <= sims[1]:
            raise RuntimeError(f"embeddings look wrong: dim {v.shape[1]}, sims {sims}")
        return f"{engine.engine_version()}, {r['embed']['file']}, dim {v.shape[1]}, query sims {sims}"

    def _voice_and_captions():
        """Voiceprint model + fbank run, and libass renders captions with the bundled font."""
        import numpy as np
        import tempfile
        from . import voice
        from .config import ffmpeg_bin, popen_kwargs
        from .editing.clipexport import _fonts_dir, build_ass
        rng = np.random.default_rng(0)
        t = np.arange(32000) / 16000
        pcm = ((0.3 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t)) + 0.02 * rng.standard_normal(32000)) * 32767).astype(np.int16)
        e = voice.embed_segments(pcm, [(0.0, 2.0)])
        if e.shape[1] < 128 or abs(float(np.linalg.norm(e[0])) - 1) > 1e-3:
            raise RuntimeError(f"bad voiceprint {e.shape}")
        fonts = _fonts_dir()
        if not any(fonts.glob("*.ttf")):
            raise RuntimeError("caption font missing from assets/fonts")
        ass = build_ass([{"w": " Hello", "s": 0.1, "e": 0.6}, {"w": " world!", "s": 0.6, "e": 1.2}], 0, 2, 540, 960, True, "Test", "crop")
        d = fonts.parent
        (d / "selftest.ass").write_text(ass, "utf-8")
        out = Path(tempfile.gettempdir()) / "lk_selftest_caption.png"
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-y", "-f", "lavfi", "-i", "color=c=black:s=540x960:d=1",
                            "-vf", "subtitles=selftest.ass:fontsdir=fonts", "-frames:v", "1", "-update", "1", str(out)],
                           capture_output=True, text=True, cwd=str(d), **popen_kwargs())
        if r.returncode != 0:
            raise RuntimeError("caption render failed: " + r.stderr[-400:])
        from PIL import Image
        lit = np.asarray(Image.open(out).convert("L")).mean()
        if lit < 0.5:
            raise RuntimeError("captions rendered nothing (font not found by libass?)")
        import yt_dlp
        return f"voiceprint dim {e.shape[1]}, captions rendered (mean luma {lit:.1f}), yt-dlp {yt_dlp.version.__version__}"

    def _watermark():
        """drawtext (needs FFmpeg's libfreetype) draws the corner tag with the bundled font, where it should be."""
        import io
        import numpy as np
        from PIL import Image
        from .editing import watermark
        on = np.asarray(Image.open(io.BytesIO(watermark.preview("landscape", {"watermark_text": "tavernworks.dev",
                        "watermark_corner": "bottom-right", "watermark_size": "small"}))).convert("L")).astype(float)
        off = np.asarray(Image.open(io.BytesIO(watermark.preview("landscape", {"watermark_text": ""}))).convert("L")).astype(float)
        diff = np.abs(on - off)
        h, w = diff.shape
        corner, rest = diff[int(h * 0.85):, int(w * 0.7):].mean(), diff[: int(h * 0.7), : int(w * 0.6)].mean()
        if corner < 1.0 or rest > 0.5:
            raise RuntimeError(f"watermark not where expected (corner diff {corner:.2f}, elsewhere {rest:.2f})")
        return f"drawn bottom-right (corner diff {corner:.1f}, elsewhere {rest:.2f})"

    def _engine_chat():
        """Optional: LK_TEST_CHAT_MODEL / LK_TEST_MMPROJ point at a tiny VLM fetched only in CI."""
        model, proj = os.environ.get("LK_TEST_CHAT_MODEL"), os.environ.get("LK_TEST_MMPROJ")
        if not model:
            return "skipped (no LK_TEST_CHAT_MODEL)"
        import io
        from PIL import Image, ImageDraw
        from . import ai, engine
        from .config import update_settings
        update_settings({"local_chat_model": model, "provider": "builtin"})
        from . import models
        models.import_paths([p for p in (model, proj) if p])
        out = ai.chat_once([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=8)
        detail = f"text: {out[:40]!r}"
        if proj:
            im = Image.new("RGB", (320, 200), "white")
            ImageDraw.Draw(im).rectangle([40, 40, 280, 160], fill=(220, 30, 30))
            buf = io.BytesIO()
            im.save(buf, "JPEG")
            desc = ai.describe_image(buf.getvalue(), "What color is the shape? One word.")
            detail += f", vision: {desc[:60]!r}"
        info = engine.chat.info()
        engine.stop_all()
        if not out:
            raise RuntimeError("empty reply from chat model")
        return f"{detail}, backend {info['backend']}"

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

    for name, fn in (("imports", _imports), ("ffmpeg", _ffmpeg), ("whisper", _whisper), ("server", _server),
                     ("engine_embed", _engine_embed), ("voice_captions", _voice_and_captions), ("watermark", _watermark),
                     ("engine_chat", _engine_chat)):
        check(name, fn)
    _shutdown_engine()
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
    import atexit
    atexit.register(_shutdown_engine)
    from .server import TOKEN, boot
    threading.Thread(target=boot, daemon=True, name="boot").start()
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
        _shutdown_engine()
        return

    import webview
    api = DesktopAPI()
    window = webview.create_window(f"{APP_NAME}", url, js_api=api, width=1440, height=900,
                                   min_size=(1024, 680), background_color="#0B0D10", text_select=True)
    api.window = window
    window.events.loaded += lambda: _bind_native_drop(window)
    webview.start(private_mode=False, storage_path=str(LOG_DIR.parent / "webview"))
    server.should_exit = True
    _shutdown_engine()


if __name__ == "__main__":
    main()
