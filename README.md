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
| **Clips** | Pick a person; Lorekeeper finds them in your VODs **by voice**, scores every moment they're in (talk share, loud reactions, pace, laughs), and the local AI picks, titles and tightens the best ones. Review, nudge, rename, then export each clip as 16:9 or vertical 9:16 (crop with a focus slider, or whole frame over a blurred fill) with word-by-word burned-in captions. Pulls VODs straight from a Twitch channel, whole or just a time range |
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

## Clips of a specific person

1. **Get VODs in.** Clips → *Import from Twitch*: type a channel name (or paste a VOD link), tick past broadcasts, optionally give a time range like `1:30:00` to `2:15:00` so you only download that part. Or add recordings/OBS files in the Library. Every audio/video item is transcribed and voice-indexed automatically.
2. **Teach it the person.** Clips → People → *Add*. Pick a VOD they're in and hit *Find the voices*: Lorekeeper groups all the talking by voice, you play a couple of samples and click *This is them*. Or tick a few of their lines in the transcript. *Teach more* adds examples later (a different mic or a different VOD makes it sharper).
3. **Find clips.** Pick the VODs, a clip length (short 12–30 s, medium 25–60 s, long 50–120 s) and how many per VOD, then *Find clips*. The gold strip under each VOD shows where they're talking.
4. **Review and export.** Arrow keys step through clips. Trim with ±1 s or *Here*, rename, then export 16:9 or vertical. *Keep* and *Dismiss* sort the pile.

How it recognises voices: Whisper's word timestamps mark where people talk; every 2-second stretch becomes a 512-number voiceprint from the CAM++ speaker model (3D-Speaker, runs on the CPU in milliseconds). Stream audio makes everyone sound alike (game sound and music sit under every voice), so matching subtracts the average voiceprint of the stretches that sound *least* like the person, then lets the most confident matches in each VOD sharpen the person's voiceprint for that VOD. On a test stream built from real recorded speech over game noise and music, that cut wrong matches from about 1 in 5 to about 1 in 50. *Sensitivity* in a person's settings trades missed moments for wrong ones.

How it picks highlights: clip-length windows where the person does at least a fifth of the talking are scored on their share of the talking, loudness spikes against the VOD's own baseline, speech pace, and excitement in the words. The best go to the local model with a speaker-labelled transcript; it rates each 1–10, writes a Shorts-style title, and picks the tightest setup-to-payoff span. With no model installed it still works, ranking by the sound signals and titling from the transcript.

Clips are saved in the exports folder as `<Person> - <Title> - vertical|16x9 <time>.mp4`.

## Watermark

Every finished video gets a small `tavernworks.dev` tag in one corner so viewers know where to find you. It's a signpost, not copy protection, so it stays small (about 2.6% of the frame's short side: 28 px on 1080p) with a soft shadow that keeps it readable on bright or dark footage.

- **Where:** Settings → Watermark sets the text, the corner (default bottom-right) and the size (tiny / small / medium), with live previews of a 16:9 render and a vertical clip. Click a preview to see it bigger.
- **Vertical clips** always put it in the top band on the chosen side, because the bottom fifth of a TikTok / Shorts / Reels video is covered by captions and buttons.
- **What gets it:** clip exports, trims, splits, merges, speed changes, volume / denoise / loudness fixes, dead-air and transcript cuts, conversions (including GIFs) and timeline renders. Audio-only exports never do.
- **Trade-off:** a watermark can only be drawn on decoded frames, so while it's on, edits that used to copy the video untouched (Instant trim/split, volume, denoise, normalize) re-encode it once at high quality. Switch the watermark off for instant stream-copy cuts.

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
  editing/      FFmpeg recipes, transcript cuts, captions, corner watermark, timeline renderer
  engine.py     llama-server process manager    models.py  catalog, downloads, GGUF imports
  voice.py      voiceprints, voice discovery    clips.py   highlight finder    twitch.py  VOD import
  ai.py         one front door: built-in engine or Ollama      gguf.py  GGUF header reader
  search.py     hybrid retrieval      qa.py   answer prompt + streaming
web/            UI (vanilla JS modules, no build step)
packaging/      PyInstaller spec, Inno Setup script, FFmpeg + AI component fetchers, icon
.github/        Windows build workflow
```
