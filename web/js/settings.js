import { bus, clear, desktop, fmtBytes, get, h, icon, openPlayer, post, reveal, state, toast, waitForJob } from "./core.js";
import { addOwnModel, openSetup } from "./setup.js";

let root, body;
let s = null;
let mdl = null;          // /api/ai/models
let showLog = false;
let logTimer = null;

const OLLAMA_RECOMMENDED = [
  ["llama3.2:3b", "chat", "Answers questions. 2 GB, fast on a 4 GB GPU."],
  ["gemma3:4b", "vision", "Looks at images, video frames and scanned pages. 3.3 GB."],
  ["nomic-embed-text", "embed", "Turns passages into meaning vectors for semantic search. 274 MB."],
];
const WHISPER = [
  ["tiny", "Tiny, 75 MB: fastest, rough on names"], ["base", "Base, 145 MB: good default on CPU (included)"],
  ["small", "Small, 490 MB: noticeably better, ~3× slower"], ["medium", "Medium, 1.5 GB: great accuracy, slow on CPU"],
  ["large-v3", "Large v3, 3 GB: best, needs a GPU"], ["distil-large-v3", "Distil large v3: near-large accuracy, faster (English)"],
];
const STATE_TEXT = {
  ready: ["ok", "Loaded and ready"], idle: ["", "Starts on first use"], loading: ["warm", "Loading the model…"],
  sleeping: ["", "Asleep to free memory; wakes on the next question"], "no-model": ["no", "No answer model yet"],
  error: ["no", "Engine error"], "no-engine": ["no", "Engine files missing"],
};

export function mount(el) {
  root = el;
  body = h("div.settings-inner");
  el.append(h("header.view-head", h("h1", "Settings"), h("span.sub", "Changes save as you make them")), h("div.settings", body));
  const typing = () => document.activeElement?.matches?.("input, select, textarea") && body.contains(document.activeElement);
  bus.on("status", () => { if (!root.hidden && !typing()) render(); });
  bus.on("models:refresh", () => { if (!root.hidden) loadModels(); });
}
export async function show() { s = await get("/api/settings"); await loadModels(); }
export function hide() { clearInterval(logTimer); logTimer = null; }

async function loadModels() {
  try { mdl = await get("/api/ai/models"); } catch { mdl = null; }
  render();
}

async function save(patch, quiet) {
  try {
    s = await post("/api/settings", patch);
    if (!quiet) toast("Saved");
    bus.emit("status:refresh");
    if ("provider" in patch || "local_chat_model" in patch || "local_embed_model" in patch) loadModels();
  } catch (e) { toast(e.message, true); }
}

function text(key, label, attrs = {}) {
  return h("label.field" + (attrs.full ? ".full" : ""), h("span", label),
    h("input.input", { value: s[key], ...attrs, full: undefined, onchange: (e) => save({ [key]: attrs.type === "number" ? Number(e.target.value) : e.target.value }) }));
}
function select(key, label, options, full) {
  return h("label.field" + (full ? ".full" : ""), h("span", label),
    h("select.input", { onchange: (e) => save({ [key]: e.target.value }) },
      options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(s[key]) }, l))));
}
function check(key, label, hint) {
  return h("label.check.full", h("input", { type: "checkbox", checked: !!s[key], onchange: (e) => save({ [key]: e.target.checked }) }),
    h("span", label, hint && h("small", hint)));
}
function group(title, desc, ...fields) {
  return h("div.set-group", h("div", h("h2", title), h("p", desc)), h("div.set-fields", fields));
}

