"""Fetch the bundled AI pieces into vendor/ (used by CI and by run_dev.bat).

  vendor/llama/      llama.cpp's llama-server, pinned release (Windows: Vulkan build, which
                     also carries every CPU backend, so it runs on any GPU or none at all)
  vendor/models/     EmbeddingGemma 2 (search), SHA-256 verified
  vendor/whisper/base/  Whisper base for faster-whisper (transcription, offline from day one)

  python packaging/fetch_vendor.py [--test-models DIR]

--test-models also fetches a tiny vision model (SmolVLM2 256M) used only by the CI self-test
to prove chat + vision run on the Windows build. It is not bundled.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

LLAMA_TAG = "b11810"
ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"

EMBED = ("ggml-org/embeddinggemma-2-GGUF", "embeddinggemma-2-Q8_0.gguf", 309855456,
         "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135")
TEST_MODELS = [
    ("ggml-org/SmolVLM2-256M-Video-Instruct-GGUF", "SmolVLM2-256M-Video-Instruct-Q8_0.gguf", 175056256,
     "1202d1c54493bddff5b0ecbc36fcb7520ff720b9fa3d7224aeb293581f90529a"),
    ("ggml-org/SmolVLM2-256M-Video-Instruct-GGUF", "mmproj-SmolVLM2-256M-Video-Instruct-Q8_0.gguf", 103771616,
     "05d5751132244a6ebd64cba9b34898c0d874b2cb78159d758e1d4da3aad91581"),
]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lorekeeper-build"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def download(url: str, dest: Path, size: int | None = None, sha256: str | None = None) -> None:
    if dest.exists() and (size is None or dest.stat().st_size == size):
        print(f"  have {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  get  {url}")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "lorekeeper-build"})
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as fh:
        while chunk := r.read(1 << 20):
            fh.write(chunk)
            h.update(chunk)
    if size is not None and tmp.stat().st_size != size:
        raise SystemExit(f"{dest.name}: expected {size} bytes, got {tmp.stat().st_size}")
    if sha256 and h.hexdigest() != sha256:
        raise SystemExit(f"{dest.name}: SHA-256 mismatch ({h.hexdigest()})")
    os.replace(tmp, dest)


def fetch_llama() -> None:
    out = VENDOR / "llama"
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    if (out / exe).exists() and (out / "VERSION").exists() and (out / "VERSION").read_text().strip() == LLAMA_TAG:
        print(f"  have llama.cpp {LLAMA_TAG}")
        return
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    base = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}"
    if os.name == "nt":
        url = f"{base}/llama-{LLAMA_TAG}-bin-win-vulkan-x64.zip"
        print(f"  get  {url}")
        with zipfile.ZipFile(io.BytesIO(fetch(url))) as z:
            for info in z.infolist():
                name = Path(info.filename).name
                # the server plus every runtime library; skip the other CLI tools
                if name == "llama-server.exe" or name.lower().endswith(".dll") or name.upper().startswith("LICENSE"):
                    (out / name).write_bytes(z.read(info))
    else:
        url = f"{base}/llama-{LLAMA_TAG}-bin-ubuntu-x64.tar.gz"
        print(f"  get  {url}")
        with tarfile.open(fileobj=io.BytesIO(fetch(url)), mode="r:gz") as t:
            for m in t.getmembers():
                name = Path(m.name).name
                if not (name == "llama-server" or ".so" in name or name.upper().startswith("LICENSE")):
                    continue
                if m.issym():
                    target = out / Path(m.linkname).name
                    link = out / name
                    if not link.exists():
                        link.symlink_to(target.name)
                elif m.isfile():
                    data = t.extractfile(m).read()
                    (out / name).write_bytes(data)
                    (out / name).chmod(0o755)
    (out / "VERSION").write_text(LLAMA_TAG)
    print(f"  llama.cpp {LLAMA_TAG}: {sum(p.stat().st_size for p in out.iterdir() if p.is_file()) / 1e6:.0f} MB")


def fetch_embed() -> None:
    repo, name, size, sha = EMBED
    download(f"https://huggingface.co/{repo}/resolve/main/{name}", VENDOR / "models" / name, size, sha)


def fetch_whisper() -> None:
    out = VENDOR / "whisper" / "base"
    if (out / "model.bin").exists():
        print("  have whisper base")
        return
    from faster_whisper.utils import download_model
    download_model("base", output_dir=str(out))
    print("  whisper base:", ", ".join(p.name for p in out.iterdir()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-models", help="also fetch the tiny CI test VLM into this folder")
    ap.add_argument("--skip-whisper", action="store_true")
    a = ap.parse_args()
    print("Fetching bundled AI components into", VENDOR)
    fetch_llama()
    fetch_embed()
    if not a.skip_whisper:
        fetch_whisper()
    if a.test_models:
        d = Path(a.test_models)
        for repo, name, size, sha in TEST_MODELS:
            download(f"https://huggingface.co/{repo}/resolve/main/{name}", d / name, size, sha)
    print("done")


if __name__ == "__main__":
    sys.exit(main())
