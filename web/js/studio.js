import { bus, clear, debounce, fmtTime, get, h, icon, jobCard, parseTime, post, runJob, sourceById, state, toast } from "./core.js";

let root, picker, monitor, video, scrub, timeEl, inEl, outEl, pane, side, modeSeg, playBtn;
let src = null;               // current source
let mode = "tools";
let tool = "trim";
let inPt = 0, outPt = 0;
const params = {};            // per tool
const transcripts = {};       // id -> {words, segments}
const deletions = {};         // id -> Set(word index)
let picked = new Set();
let anchor = null;
let previewCuts = true;
let mergeList = [];
let findText = "";

const FILLERS = new Set(["um", "uh", "erm", "er", "ah", "hmm", "mm", "mhm", "uhh", "umm", "eh"]);

// copied (stream-copy) video can't be drawn on, so instant cuts aren't offered while the watermark is on
const wmBlocksCopy = () => !!(state.status?.settings?.watermark && state.status?.settings?.watermark_text && src?.meta?.has_video);
const MODE = { k: "precise", label: "Mode", type: "seg", def: true, options: [[true, "Frame-accurate"], [false, "Instant (stream copy)"]], when: () => !wmBlocksCopy() };
const WM_NOTE = "Cuts are frame-accurate re-encodes while the watermark is on: instant (stream copy) cuts can't have the tag drawn on them. Turn the watermark off in Settings for instant cuts.";

const TOOLS = [
  { group: "Cut", id: "trim", icon: "in", name: "Trim", lede: "Keep only the stretch between your in and out points (I and O keys).",
    params: [MODE],
    build: (p) => ({ source_id: src.id, start: inPt, end: outPt, precise: wmBlocksCopy() || p.precise }) },
  { id: "split", icon: "split", name: "Split", lede: "Cut the file into two files at the playhead.",
    params: [MODE],
    build: (p) => ({ source_id: src.id, at: video.currentTime, precise: wmBlocksCopy() || p.precise }) },
  { id: "speed", icon: "speed", name: "Speed", lede: "Speed up or slow down. Voices keep their natural pitch.",
    params: [{ k: "factor", label: "Speed", type: "select", def: 1.5, options: [[0.5, "0.5×"], [0.75, "0.75×"], [1.25, "1.25×"], [1.5, "1.5×"], [2, "2×"], [3, "3×"], [4, "4×"]] }],
    build: (p) => ({ source_id: src.id, factor: p.factor }) },
  { id: "merge", icon: "merge", name: "Merge", lede: "Join several clips end to end into one file. Different sizes and frame rates are conformed automatically.",
    params: [], build: () => ({ source_ids: mergeList }) },
  { group: "Sound", id: "volume", icon: "vol", name: "Volume", lede: "Make it louder or quieter. A limiter stops boosted peaks from clipping.",
    params: [{ k: "gain_db", label: "Gain", type: "range", def: 6, min: -20, max: 20, step: 1, unit: " dB" }],
    build: (p) => ({ source_id: src.id, gain_db: p.gain_db }) },
  { id: "denoise", icon: "noise", name: "Denoise", lede: "Remove fan hum, hiss and room noise behind voices.",
    params: [{ k: "strength", label: "Strength", type: "seg", def: "medium", options: [["light", "Light"], ["medium", "Medium"], ["strong", "Strong"]] }],
    build: (p) => ({ source_id: src.id, strength: p.strength }) },
  { id: "normalize", icon: "level", name: "Normalize loudness", lede: "Match the loudness standard of where you'll publish.",
    params: [{ k: "target", label: "Target", type: "seg", def: "streaming", options: [["streaming", "YouTube / Twitch −14"], ["podcast", "Podcast −16"], ["broadcast", "Broadcast −23"]] }],
    build: (p) => ({ source_id: src.id, target: p.target }) },
  { id: "cut_silences", icon: "silence", name: "Cut dead air", lede: "Find long silences and cut them out automatically. Great for raw VODs.",
    params: [
      { k: "threshold_db", label: "Silence below", type: "range", def: -35, min: -60, max: -20, step: 1, unit: " dB" },
      { k: "min_silence", label: "Longer than", type: "select", def: 0.8, options: [[0.4, "0.4 s"], [0.6, "0.6 s"], [0.8, "0.8 s"], [1.2, "1.2 s"], [2, "2 s"], [3, "3 s"]] },
      { k: "padding", label: "Keep around words", type: "select", def: 0.15, options: [[0.05, "0.05 s"], [0.1, "0.1 s"], [0.15, "0.15 s"], [0.25, "0.25 s"], [0.4, "0.4 s"]] }],
    build: (p) => ({ source_id: src.id, ...p }) },
  { group: "Convert", id: "convert", icon: "convert", name: "Convert", lede: "Re-encode to another format, size or quality.",
    params: [
      { k: "format", label: "Format", type: "select", def: "mp4", options: () => (src?.meta?.has_video
        ? [["mp4", "MP4 (H.264)"], ["webm", "WebM (VP9)"], ["mov", "MOV"], ["mkv", "MKV"], ["gif", "GIF"], ["mp3", "MP3 audio"], ["wav", "WAV audio"], ["flac", "FLAC audio"], ["m4a", "M4A audio"], ["ogg", "Opus audio"]]
        : [["mp3", "MP3"], ["wav", "WAV"], ["flac", "FLAC"], ["m4a", "M4A"], ["ogg", "Opus"]]) },
      { k: "height", label: "Resolution", type: "select", def: 0, options: [[0, "Keep original"], [1080, "1080p"], [720, "720p"], [480, "480p"], [360, "360p"]], when: () => src?.meta?.has_video },
      { k: "quality", label: "Quality", type: "seg", def: "balanced", options: [["high", "High"], ["balanced", "Balanced"], ["small", "Small file"]], when: () => src?.meta?.has_video }],
    build: (p) => ({ source_id: src.id, ...p }) },
  { id: "extract_audio", icon: "extract", name: "Extract audio", lede: "Save just the soundtrack.",
    params: [{ k: "format", label: "Format", type: "select", def: "mp3", options: [["mp3", "MP3"], ["wav", "WAV"], ["flac", "FLAC"], ["m4a", "M4A"], ["ogg", "Opus"]] }],
    build: (p) => ({ source_id: src.id, format: p.format }) },
];
const toolById = (id) => TOOLS.find((t) => t.id === id);

