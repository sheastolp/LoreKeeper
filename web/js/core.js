// Shared helpers: API, DOM building, formatting, state, jobs, toasts.

export async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || j.error || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res.json();
}
export const get = (p) => api(p);
export const post = (p, b = {}) => api(p, { method: "POST", body: b });
export const put = (p, b) => api(p, { method: "PUT", body: b });
export const del = (p) => api(p, { method: "DELETE" });
export const patch = (p, b) => api(p, { method: "PATCH", body: b });

// h("div.cls#id", {attrs}, ...children)
export function h(tag, attrs, ...kids) {
  const m = tag.match(/^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i);
  const el = document.createElement(m[1] || "div");
  for (const part of (m[2] || "").match(/[.#][\w-]+/g) || []) {
    if (part[0] === ".") el.classList.add(part.slice(1)); else el.id = part.slice(1);
  }
  if (attrs !== null && attrs !== undefined && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    kids.unshift(attrs); attrs = null;
  }
  let value;
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "value") value = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "html") el.innerHTML = v;
    else if (k in el && k !== "list" && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  if (value !== undefined) el.value = value;
  return el;
}
function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
export function icon(name, cls = "") {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  if (cls) svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
export function clear(el, ...kids) { el.replaceChildren(); append(el, kids); return el; }

export function fmtTime(t, ms = false) {
  if (t === null || t === undefined || isNaN(t)) return "–";
  const neg = t < 0; t = Math.abs(t);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
  const frac = ms ? "." + String(Math.floor((t % 1) * 100)).padStart(2, "0") : "";
  const core = h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  return (neg ? "-" : "") + core + frac;
}
export function parseTime(str) {
  if (typeof str === "number") return str;
  const parts = String(str).trim().split(":").map(Number);
  if (parts.some(isNaN)) return NaN;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}
export function fmtBytes(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
export const kindLabel = { video: "Video", audio: "Audio", image: "Image", document: "Document", url: "Web page" };

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ---------------------------------------------------------------- bus & state
const listeners = {};
export const bus = {
  on(ev, fn) { (listeners[ev] ||= new Set()).add(fn); return () => listeners[ev].delete(fn); },
  emit(ev, data) { for (const fn of listeners[ev] || []) { try { fn(data); } catch (e) { console.error(e); } } },
};
export const state = { sources: [], status: null, jobs: [], scope: new Set(), desktop: false };

export function sourceById(id) { return state.sources.find((s) => s.id === id); }

// ---------------------------------------------------------------- toasts
export function toast(msg, bad = false, ms = 4200) {
  const t = h("div.toast", { role: "status" }, msg);
  if (bad) t.classList.add("bad");
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), ms);
}

// ---------------------------------------------------------------- desktop bridge
export function desktop() { return window.pywebview && window.pywebview.api; }

export async function pickAndAddFiles() {
  const d = desktop();
  if (d) {
    const paths = await d.pick_files();
    if (!paths || !paths.length) return;
    const r = await post("/api/sources/paths", { paths });
    if (r.errors.length) toast(r.errors.join("\n"), true, 7000);
    if (r.added.length) toast(`Added ${r.added.length} item${r.added.length > 1 ? "s" : ""}. Indexing in the background.`);
    bus.emit("sources:refresh");
    return;
  }
  const input = document.getElementById("file-input");
  input.value = "";
  input.onchange = () => uploadFiles([...input.files]);
  input.click();
}

export function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/sources/upload?name=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else { let m = xhr.statusText; try { m = JSON.parse(xhr.responseText).detail; } catch { /* */ } reject(new Error(m)); }
    };
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(file);
  });
}