// ---------------------------------------------------------------- built-in engine
function engineStatus(ai) {
  const [cls, txt] = STATE_TEXT[ai.state] || ["", ai.state];
  const e = ai.engine || {};
  const chat = e.chat || {};
  const facts = [];
  const fact = (k, v) => v && facts.push(h("span", `${k} `, h("b", v)));
  fact("Running on", e.label || (chat.state === "stopped" ? null : "…"));
  fact("Slots", chat.slots && `${chat.slots[0]} × ${chat.slots[1].toLocaleString()} tokens`);
  fact("Load time", chat.load_s && `${chat.load_s} s`);
  fact("Engine", mdl?.engine_version && `llama.cpp ${mdl.engine_version}`);
  fact("RAM", ai.ram_gb && `${Math.round(ai.ram_gb)} GB`);
  return h("div.full.engine-card",
    h("div.engine-top", h(`span.pill.${cls || "plain"}`, txt), ai.error && h("span.err", ai.error), h("div.grow"),
      h("button.btn.sm", { onclick: async () => { await post("/api/ai/restart"); toast("Restarting the engine"); bus.emit("status:refresh"); } }, icon("refresh"), "Restart"),
      h("button.btn.sm", { title: "Unload the model right now, e.g. before launching a game",
        onclick: async () => { await post("/api/ai/unload"); toast("Model unloaded. It reloads on the next question."); bus.emit("status:refresh"); } }, "Free memory now"),
      h("button.btn.ghost.sm", { "aria-pressed": String(showLog), onclick: () => { showLog = !showLog; render(); } }, showLog ? "Hide log" : "Engine log")),
    h("div.facts", facts),
    showLog && h("pre.enginelog", { id: "enginelog" }, "…"));
}

function modelRow(m, inUse, onUse, extra) {
  const bits = [m.size_label, m.quant, fmtBytes(m.size)].filter(Boolean).join(", ");
  return h(`div.modelrow${inUse ? ".inuse" : ""}`,
    h("div.nm", h("b", m.name || m.file), h("small", `${m.file}, ${bits}`),
      m.context && h("small", `trained for ${Number(m.context).toLocaleString()} tokens of context`)),
    extra,
    m.origin === "bundled" && h("span.pill", "Included"),
    m.origin === "imported" && h("span.pill", "Your file"),
    inUse ? h("span.pill.ok", "In use") : h("button.btn.sm", { onclick: onUse }, "Use"),
    m.origin !== "bundled" && h("button.btn.ghost.sm.icon", {
      title: m.origin === "library" ? "Delete this download" : "Forget this file (it stays on disk)", "aria-label": "Remove",
      onclick: async () => {
        const del = m.origin === "library";
        if (!confirm(del ? `Delete ${m.file} (${fmtBytes(m.size)})?` : `Stop using ${m.file}? The file stays where it is.`)) return;
        await post("/api/ai/forget", { path: m.path, delete: del });
        bus.emit("status:refresh"); loadModels();
      },
    }, icon("trash")));
}