export function mount(el) {
  root = el;
  picker = h("select.input", { "aria-label": "Clip to edit", style: { minWidth: "260px" }, onchange: (e) => load(e.target.value) });
  video = h("video", { preload: "auto" });
  monitor = h("div.monitor", video, h("div.placeholder", "Pick a clip to edit"));
  scrub = h("div.scrub", { "aria-label": "Seek" });
  playBtn = h("button.btn.icon", { onclick: togglePlay, "aria-label": "Play or pause" }, icon("play"));
  timeEl = h("span.time");
  inEl = h("input.input", { "aria-label": "In point", onchange: () => { inPt = clampT(parseTime(inEl.value)); syncInOut(); } });
  outEl = h("input.input", { "aria-label": "Out point", onchange: () => { outPt = clampT(parseTime(outEl.value)); syncInOut(); } });
  modeSeg = h("div.seg", { role: "group" },
    h("button", { "aria-pressed": "true", "data-m": "tools", onclick: () => setMode("tools") }, "Tools"),
    h("button", { "aria-pressed": "false", "data-m": "transcript", onclick: () => setMode("transcript") }, "Edit by transcript"));
  pane = h("div.studio-pane");
  side = h("aside.side");

  el.append(
    h("header.view-head", h("h1", "Studio"), h("span.sub", "Quick edits, cleanup and transcript cuts. Originals are never modified."),
      h("div.grow"), picker),
    h("div.studio-body",
      h("div.studio-main",
        h("div.stage", monitor, scrub,
          h("div.transport", playBtn, timeEl,
            h("div.inout#studio-inout",
              h("button.btn.sm", { onclick: () => { inPt = video.currentTime; syncInOut(); }, title: "Set in point (I)" }, icon("in"), "In"), inEl,
              h("button.btn.sm", { onclick: () => { outPt = video.currentTime; syncInOut(); }, title: "Set out point (O)" }, icon("out"), "Out"), outEl,
              h("button.btn.ghost.sm", { onclick: () => { inPt = 0; outPt = src?.duration || 0; syncInOut(); } }, "Reset")),
            h("div.grow"), h("span.muted", { style: { fontSize: "12px" } }, h("span.kbd", "Space"), " play  ", h("span.kbd", "I"), " ", h("span.kbd", "O"), " in/out  ", h("span.kbd", "←"), h("span.kbd", "→"), " nudge"))),
        h("div.studio-modes", modeSeg),
        pane),
      side));

  video.addEventListener("timeupdate", drawScrub);
  video.addEventListener("play", () => { clear(playBtn, icon("pause")); tick(); });
  video.addEventListener("pause", () => clear(playBtn, icon("play")));
  video.addEventListener("loadedmetadata", () => { if (!outPt) outPt = video.duration; syncInOut(); });
  scrubDrag();
  document.addEventListener("keydown", onKey);
  bus.on("sources", fillPicker);
  bus.on("jobs", renderSide);
  fillPicker();
  renderPane();
  renderSide();
}