export async function uploadFiles(files) {
  if (!files.length) return;
  let ok = 0;
  for (const f of files) {
    const t = h("div.toast", `Copying ${f.name}… 0%`);
    document.getElementById("toasts").append(t);
    try {
      await uploadFile(f, (p) => { t.textContent = `Copying ${f.name}… ${Math.round(p * 100)}%`; });
      ok++;
    } catch (e) {
      toast(`${f.name}: ${e.message}`, true, 7000);
    } finally { t.remove(); }
    bus.emit("sources:refresh");
  }
  if (ok) toast(`Added ${ok} item${ok > 1 ? "s" : ""}. Indexing in the background.`);
}

export async function reveal(path) {
  try { await post("/api/reveal", { path }); } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- media modal
export function openPlayer(src, title) {
  const isAudio = /\.(mp3|wav|m4a|flac|ogg|opus|aac)$/i.test(title);
  const isImg = /\.(gif|png|jpe?g|webp)$/i.test(title);
  const media = isImg ? h("img", { src }) : h(isAudio ? "audio" : "video", { src, controls: true, autoplay: true });
  const close = () => { if (media.pause) media.pause(); m.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => e.key === "Escape" && close();
  const m = h("div.modal", { onclick: (e) => e.target === m && close() },
    h("div.card", h("header", h("b", title), h("button.btn.ghost.sm.icon", { onclick: close, "aria-label": "Close" }, icon("x"))), media));
  document.body.append(m);
  document.addEventListener("keydown", onKey);
}

// ---------------------------------------------------------------- jobs
export function jobCard(j, { compact = false } = {}) {
  const st = { queued: "Waiting", running: `${Math.round(j.progress * 100)}%`, done: "Done", error: "Failed", cancelled: "Cancelled" }[j.status];
  const card = h(`div.job.${j.status}`, { "data-id": j.id },
    h("div.jt", h("span.name", j.title), h("span.state", st),
      (j.status === "running" || j.status === "queued") &&
        h("button.btn.ghost.sm", { onclick: () => post(`/api/jobs/${j.id}/cancel`).then(() => bus.emit("jobs:refresh")) }, "Cancel")),
    (j.status === "running" || j.status === "queued") && j.message && h("div.msg", j.message),
    j.status === "running" && h("div.progress", h("i", { style: { width: `${j.progress * 100}%` } })),
    j.status === "error" && h("div.msg.err", { title: j.error }, j.error));
  const outs = (j.result && j.result.outputs) || [];
  if (outs.length) {
    card.append(h("div.outs", outs.map((o) => {
      const name = o.split(/[\\/]/).pop();
      return h("div.out", h("span", { title: o }, name),
        h("button.btn.sm", { onclick: () => openPlayer(`/exports/file?path=${encodeURIComponent(o)}`, name) }, "Play"),
        h("button.btn.sm.icon", { title: "Show in folder", onclick: () => reveal(o) }, icon("folder")),
        !compact && h("button.btn.sm", {
          title: "Index this export so you can ask about it and use it on the timeline",
          onclick: async () => { await post("/api/exports/add", { path: o }); toast(`Added ${name} to the library`); bus.emit("sources:refresh"); },
        }, "Add to library"));
    })));
  }
  if (!compact && j.log && j.log.length) {
    card.append(h("details", h("summary.muted", "Log and commands"), h("pre", j.log.join("\n"))));
  }
  return card;
}

export async function runJob(op, params, title) {
  const j = await post(`/api/edit/${op}`, { params, title });
  toast(`${title}: started. Track it under Jobs.`);
  bus.emit("jobs:refresh");
  return j;
}

export function waitForJob(id, onTick) {
  return new Promise((resolve) => {
    const tick = async () => {
      const list = await get("/api/jobs");
      const j = list.find((x) => x.id === id);
      if (onTick && j) onTick(j);
      if (!j || ["done", "error", "cancelled"].includes(j.status)) return resolve(j);
      setTimeout(tick, 700);
    };
    tick();
  });
}

export function glyphFor(s) {
  const g = h("div.glyph");
  if (s.has_thumb) g.style.backgroundImage = `url(/thumb/${s.id})`;
  else g.append(icon(s.kind));
  return g;
}