function builtinPanel(ai) {
  if (!mdl) return h("p.full.muted", "Loading…");
  const local = mdl.local || [];
  const chats = local.filter((m) => m.role === "chat");
  const embeds = local.filter((m) => m.role === "embedding");
  const projs = local.filter((m) => m.role === "mmproj");
  const broken = local.filter((m) => m.role === "invalid" || m.role === "missing");
  const sel = mdl.selected || {};

  const chatRows = chats.map((m) => modelRow(m, m.path === sel.chat,
    () => post("/api/ai/use", { chat: m.path, builtin: true }).then(() => { toast(`Switching to ${m.name}`); bus.emit("status:refresh"); loadModels(); }),
    m.path === sel.chat ? h("span.pill" + (sel.mmproj ? ".ok" : ".no"), sel.mmproj ? "Sees images" : "Text only") : null));
  const avail = mdl.catalog.filter((c) => !c.installed).map((c) => {
    const j = mdl.downloads[c.id];
    const active = j && (j.status === "running" || j.status === "queued");
    return h("div.modelrow",
      h("div.nm", h("b", c.name), h("small", c.blurb),
        active && h("div.progress", { style: { marginTop: "6px" } }, h("i", { style: { width: `${j.progress * 100}%` } })),
        active && h("small", j.message || "Starting…"),
        j && j.status === "error" && h("small.err", j.error)),
      c.recommended && h("span.pill.ok", "Best fit for this PC"),
      active ? h("button.btn.sm", { onclick: () => post(`/api/jobs/${j.id}/cancel`).then(loadModels) }, "Pause")
        : h("button.btn.sm", { onclick: () => post("/api/ai/download", { preset: c.id }).then(() => { toast(`Downloading ${c.name}`); loadModels(); }) },
          `${c.partial ? "Resume" : "Download"} (${fmtBytes(c.size)})`));
  });
  const stale = state.status?.stale_vectors || 0;

  return [
    engineStatus(ai),
    h("div.full", h("h3.sub-h", "Answer model"), h("p.hint", "Reads your sources, writes answers, and looks at images, frames and scans when it has a vision file."),
      chatRows.length ? chatRows : h("p.muted", "None installed yet."), avail,
      h("div.row-actions",
        h("button.btn.sm", { onclick: addOwnModel }, icon("folder"), "Add a GGUF file…"),
        h("button.btn.ghost.sm", { onclick: openSetup }, "Open the setup guide"),
        projs.length > 0 && h("span.muted", `${projs.length} vision file${projs.length > 1 ? "s" : ""} found`))),
    h("div.full", h("h3.sub-h", "Search model"), h("p.hint", "Turns every passage into a 768-number meaning vector for semantic search. Runs on the CPU so your GPU stays free."),
      embeds.map((m) => modelRow(m, m.path === sel.embed,
        () => post("/api/ai/use", { embed: m.path }).then(() => { toast("Search model changed. Rebuild the semantic index to use it."); bus.emit("status:refresh"); loadModels(); }))),
      stale > 0 && h("div.warn", `${stale} item${stale > 1 ? "s were" : " was"} indexed with a different search model, so ${stale > 1 ? "they don't" : "it doesn't"} show up in meaning search yet. `,
        h("button.btn.sm", { onclick: rebuild }, "Rebuild semantic index"))),
    broken.length > 0 && h("div.full", broken.map((m) => h("div.warn.bad", `${m.file}: ${m.error || "file not found"}`,
      h("button.btn.ghost.sm", { onclick: () => post("/api/ai/forget", { path: m.path }).then(loadModels) }, "Forget")))),
  ];
}

function performanceGroup() {
  return group("Performance", "How the built-in engine uses this PC. llama.cpp measures free GPU memory and puts as much of the model there as fits; the rest runs on the CPU.",
    select("gpu_mode", "Graphics card", [["auto", "Auto: use the GPU as far as it fits"], ["cpu", "CPU only (keep the GPU free)"]]),
    select("idle_sleep_min", "Free memory when idle", [[0, "Never"], [5, "After 5 minutes"], [10, "After 10 minutes"], [30, "After 30 minutes"], [60, "After 1 hour"]]),
    select("num_ctx", "Context window", [[4096, "4k tokens, least memory"], [8192, "8k tokens (default)"], [16384, "16k tokens"], [32768, "32k tokens, needs lots of memory"]]),
    select("threads", "CPU threads", [[0, "Auto"], [2, "2"], [4, "4"], [6, "6"], [8, "8"], [12, "12"], [16, "16"]]));
}

// ---------------------------------------------------------------- ollama
function ollamaPanel(ai) {
  const names = ai.ollama_models || [];
  const installed = new Set(names.flatMap((n) => [n, n.replace(/:latest$/, "")]));
  const opts = (kind) => {
    const cur = s[`${kind}_model`];
    const list = names.map((n) => (n === `${cur}:latest` ? cur : n));
    if (!list.includes(cur)) list.unshift(cur);
    return list.map((n) => [n, n]);
  };
  const conn = ai.state !== "offline"
    ? h("span.pill.ok", `Connected, Ollama ${ai.ollama_version || ""}`)
    : h("span.pill.no", ai.error || "Not connected");
  return [
    text("ollama_url", "Server address", { full: true, spellcheck: false }),
    h("div.full", { style: { display: "flex", gap: "10px", alignItems: "center" } }, conn,
      h("button.btn.sm", { onclick: () => bus.emit("status:refresh") }, icon("refresh"), "Test connection")),
    select("chat_model", "Answers", opts("chat")),
    select("vision_model", "Vision (images, frames, scans)", opts("vision")),
    select("embed_model", "Embeddings (semantic search)", opts("embed"), true),
    h("div.full", OLLAMA_RECOMMENDED.map(([name, role, why]) => h("div.modelrow",
      h("div.nm", h("b", name), h("small", why)),
      installed.has(name) ? h("span.pill.ok", "Installed")
        : h("button.btn.sm", { disabled: ai.state === "offline", onclick: (e) => pull(name, e.currentTarget) }, "Download"),
      installed.has(name) && s[`${role}_model`] !== name && h("button.btn.ghost.sm", { onclick: () => save({ [`${role}_model`]: name }) }, "Use")))),
  ];
}