export function show(arg) {
  fillPicker();
  if (arg && arg.source) { picker.value = arg.source; load(arg.source); }
}
export function hide() { video.pause(); }

let pickerKey = "";
function fillPicker() {
  const list = state.sources.filter((s) => (s.kind === "video" || s.kind === "audio") && s.status === "ready");
  const key = list.map((s) => s.id + s.title).join("|");
  if (key === pickerKey) return;
  pickerKey = key;
  const cur = picker.value;
  clear(picker, h("option", { value: "" }, list.length ? "Choose a clip…" : "No audio or video in the library yet"),
    list.map((s) => h("option", { value: s.id }, `${s.title}  (${fmtTime(s.duration)})`)));
  picker.value = src ? src.id : cur;
  if (!src && list.length && !cur) { picker.value = list[0].id; load(list[0].id); }
}

function load(id) {
  const s = sourceById(id);
  if (!s) return;
  src = s;
  video.src = `/media/${s.id}`;
  inPt = 0; outPt = s.duration || 0;
  picked = new Set(); anchor = null;
  monitor.classList.toggle("audio", !s.meta.has_video);
  monitor.querySelector(".placeholder")?.remove();
  monitor.querySelector(".wave")?.remove();
  if (!s.meta.has_video) monitor.prepend(h("div.wave", { style: { backgroundImage: `url(/waveform/${s.id})` } }));
  if (!mergeList.length || !mergeList.includes(s.id)) mergeList = [s.id];
  syncInOut();
  renderPane();
}

function clampT(t) { return isNaN(t) ? 0 : Math.max(0, Math.min(src?.duration || 0, t)); }
function syncInOut() {
  if (outPt < inPt) [inPt, outPt] = [outPt, inPt];
  inEl.value = fmtTime(inPt, true);
  outEl.value = fmtTime(outPt, true);
  drawScrub();
  if (mode === "tools" && (tool === "trim")) dryRun();
}

function togglePlay() { if (!src) return; video.paused ? video.play() : video.pause(); }

