import { bus, get, h, icon, jobCard, post, reveal, state, toast, uploadFiles, clear } from "./core.js";
import * as library from "./library.js";
import * as ask from "./ask.js";
import * as studio from "./studio.js";
import * as timeline from "./timeline.js";
import * as settings from "./settings.js";
import { maybeShowSetup } from "./setup.js";

const VIEWS = { library, ask, studio, timeline, settings };
const containers = {};
let current = null;

function show(name, arg) {
  if (!VIEWS[name]) name = "library";
  if (!containers[name]) {
    const el = h("section.view", { "data-name": name });
    document.getElementById("views").append(el);
    containers[name] = el;
    VIEWS[name].mount(el);
  }
  for (const [n, el] of Object.entries(containers)) el.hidden = n !== name;
  document.querySelectorAll(".rail-btn[data-view]").forEach((b) =>
    b.setAttribute("aria-current", b.dataset.view === name ? "page" : "false"));
  if (current !== name && current && VIEWS[current].hide) VIEWS[current].hide();
  current = name;
  if (VIEWS[name].show) VIEWS[name].show(arg);
  try { localStorage.setItem("lk:view", name); } catch { /* storage off */ }
}
bus.on("go", ({ view, arg }) => show(view, arg));
export const currentView = () => current;

document.querySelectorAll(".rail-btn[data-view]").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

// ---------------------------------------------------------------- polling
let busyLibrary = false;
async function refreshSources() {
  try {
    state.sources = await get("/api/sources");
    busyLibrary = state.sources.some((s) => s.status === "queued" || s.status === "processing");
    for (const id of [...state.scope]) if (!state.sources.find((s) => s.id === id)) state.scope.delete(id);
    bus.emit("sources", state.sources);
  } catch (e) { console.warn(e); }
}
bus.on("sources:refresh", refreshSources);

const AI_LABEL = {
  ready: ["ok", "AI ready"], idle: ["ok", "AI ready"], loading: ["warm", "Loading AI"], sleeping: ["", "AI asleep"],
  "no-model": ["bad", "No AI model"], error: ["bad", "AI error"], "no-engine": ["bad", "Engine missing"],
  offline: ["bad", "Ollama offline"], missing: ["warm", "Models missing"],
};
let statusTimer = null;
async function refreshStatus() {
  try {
    state.status = await get("/api/status");
    state.desktop = state.status.desktop;
  } catch (e) {
    state.status = { ai: { provider: "?", state: "error", error: e.message } };
  }
  const ai = state.status.ai || {};
  const [cls, text] = AI_LABEL[ai.state] || ["", ai.state];
  document.getElementById("conn-dot").className = "dot " + cls;
  document.getElementById("conn-label").textContent = ai.provider === "ollama" && ai.state === "ready" ? "Ollama" : text;
  const e = ai.engine || {};
  document.getElementById("conn").title = ai.provider === "ollama"
    ? (ai.error || `Ollama ${ai.ollama_version || ""}` + (ai.missing?.length ? `\nMissing: ${ai.missing.join(", ")}` : ""))
    : [ai.chat_model ? ai.chat_model.name : "No answer model", e.label, ai.error].filter(Boolean).join("\n");
  bus.emit("status", state.status);
  maybeShowSetup();
  // poll quickly while something is changing (model loading or downloading), slowly otherwise
  const busy = ai.state === "loading" || Object.values(ai.downloads || {}).some((d) => d.status === "running" || d.status === "queued");
  clearTimeout(statusTimer);
  statusTimer = setTimeout(refreshStatus, busy ? 1500 : 12000);
}
bus.on("status:refresh", refreshStatus);
document.getElementById("conn").addEventListener("click", () => show("settings"));

let busyJobs = false;
async function refreshJobs() {
  try { state.jobs = await get("/api/jobs"); } catch { return; }
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued");
  busyJobs = active.length > 0;
  const badge = document.getElementById("jobs-badge");
  badge.hidden = !active.length;
  badge.textContent = active.length;
  renderDrawer();
  bus.emit("jobs", state.jobs);
}
bus.on("jobs:refresh", refreshJobs);

function loop(fn, fast, slow, isBusy) {
  const tick = async () => { await fn(); setTimeout(tick, isBusy() ? fast : slow); };
  tick();
}

// ---------------------------------------------------------------- jobs drawer
const drawer = document.getElementById("drawer");
document.getElementById("jobs-btn").addEventListener("click", () => { drawer.classList.toggle("open"); renderDrawer(); });
document.getElementById("drawer-close").addEventListener("click", () => drawer.classList.remove("open"));
document.getElementById("open-exports").addEventListener("click", () => reveal(null));
function renderDrawer() {
  if (!drawer.classList.contains("open")) return;
  const list = document.getElementById("drawer-list");
  const openLogs = new Set([...list.querySelectorAll(".job details[open]")].map((d) => d.closest(".job").dataset.id));
  const top = list.scrollTop;
  const jobs = state.jobs.filter((j) => j.kind !== "ingest" || j.status !== "done");
  clear(list, jobs.length ? jobs.map((j) => jobCard(j)) :
    h("div.empty", h("h2", "Nothing running"), "Renders, indexing and model downloads show up here with live progress."));
  list.querySelectorAll(".job").forEach((c) => { if (openLogs.has(c.dataset.id)) c.querySelector("details")?.setAttribute("open", ""); });
  list.scrollTop = top;
}

// ---------------------------------------------------------------- drag & drop anywhere
const dz = document.getElementById("dropzone");
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; dz.hidden = false; } });
window.addEventListener("dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; dz.hidden = true; } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", async (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; dz.hidden = true;
  const files = [...e.dataTransfer.files];
  if (window.pywebview) {
    // the desktop shell indexes dropped files in place (no copy); wait briefly for it to report back
    const native = await new Promise((resolve) => {
      const timer = setTimeout(() => resolve(null), 900);
      window.addEventListener("lk-native-drop", (ev) => { clearTimeout(timer); resolve(ev.detail); }, { once: true });
    });
    if (native && native.handled) {
      if (native.errors.length) toast(native.errors.join("\n"), true, 7000);
      if (native.added) toast(`Added ${native.added} item${native.added > 1 ? "s" : ""}. Indexing in the background.`);
      bus.emit("sources:refresh");
      return;
    }
  }
  uploadFiles(files);
});

// keep links inside answers from navigating the app window away
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href^='http']");
  if (!a) return;
  e.preventDefault();
  window.open(a.href, "_blank");
});

// ---------------------------------------------------------------- boot
(async function boot() {
  await refreshStatus();
  await refreshSources();
  loop(refreshSources, 1200, 6000, () => busyLibrary);
  loop(refreshJobs, 800, 4000, () => busyJobs);
  let start = "library";
  try { start = localStorage.getItem("lk:view") || "library"; } catch { /* */ }
  if (!state.sources.length) start = "library";
  show(start);
  const ai = state.status?.ai;
  if (ai && ai.provider === "ollama" && ai.state === "offline") {
    toast("Can't reach the Ollama server. Check its address in Settings, or switch to the built-in engine.", true, 8000);
  }
})();