// ---------------------------------------------------------------- watermark
const CORNERS = [["top-left", "Top left"], ["top-right", "Top right"], ["bottom-left", "Bottom left"], ["bottom-right", "Bottom right"]];
const WM_SIZES = [["tiny", "Tiny"], ["small", "Small"], ["medium", "Medium"]];
const previews = new Map();   // layout -> <img>, reused while the settings are unchanged (render() runs on every status tick)

async function saveWm(patch) { await save(patch); render(); }

function wmPreview(layout, caption) {
  const q = new URLSearchParams({ layout, text: s.watermark_text || "", corner: s.watermark_corner, size: s.watermark_size }).toString();
  let img = previews.get(layout);
  if (!img || img.dataset.key !== q) {
    img = h("img", { src: `/api/watermark/preview?${q}`, alt: `Watermark preview, ${caption}`, "data-key": q, loading: "lazy",
      title: "Click to see it bigger", onclick: () => openPlayer(img.src, `Watermark preview: ${caption}`, "image") });
    previews.set(layout, img);
  }
  return h(`figure.wm-fig.${layout}`, img, h("figcaption", caption));
}

function watermarkGroup() {
  const on = !!s.watermark;
  const corner = h("div.corner-pick", { role: "radiogroup", "aria-label": "Corner" },
    CORNERS.map(([v, label]) => h(`button.${v}`, { type: "button", role: "radio", title: label, "aria-label": label,
      "aria-checked": String(s.watermark_corner === v), disabled: !on, onclick: () => saveWm({ watermark_corner: v }) }, h("i"))));
  const size = h("div.seg", { role: "group", "aria-label": "Size" },
    WM_SIZES.map(([v, label]) => h("button", { type: "button", "aria-pressed": String(s.watermark_size === v), disabled: !on,
      onclick: () => saveWm({ watermark_size: v }) }, label)));
  const where = (CORNERS.find(([v]) => v === s.watermark_corner) || CORNERS[3])[1].toLowerCase();
  return group("Watermark", "A small tag in one corner of every finished video, so viewers know where to find you. It's a signpost, not copy protection.",
    h("label.check.full", h("input", { type: "checkbox", checked: on, onchange: (e) => saveWm({ watermark: e.target.checked }) }),
      h("span", "Add it to finished videos",
        h("small", "Clips, cuts, merges, timeline renders, conversions and GIFs. Audio-only exports never get one. While it's on, Instant (stream copy) cuts re-encode, because copied video can't be drawn on."))),
    h("label.field", h("span", "Text"),
      h("input.input", { value: s.watermark_text, placeholder: "tavernworks.dev", maxlength: 80, spellcheck: false, disabled: !on,
        onchange: (e) => saveWm({ watermark_text: e.target.value }) })),
    h("div.field", h("span", "Size"), size),
    h("div.field", h("span", "Corner"), h("div.corner-row", corner, h("span.muted", `${where[0].toUpperCase()}${where.slice(1)}`))),
    on && h("div.full.wm-previews",
      wmPreview("landscape", "16:9 renders"),
      wmPreview("vertical", "Vertical clips: top band, clear of app buttons")));
}

