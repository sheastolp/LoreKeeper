# PyInstaller spec — builds dist/Lorekeeper/Lorekeeper.exe (one-folder, windowed).
# Run from the repo root:  pyinstaller packaging/lorekeeper.spec --noconfirm --clean
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas, binaries, hiddenimports = [], [], []
# Packages that ship DLLs, model assets or data files PyInstaller can't discover by import tracing.
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers", "huggingface_hub",
            "trafilatura", "justext", "courlan", "htmldate", "dateparser", "lxml",
            "pptx", "docx", "pypdfium2", "pypdfium2_raw", "webview", "clr_loader", "pythonnet",
            "kaldi_native_fbank", "yt_dlp"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # optional package missing on this machine
        print(f"[spec] skipping {pkg}: {exc}")

hiddenimports += collect_submodules("uvicorn") + collect_submodules("app")
datas += [(os.path.join(ROOT, "web"), "web"), (os.path.join(ROOT, "assets"), "assets")]
# bundled binaries + models: ffmpeg, llama.cpp's llama-server, EmbeddingGemma 2, Whisper base, CAM++ voiceprints
for sub in ("ffmpeg", "llama", "models", os.path.join("whisper", "base"), "voice"):
    src = os.path.join(ROOT, "vendor", sub)
    if os.path.isdir(src):
        datas.append((src, os.path.join("vendor", sub)))
    else:
        print(f"[spec] WARNING: vendor/{sub} missing - run packaging/fetch_vendor.py")

a = Analysis(
    [os.path.join(ROOT, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "torch", "tensorflow", "scipy", "pandas", "sympy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Lorekeeper",
    icon=os.path.join(SPECPATH, "lorekeeper.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Lorekeeper", upx=False)
