import { bus, clear, desktop, fmtBytes, get, h, icon, post, reveal, state, toast, waitForJob } from "./core.js";

let root, body;
let s = null;

const RECOMMENDED = [
  ["llama3.2:3b", "chat", "Answers questions. 2 GB, fast on a 4 GB GPU."],
  ["qwen2.5:7b", "chat", "Smarter answers if you have 8 GB+ of VRAM. 4.7 GB."],
  ["gemma3:4b", "vision", "Looks at images, video frames and scanned pages. 3.3 GB."],
  ["nomic-embed-text", "embed", "Turns passages into meaning vectors for semantic search. 274 MB."],
];
const WHISPER = [
  ["tiny", "Tiny, 75 MB: fastest, rough on names"], ["base", "Base, 145 MB: good default on CPU"],
  ["small", "Small, 490 MB: noticeably better, ~3× slower"], ["medium", "Medium, 1.5 GB: great accuracy, slow on CPU"],
  ["large-v3", "Large v3, 3 GB: best, needs a GPU"], ["distil-large-v3", "Distil large v3: near-large accuracy, faster (English)"],
];

export function mount(el) {
  root = el;
  body = h("div.settings-inner");
  el.append(h("header.view-head", h("h1", "Settings"), h("span.sub", "Changes save as you make them")), h("div.settings", body));
  bus.on("status", () => !root.hidden && render());
}
export async function show() { s = await get("/api/settings"); render(); }

async function save(patch) {
  try {
    s = await post("/api/settings", patch);
    toast("Saved");
    if ("ollama_url" in patch || /model/.test(Object.keys(patch).join())) bus.emit("status:refresh");
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

function modelOptions(kind) {
  const names = (state.status?.models || []).map((m) => m.name);
  const cur = s[`${kind}_model`];
  const list = names.map((n) => (n === `${cur}:latest` ? cur : n));
  if (!list.includes(cur)) list.unshift(cur);
  if (kind === "embed") list.sort((a, b) => /embed|bge|minilm/i.test(b) - /embed|bge|minilm/i.test(a));
  return list.map((n) => [n, n]);
}

function render() {
  if (!s) return;
  const st = state.status || {};
  const installed = new Set((st.models || []).flatMap((m) => [m.name, m.name.replace(/:latest$/, "")]));
  const conn = st.ollama
    ? h("span.pill.ok", `Connected, Ollama ${st.ollama_version || ""}`)
    : h("span.pill.no", st.ollama_error || "Not connected");

  clear(body,
    group("Ollama", "Lorekeeper talks to Ollama for answers, image understanding and embeddings. Point it at this PC, or at your AI box over Tailscale (for example http://100.x.x.x:11434).",
      text("ollama_url", "Server address", { full: true, spellcheck: false }),
      h("div.full", { style: { display: "flex", gap: "10px", alignItems: "center" } }, conn,
        h("button.btn.sm", { onclick: () => bus.emit("status:refresh") }, icon("refresh"), "Test connection"))),

    group("Models", "Pick which installed model does each job. Missing ones can be downloaded right here.",
      select("chat_model", "Answers", modelOptions("chat")),
      select("vision_model", "Vision (images, frames, scans)", modelOptions("vision")),
      select("embed_model", "Embeddings (semantic search)", modelOptions("embed"), true),
      h("div.full", RECOMMENDED.map(([name, role, why]) => h("div.modelrow",
        h("div.nm", h("b", name), h("small", why)),
        installed.has(name) ? h("span.pill.ok", "Installed")
          : h("button.btn.sm", { disabled: !st.ollama, onclick: (e) => pull(name, e.currentTarget) }, "Download"),
        installed.has(name) && s[`${role}_model`] !== name && h("button.btn.ghost.sm", { onclick: () => save({ [`${role}_model`]: name }) }, "Use")))),
      h("p.full.muted", { style: { fontSize: "12px", margin: 0 } },
        "Changed the embedding model? Vectors from different models live in different spaces and can't be compared, so rebuild the index:"),
      h("div.full", h("button.btn.sm", { onclick: rebuild }, icon("refresh"), "Rebuild semantic index"))),

    group("Answers", "How much the model reads and how adventurous it gets.",
      select("top_k", "Passages per answer", [[4, "4, fastest"], [6, "6"], [8, "8, balanced"], [10, "10"], [12, "12, most thorough"]]),
      select("num_ctx", "Context window", [[4096, "4k tokens, low VRAM"], [8192, "8k tokens"], [16384, "16k tokens"], [32768, "32k tokens, needs lots of VRAM"]]),
      select("temperature", "Creativity", [[0.1, "Strictly factual (0.1)"], [0.4, "Balanced (0.4)"], [0.7, "Playful (0.7)"]], true)),

    group("Transcription", "Whisper runs on this PC. The first use of each size downloads it once.",
      select("whisper_model", "Model size", WHISPER, true),
      select("whisper_device", "Run on", [["cpu", "CPU (works everywhere)"], ["auto", "GPU if available, else CPU"], ["cuda", "NVIDIA GPU (needs CUDA 12 + cuDNN)"]]),
      text("whisper_language", "Language code (blank = detect)", { placeholder: "en, es, de…", spellcheck: false })),

    group("Indexing", "Extra work done when you add files. Each video frame or scanned page is one vision-model call.",
      select("max_keyframes", "Frames described per video", [[0, "None"], [8, "Up to 8"], [16, "Up to 16"], [32, "Up to 32"], [64, "Up to 64"]], true),
      check("caption_frames", "Describe video frames", "Lets you ask about what was on screen, not just what was said."),
      check("ocr_scanned_pdfs", "Read scanned PDFs", "Pages without a text layer are rendered and read by the vision model.")),

    group("Exports", "Every render and caption file is saved here. Your originals are never changed.",
      h("label.field.full", h("span", "Folder"),
        h("div", { style: { display: "flex", gap: "8px" } },
          h("input.input", { value: s.export_dir, style: { flex: 1 }, onchange: (e) => save({ export_dir: e.target.value }) }),
          desktop() && h("button.btn", { onclick: async () => { const p = await desktop().pick_folder(); if (p) { await save({ export_dir: p }); render(); } } }, "Browse"),
          h("button.btn", { onclick: () => reveal(null) }, icon("folder"), "Open")))));
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