// ---------------------------------------------------------------- render
function render() {
  if (!s) return;
  const ai = state.status?.ai || { provider: s.provider, state: "loading" };
  const builtin = (s.provider || "builtin") === "builtin";
  const seg = h("div.seg.full", { role: "group", "aria-label": "Where the AI runs", style: { justifySelf: "start" } },
    h("button", { "aria-pressed": String(builtin), onclick: () => !builtin && save({ provider: "builtin" }) }, "Built-in (llama.cpp)"),
    h("button", { "aria-pressed": String(!builtin), onclick: () => builtin && save({ provider: "ollama" }) }, "Ollama server"));

  const scroll = body.parentNode.scrollTop;
  clear(body,
    group("AI", builtin
      ? "Lorekeeper carries its own AI engine (llama.cpp) and runs models straight from GGUF files. No servers, no accounts, works offline."
      : "Use models from an Ollama server instead, on this PC or on another box over Tailscale (for example http://100.x.x.x:11434).",
    seg, builtin ? builtinPanel(ai) : ollamaPanel(ai)),

    builtin && performanceGroup(),

    group("Answers", "How much the model reads and how adventurous it gets.",
      select("top_k", "Passages per answer", [[4, "4, fastest"], [6, "6"], [8, "8, balanced"], [10, "10"], [12, "12, most thorough"]]),
      select("temperature", "Creativity", [[0.1, "Strictly factual (0.1)"], [0.4, "Balanced (0.4)"], [0.7, "Playful (0.7)"]]),
      !builtin && select("num_ctx", "Context window", [[4096, "4k tokens, low VRAM"], [8192, "8k tokens"], [16384, "16k tokens"], [32768, "32k tokens"]], true)),

    group("Transcription", "Whisper runs on this PC. Base is included; other sizes download once on first use.",
      select("whisper_model", "Model size", WHISPER, true),
      select("whisper_device", "Run on", [["cpu", "CPU (works everywhere)"], ["auto", "GPU if available, else CPU"], ["cuda", "NVIDIA GPU (needs CUDA 12 + cuDNN)"]]),
      text("whisper_language", "Language code (blank = detect)", { placeholder: "en, es, de…", spellcheck: false })),

    group("Indexing", "Extra work done when you add files. Each video frame or scanned page is one vision-model call.",
      select("max_keyframes", "Frames described per video", [[0, "None"], [8, "Up to 8"], [16, "Up to 16"], [32, "Up to 32"], [64, "Up to 64"]], true),
      check("caption_frames", "Describe video frames", "Lets you ask about what was on screen, not just what was said."),
      check("ocr_scanned_pdfs", "Read scanned PDFs", "Pages without a text layer are rendered and read by the vision model.")),

    watermarkGroup(),

    group("Exports", "Every render and caption file is saved here. Your originals are never changed.",
      h("label.field.full", h("span", "Folder"),
        h("div", { style: { display: "flex", gap: "8px" } },
          h("input.input", { value: s.export_dir, style: { flex: 1 }, onchange: (e) => save({ export_dir: e.target.value }) }),
          desktop() && h("button.btn", { onclick: async () => { const p = await desktop().pick_folder(); if (p) { await save({ export_dir: p }); render(); } } }, "Browse"),
          h("button.btn", { onclick: () => reveal(null) }, icon("folder"), "Open")))));
  body.parentNode.scrollTop = scroll;

  clearInterval(logTimer); logTimer = null;
  if (showLog) { pollLog(); logTimer = setInterval(pollLog, 2000); }
}

async function pollLog() {
  const pre = document.getElementById("enginelog");
  if (!pre) return;
  try {
    const r = await get("/api/ai/log?server=chat");
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
    pre.textContent = r.lines.join("\n") || "The engine hasn't started yet. It starts on the first question.";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch { /* ignore */ }
}

async function pull(name, btn) {
  btn.disabled = true;
  try {
    const j = await post("/api/models/pull", { name });
    bus.emit("jobs:refresh");
    const done = await waitForJob(j.id, (jj) => { btn.textContent = jj.progress ? `${Math.round(jj.progress * 100)}%` : (jj.message || "Starting…"); });
    if (done.status === "done") { toast(`${name} is ready`); bus.emit("status:refresh"); }
    else toast(`${name}: ${done.error || done.status}`, true, 8000);
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}

async function rebuild() {
  try { await post("/api/reembed"); toast("Rebuilding the semantic index. Progress is under Jobs."); bus.emit("jobs:refresh"); }
  catch (e) { toast(e.message, true); }
}