function tick() {
  if (video.paused) return;
  drawScrub();
  if (mode === "transcript") {
    if (previewCuts) {
      const t = video.currentTime;
      for (const [a, b] of removalRanges()) if (t >= a && t < b - 0.02) { video.currentTime = b; break; }
    }
    highlightWord();
  }
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- scrub bar
function drawScrub() {
  if (!src) { clear(scrub); return; }
  const D = src.duration || video.duration || 1;
  const pct = (t) => `${(t / D) * 100}%`;
  const kids = [h("div.track")];
  if (mode === "tools" && tool === "trim") kids.push(h("div.sel", { style: { left: pct(inPt), width: pct(Math.max(0, outPt - inPt)) } }));
  else for (const [a, b] of removalRanges()) kids.push(h("div.cut", { style: { left: pct(a), width: pct(b - a) } }));
  kids.push(h("div.head", { style: { left: pct(video.currentTime || 0) } }));
  clear(scrub, kids);
  timeEl.textContent = `${fmtTime(video.currentTime || 0, true)} / ${fmtTime(D, true)}`;
}

function scrubDrag() {
  const seekTo = (e) => {
    if (!src) return;
    const r = scrub.getBoundingClientRect();
    video.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (src.duration || video.duration);
    drawScrub(); highlightWord();
  };
  scrub.addEventListener("pointerdown", (e) => {
    scrub.setPointerCapture(e.pointerId); seekTo(e);
    const move = (ev) => seekTo(ev);
    scrub.addEventListener("pointermove", move);
    scrub.addEventListener("pointerup", () => scrub.removeEventListener("pointermove", move), { once: true });
  });
}

function onKey(e) {
  if (root.hidden || !src) return;
  if (e.target.closest("input, textarea, select")) return;
  const k = e.key;
  if (k === " ") { e.preventDefault(); togglePlay(); }
  else if (k === "i" || k === "I") { inPt = video.currentTime; syncInOut(); }
  else if (k === "o" || k === "O") { outPt = video.currentTime; syncInOut(); }
  else if (k === "ArrowLeft" || k === "ArrowRight") {
    e.preventDefault();
    const step = e.shiftKey ? 5 : (src.meta.fps ? 1 / src.meta.fps : 0.1) * (e.altKey ? 1 : 10);
    video.currentTime = clampT(video.currentTime + (k === "ArrowLeft" ? -step : step));
    drawScrub(); highlightWord();
  } else if (mode === "transcript" && (k === "Delete" || k === "Backspace")) { e.preventDefault(); strikePicked(); }
  else if (mode === "transcript" && k === "Escape") { picked.clear(); paintWords(); }
}

function setMode(m) {
  mode = m;
  modeSeg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.m === m)));
  root.querySelector("#studio-inout").hidden = m !== "tools";
  renderPane(); drawScrub();
}

// ---------------------------------------------------------------- tools mode
function renderPane() {
  if (!src) {
    clear(pane, h("div.empty", h("h2", "Nothing to edit yet"),
      "Add a video or audio file in the Library. Once it's indexed it shows up in the picker above."));
    return;
  }
  if (mode === "transcript") return renderTranscript();
  const list = h("nav.toollist", { "aria-label": "Tools" });
  for (const t of TOOLS) {
    if (t.group) list.append(h("h4", t.group));
    list.append(h("button", { "aria-pressed": String(t.id === tool), onclick: () => { tool = t.id; renderPane(); drawScrub(); } }, icon(t.icon), t.name));
  }
  clear(pane, h("div.tools", list, toolPanel()));
  dryRun();
}

function pval(t, p) {
  params[t.id] ||= {};
  if (!(p.k in params[t.id])) params[t.id][p.k] = p.def;
  return params[t.id][p.k];
}

