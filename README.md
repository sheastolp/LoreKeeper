# Lorekeeper

A Windows desktop app that turns your videos, recordings, screenshots, PDFs, docs and web links into a library you can **ask questions about**, plus a built-in **audio/video editor**. Everything runs locally: Whisper transcribes on your PC, and your own Ollama server does the thinking.

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
2. Open the repo's **Actions** tab and wait for **Build Windows installer** (about 10 minutes). It downloads FFmpeg, bundles everything with PyInstaller, runs a self-test on the frozen app (imports, FFmpeg, a real Whisper run, the local server), then builds the installer with Inno Setup.
3. Download `Lorekeeper-Setup-x.y.z.exe` from the run's **Artifacts**. Tag a commit `v1.0.0` and the workflow also publishes a GitHub Release.

The installer is per-user (no admin prompt) and installs to `%LOCALAPPDATA%\Programs\Lorekeeper`.

## First run

1. Make sure Ollama is reachable. In **Settings**, set the server address: `http://127.0.0.1:11434` for this PC, or your AI box over Tailscale, e.g. `http://100.x.x.x:11434`. On the Ollama box, set `OLLAMA_HOST=0.0.0.0` so it accepts connections from other machines.
2. In **Settings → Models**, download what's missing:
   - `llama3.2:3b` for answers (or `qwen2.5:7b` if you have 8 GB+ VRAM)
   - `gemma3:4b` for images, video frames and scanned pages
   - `nomic-embed-text` for semantic search
3. Drop some files into the Library. The first transcription downloads the Whisper model (base, ~145 MB) once.

Thinking models (qwen3, deepseek-r1) work too: the app requests `think: false` and strips any `<think>` blocks that leak through anyway.

## Running from source

```
run_dev.bat              # desktop window
run_dev.bat --browser    # open in your normal browser instead
```
Needs Python 3.12. The script creates a venv, installs requirements and downloads FFmpeg into `vendor\ffmpeg`.

## Where things live

- `%LOCALAPPDATA%\Lorekeeper\` — library database, thumbnails/proxies, Whisper models, logs, settings
- `Videos\Lorekeeper Exports\` (changeable in Settings) — every render and caption file
- Files added with **Add files** or drag-and-drop in the desktop app are indexed where they are, not copied. Your originals are never modified.

## How it works (the short version)

```
file ─► extract ─► chunk (~900 chars, sentence-aligned, 150 overlap)
        │  docs: pypdf / python-docx / python-pptx / trafilatura
        │  scans: pdfium render ─► vision model OCR
        │  images: vision model description + visible text
        │  audio/video: FFmpeg ─► faster-whisper (word timestamps, VAD)
        │               keyframes (I-frame scene detection) ─► vision model
        ▼
SQLite: chunks + FTS5 (BM25)  +  embedding vectors (nomic-embed-text)
        ▼
question ─► BM25 top 50 ┐
         └► cosine top 50┴► RRF fuse ─► top 8 (max 4 per file) ─► LLM with [S#] tags ─► streamed answer
```

Editing is FFmpeg all the way down. Transcript and dead-air cuts use `select`/`aselect` (a streaming filter, so 3-hour VODs don't blow up RAM) after forcing constant frame rate so audio can't drift. The timeline compiles to a single `filter_complex`: each clip is input-seeked, conformed, alpha-faded, shifted with `setpts` and stacked with `overlay`; audio is placed with `adelay`, summed with `amix normalize=0` and caught by a limiter.

## Project layout

```
app/            Python backend (FastAPI on 127.0.0.1, token-locked)
  ingest/       extraction, Whisper, keyframes, chunking, pipeline
  editing/      FFmpeg recipes, transcript cuts, captions, timeline renderer
  search.py     hybrid retrieval      qa.py   answer prompt + streaming
web/            UI (vanilla JS modules, no build step)
packaging/      PyInstaller spec, Inno Setup script, FFmpeg fetcher, icon
.github/        Windows build workflow
```
