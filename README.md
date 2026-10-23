# Lorekeeper

A Windows desktop app that turns your videos, recordings, screenshots, PDFs, docs and web links into a library you can **ask questions about**, plus a built-in **audio/video editor**. Everything runs locally and offline. The AI engine ships inside the app: **llama.cpp** runs **Gemma 4** for answers and vision, **EmbeddingGemma 2** powers search, and **Whisper** transcribes. No Ollama, no servers, no accounts.

Every answer comes back in four parts: **The Answer**, **The How**, **The Why**, and a **Deep Cut**, with gold citation numbers you can click to jump to the exact page, passage or video timestamp.

## What it does

| Area | Features |
|---|---|
| **Library** | Drag-and-drop or pick files, paste links. Video, audio, images, PDF (scanned pages too), Word, PowerPoint, text/code, HTML, web pages |
| **Indexing** | Whisper transcripts with word timestamps, vision-model descriptions of images and video frames (incl. on-screen text), OCR for scanned PDFs, embeddings for semantic search |
| **Ask** | Hybrid search (BM25 keywords + vector meaning, merged with Reciprocal Rank Fusion), streamed answers, clickable sources, retrieval telemetry, follow-up questions, scope to selected items |
| **Studio** | Trim, split, speed, merge, volume, denoise, loudness normalize (EBU R128), cut dead air, convert (MP4/WebM/MOV/MKV/GIF/MP3/WAV/FLAC/M4A/Opus), extract audio. Each tool shows the exact FFmpeg command it will run |
| **Edit by transcript** | Strike words to cut them from the video, auto-strike fillers, find and strike, export SRT/VTT captions (re-timed to match your cuts) |
| **Timeline** | V1 main + V2 overlay (picture-in-picture: facecams, logos), two audio tracks, drag/trim/split/ripple delete, fades, volume, snapping, undo/redo, 16:9 / vertical / square canvases, draft or final renders |

## Getting the installer (GitHub Actions)

1. Create an empty GitHub repo (e.g. `Lorekeeper`) and push this folder to it:
   ```
   git remote add origin https://github.com/<you>/Lorekeeper.git
   git push -u origin main
   ```
2. Open the repo's **Actions** tab and wait for **Build Windows installer** (about 10–15 minutes). It downloads FFmpeg, llama.cpp (pinned release), EmbeddingGemma 2 and Whisper base, bundles everything with PyInstaller, and self-tests the frozen app: imports, FFmpeg, a real Whisper run, the local server, embeddings through the bundled engine, and a chat + vision round trip with a tiny test model. Then Inno Setup builds the installer.
3. Download `Lorekeeper-Setup-x.y.z.exe` from the run's **Artifacts**. Tag a commit `v1.0.0` and the workflow also publishes a GitHub Release.

The installer is per-user (no admin prompt) and installs to `%LOCALAPPDATA%\Programs\Lorekeeper`.

## First run

The installer asks which answer model you want (it picks the best fit for your RAM by default). The first time Lorekeeper opens it downloads that model once, with a progress screen, resumable if your connection drops, and SHA-256 verified. Search, transcription and editing work right away; questions work as soon as the model lands.

| Model | Download | Good for |
|---|---|---|
| Gemma 4 E4B | 4.8 GB | Best answers. 12 GB+ RAM, or a GPU with 6 GB+ |
| Gemma 4 E2B | 3.2 GB | Lighter and faster, same vision skills. Runs on 8 GB RAM or a 4 GB GPU |

Why isn't the model inside the installer? GitHub caps release files at 2 GB and the model alone is bigger than that. Everything else — the engine, the search model, Whisper, FFmpeg — is inside.

**Already have GGUF models?** Settings → AI → *Add a GGUF file…* uses them where they are (nothing is copied). Pick the model and its `mmproj-*.gguf` together to give it vision.

**GPU:** the engine is llama.cpp's Vulkan build, so it uses NVIDIA, AMD and Intel GPUs alike and falls back to the CPU when there's none. It measures free VRAM and offloads as many layers as fit. *Free memory when idle* (default 10 min) unloads the model so your games get the VRAM back; it reloads on the next question. Settings shows which device it's using and how many layers made it onto the GPU.

**Prefer Ollama?** Settings → AI → *Ollama server* switches every AI call to an Ollama server instead (this PC, or your AI box over Tailscale).

## Running from source

```
run_dev.bat              # desktop window
run_dev.bat --browser    # open in your normal browser instead
```
Needs Python 3.12. The script creates a venv, installs requirements, and fetches FFmpeg, llama.cpp, the search model and Whisper base into `vendor\`.

## Where things live

- `%LOCALAPPDATA%\Lorekeeper\` — library database, thumbnails/proxies, downloaded AI models (`models\gguf`), engine logs, settings
- `Videos\Lorekeeper Exports\` (changeable in Settings) — every render and caption file
- Files added with **Add files** or drag-and-drop in the desktop app are indexed where they are, not copied. Your originals are never modified.

## How it works (the short version)

```
file ─► extract ─► chunk (~900 chars, sentence-aligned, 150 overlap)
        │  docs: pypdf / python-docx / python-pptx / trafilatura
        │  scans: pdfium render ─► Gemma 4 vision OCR
        │  images: Gemma 4 vision description + visible text
        │  audio/video: FFmpeg ─► faster-whisper (word timestamps, VAD)
        │               keyframes (I-frame scene detection) ─► Gemma 4 vision
        ▼
SQLite: chunks + FTS5 (BM25)  +  EmbeddingGemma 2 vectors ("title: … | text: …")
        ▼
question ─► BM25 top 50 ┐
         └► cosine top 50┴► RRF fuse ─► top 8 (max 4 per file) ─► Gemma 4 with [S#] tags ─► streamed answer
```

The engine is two `llama-server` processes Lorekeeper starts and owns: one for the answer model plus its vision projector, one for embeddings (pinned to the CPU so the GPU stays free). They listen on random localhost ports behind a per-launch API key and sit in a Windows Job Object, so they die with the app even if it crashes.

Editing is FFmpeg all the way down. Transcript and dead-air cuts use `select`/`aselect` (a streaming filter, so 3-hour VODs don't blow up RAM) after forcing constant frame rate so audio can't drift. The timeline compiles to a single `filter_complex`: each clip is input-seeked, conformed, alpha-faded, shifted with `setpts` and stacked with `overlay`; audio is placed with `adelay`, summed with `amix normalize=0` and caught by a limiter.

## Project layout

```
app/            Python backend (FastAPI on 127.0.0.1, token-locked)
  ingest/       extraction, Whisper, keyframes, chunking, pipeline
  editing/      FFmpeg recipes, transcript cuts, captions, timeline renderer
  engine.py     llama-server process manager    models.py  catalog, downloads, GGUF imports
  ai.py         one front door: built-in engine or Ollama      gguf.py  GGUF header reader
  search.py     hybrid retrieval      qa.py   answer prompt + streaming
web/            UI (vanilla JS modules, no build step)
packaging/      PyInstaller spec, Inno Setup script, FFmpeg + AI component fetchers, icon
.github/        Windows build workflow
```