function toolPanel() {
  const t = toolById(tool);
  const set = (k, v) => { params[t.id][k] = v; dryRun(); };
  const fields = [];
  for (const p of t.params) {
    if (p.when && !p.when()) continue;
    const v = pval(t, p);
    const opts = typeof p.options === "function" ? p.options() : p.options;
    if (opts && !opts.some(([k]) => k === v)) params[t.id][p.k] = opts[0][0];
    let ctl;
    if (p.type === "seg") {
      ctl = h("div.seg", opts.map(([k, label]) => h("button", { "aria-pressed": String(k === params[t.id][p.k]),
        onclick: (e) => { e.currentTarget.parentNode.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false"));
          e.currentTarget.setAttribute("aria-pressed", "true"); set(p.k, k); } }, label)));
    } else if (p.type === "select") {
      ctl = h("select.input", { onchange: (e) => set(p.k, opts[e.target.selectedIndex][0]) },
        opts.map(([k, label]) => h("option", { selected: k === params[t.id][p.k] }, label)));
    } else if (p.type === "range") {
      const out = h("output", `${v}${p.unit}`);
      ctl = h("div.rv", { style: { display: "flex", gap: "10px", alignItems: "center", width: "260px" } },
        h("input", { type: "range", min: p.min, max: p.max, step: p.step, value: v, style: { flex: 1 },
          oninput: (e) => { out.textContent = `${e.target.value}${p.unit}`; },
          onchange: (e) => set(p.k, Number(e.target.value)) }), out);
    }
    fields.push(h("label.field", h("span", p.label), ctl));
  }

  let context = null;
  if (t.id === "trim") context = h("p.muted", `Keeping ${fmtTime(inPt, true)} to ${fmtTime(outPt, true)}, ${(outPt - inPt).toFixed(2)} s. Set the range with the In and Out buttons or the I and O keys.`);
  if (t.id === "split") context = h("p.muted", "Move the playhead to where the cut should go, then render.");
  if ((t.id === "trim" || t.id === "split") && wmBlocksCopy()) context = [context, h("p.muted", WM_NOTE)];
  if (t.id === "merge") context = mergeUI();

  const hood = h("details.hood", { open: true }, h("summary", icon("chev"), "Under the hood"),
    h("div.explain", "…"), h("pre", "…"));
  return h("div.toolpanel", h("h2", t.name), h("p.lede", t.lede), context, h("div.params", fields), hood,
    h("button.btn.primary", { onclick: () => render(t) }, `Render ${t.name.toLowerCase()}`));
}

function mergeUI() {
  const avail = state.sources.filter((s) => (s.kind === "video" || s.kind === "audio") && s.status === "ready");
  const move = (i, d) => { const j = i + d; if (j < 0 || j >= mergeList.length) return; [mergeList[i], mergeList[j]] = [mergeList[j], mergeList[i]]; renderPane(); };
  const add = h("select.input", { onchange: (e) => { if (e.target.value) { mergeList.push(e.target.value); renderPane(); } } },
    h("option", { value: "" }, "Add a clip…"), avail.map((s) => h("option", { value: s.id }, s.title)));
  return h("div.mergelist",
    mergeList.map((id, i) => {
      const s = sourceById(id);
      return h("div.mi", h("b", { style: { width: "18px", color: "var(--vellum-dim)" } }, i + 1), h("span", s ? s.title : "(removed)"),
        h("span.muted", { style: { flex: "none" } }, s ? fmtTime(s.duration) : ""),
        h("button.btn.ghost.sm", { onclick: () => move(i, -1), disabled: i === 0, "aria-label": "Move up" }, "↑"),
        h("button.btn.ghost.sm", { onclick: () => move(i, 1), disabled: i === mergeList.length - 1, "aria-label": "Move down" }, "↓"),
        h("button.btn.ghost.sm.icon", { onclick: () => { mergeList.splice(i, 1); renderPane(); }, "aria-label": "Remove" }, icon("x")));
    }), add);
}

const dryRun = debounce(async () => {
  if (!src || mode !== "tools") return;
  const t = toolById(tool);
  const hood = pane.querySelector(".hood");
  if (!hood) return;
  for (const p of t.params) pval(t, p);
  try {
    const r = await post(`/api/edit/${t.id}`, { params: t.build(params[t.id]), dry: true });
    hood.querySelector(".explain").textContent = r.explain;
    hood.querySelector("pre").textContent = r.commands.join("\n\n");
  } catch (e) {
    hood.querySelector(".explain").textContent = e.message;
    hood.querySelector("pre").textContent = "";
  }
}, 250);

async function render(t) {
  for (const p of t.params) pval(t, p);
  try { await runJob(t.id, t.build(params[t.id]), `${t.name}: ${src.title}`); } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- transcript mode
async function renderTranscript() {
  if (!src.meta.has_audio) { clear(pane, h("div.empty", h("h2", "No soundtrack"), "This file has no audio, so there's nothing to transcribe.")); return; }
  if (!transcripts[src.id]) {
    clear(pane, h("p.muted", "Loading transcript…"));
    try { transcripts[src.id] = await get(`/api/sources/${src.id}/transcript`); }
    catch { clear(pane, h("div.empty", h("h2", "No transcript yet"), "Re-index this clip from the Library to transcribe it.")); return; }
  }
  deletions[src.id] ||= new Set();
  const tr = transcripts[src.id];
  if (!tr.words.length) { clear(pane, h("div.empty", h("h2", "No speech found"), "Whisper didn't detect any words in this clip.")); return; }

  const stats = h("span.stat");
  const find = h("input.input", { placeholder: "Find words", value: findText, style: { width: "180px" },
    oninput: (e) => { findText = e.target.value.toLowerCase().trim(); paintWords(); } });
  const bar = h("div.tr-bar",
    h("button.btn.sm", { onclick: strikeFillers, title: "Strike um, uh, erm, hmm…" }, "Strike fillers"),
    find,
    h("button.btn.sm", { onclick: strikeFound }, "Strike matches"),
    h("button.btn.ghost.sm", { onclick: () => { deletions[src.id].clear(); paintWords(); } }, "Restore all"),
    h("label.check", { style: { fontSize: "13px", alignItems: "center" } },
      h("input", { type: "checkbox", checked: previewCuts, onchange: (e) => { previewCuts = e.target.checked; } }), "Skip cuts while playing"),
    h("div.grow"), stats);
  const words = h("div.words");
  let seg = 0;
  let para = null;
  tr.words.forEach((w, i) => {
    while (seg + 1 < tr.segments.length && w.s >= tr.segments[seg + 1].start - 0.01) { seg++; para = null; }
    if (!para) {
      const start = tr.segments[seg] ? tr.segments[seg].start : w.s;
      para = h("p.para", h("button.ts", { onclick: () => seekPlay(start) }, fmtTime(start)));
      words.append(para);
    }
    const span = h("span.w", { "data-i": i, title: w.p < 0.5 ? `Whisper was only ${Math.round(w.p * 100)}% sure` : "" }, w.w);
    if (w.p < 0.5) span.classList.add("low");
    para.append(span);
  });
  words.addEventListener("pointerdown", (e) => {
    const el = e.target.closest(".w");
    if (!el) return;
    const i = Number(el.dataset.i);
    if (e.shiftKey && anchor !== null) selectRange(anchor, i);
    else { anchor = i; picked = new Set([i]); video.currentTime = tr.words[i].s; drawScrub(); }
    paintWords();
    const move = (ev) => {
      const t = document.elementFromPoint(ev.clientX, ev.clientY)?.closest(".w");
      if (t) { selectRange(anchor, Number(t.dataset.i)); paintWords(); }
    };
    words.addEventListener("pointermove", move);
    window.addEventListener("pointerup", () => words.removeEventListener("pointermove", move), { once: true });
  });

  clear(pane,
    h("p.muted", { style: { marginTop: 0 } }, "Click a word to jump there. Drag or Shift-click to select a run, then press ",
      h("span.kbd", "Delete"), " to strike it out (again to restore). Struck words get cut from the video when you render."),
    bar, words,
    h("div", { style: { display: "flex", gap: "8px", marginTop: "18px", flexWrap: "wrap" } },
      h("button.btn.primary", { onclick: renderTranscriptCut }, icon("studio"), "Render edited video"),
      h("button.btn", { onclick: () => exportSubs("srt") }, icon("cc"), "Export captions (.srt)"),
      h("button.btn", { onclick: () => exportSubs("vtt") }, "Export .vtt")));
  pane._stats = stats;
  paintWords();
}

function selectRange(a, b) {
  const [lo, hi] = a < b ? [a, b] : [b, a];
  picked = new Set();
  for (let i = lo; i <= hi; i++) picked.add(i);
}

function paintWords() {
  const del = deletions[src?.id] || new Set();
  const tr = transcripts[src?.id];
  pane.querySelectorAll(".w").forEach((el) => {
    const i = Number(el.dataset.i);
    el.classList.toggle("del", del.has(i));
    el.classList.toggle("picked", picked.has(i));
    el.classList.toggle("hit", !!findText && tr && tr.words[i].w.toLowerCase().includes(findText));
  });
  if (pane._stats && tr) {
    const cut = removalRanges().reduce((a, [x, y]) => a + (y - x), 0);
    const D = src.duration || 1;
    clear(pane._stats, del.size ? ["Cutting ", h("b", `${cut.toFixed(1)} s`), ` (${Math.round((cut / D) * 100)}%) in ${removalRanges().length} place${removalRanges().length === 1 ? "" : "s"}`] : "No cuts yet");
  }
  drawScrub();
}

function strikePicked() {
  const del = deletions[src.id];
  if (!picked.size) return;
  const allDel = [...picked].every((i) => del.has(i));
  for (const i of picked) allDel ? del.delete(i) : del.add(i);
  picked = new Set();
  paintWords();
}
function strikeFillers() {
  const tr = transcripts[src.id], del = deletions[src.id];
  let n = 0;
  tr.words.forEach((w, i) => { if (FILLERS.has(w.w.toLowerCase().replace(/[^a-z]/g, ""))) { del.add(i); n++; } });
  toast(n ? `Struck ${n} filler word${n > 1 ? "s" : ""}` : "No fillers found. Whisper tends to tidy them up already.");
  paintWords();
}
function strikeFound() {
  if (!findText) return;
  const tr = transcripts[src.id], del = deletions[src.id];
  tr.words.forEach((w, i) => { if (w.w.toLowerCase().includes(findText)) del.add(i); });
  paintWords();
}

function removalRanges() {
  const tr = transcripts[src?.id], del = deletions[src?.id];
  if (!tr || !del || !del.size) return [];
  const W = tr.words, out = [];
  let i = 0;
  while (i < W.length) {
    if (!del.has(i)) { i++; continue; }
    let j = i;
    while (j + 1 < W.length && del.has(j + 1)) j++;
    const a = i > 0 ? Math.max(W[i - 1].e + 0.04, W[i].s - 0.25) : 0;
    const b = j + 1 < W.length ? Math.max(W[j].e, Math.min(W[j + 1].s - 0.04, W[j].e + 0.25)) : (src.duration || W[j].e);
    out.push([a, b]);
    i = j + 1;
  }
  return out;
}

let lastCur = -1;
function highlightWord() {
  const tr = transcripts[src?.id];
  if (mode !== "transcript" || !tr) return;
  const t = video.currentTime;
  let lo = 0, hi = tr.words.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (tr.words[mid].s <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  if (idx === lastCur) return;
  pane.querySelector(".w.cur")?.classList.remove("cur");
  const el = pane.querySelector(`.w[data-i="${idx}"]`);
  if (el) { el.classList.add("cur"); if (!video.paused) el.scrollIntoView({ block: "nearest" }); }
  lastCur = idx;
}

function seekPlay(t) { video.currentTime = t; video.play(); }

async function renderTranscriptCut() {
  const del = [...deletions[src.id]];
  if (!del.length) return toast("Strike out some words first.", true);
  try { await runJob("transcript_cut", { source_id: src.id, deleted: del }, `Transcript edit: ${src.title}`); } catch (e) { toast(e.message, true); }
}
async function exportSubs(format) {
  try {
    const r = await post(`/api/sources/${src.id}/subtitles`, { format, deleted: [...deletions[src.id]] });
    toast(`Saved ${r.cues} captions to ${r.path.split(/[\\/]/).pop()}`);
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- side
function renderSide() {
  if (!side) return;
  const renders = state.jobs.filter((j) => j.lane === "render").slice(0, 12);
  clear(side, h("h2", "Renders"),
    renders.length ? renders.map((j) => jobCard(j, { compact: true }))
      : h("p.muted", "Finished files land in your exports folder and show up here with a play button."));
}
