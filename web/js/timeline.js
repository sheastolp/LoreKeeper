import { bus, clear, debounce, del, fmtTime, get, h, icon, parseTime, post, put, runJob, sourceById, state, toast } from "./core.js";

const HEAD = 120;                    // lane header width (px)
const SNAP_PX = 8;
const CANVAS = [
  ["1920x1080", "1920 × 1080 landscape"], ["1280x720", "1280 × 720 landscape"], ["2560x1440", "2560 × 1440 landscape"],
  ["3840x2160", "3840 × 2160 (4K)"], ["1080x1920", "1080 × 1920 vertical (Shorts, TikTok)"], ["1080x1080", "1080 × 1080 square"],
];

let root, monEl, inspEl, wrapEl, lanesEl, rulerEl, headEl, binEl, projSel, timeEl, playBtn, zoomEl, presetSel, savedEl;
let proj = null;
let pps = 40;
let t = 0;
let playing = false;
let clock = { t0: 0, p0: 0 };
let selId = null;
let undoStack = [], redoStack = [];
const players = {};

// ---------------------------------------------------------------- data helpers
const tracks = () => proj.data.tracks;
const len = (c) => Math.max(0, c.out - c.in);
const end = (c) => c.start + len(c);
const uid = () => Math.random().toString(36).slice(2, 10);
function find(id) {
  for (const tr of tracks()) { const c = tr.clips.find((x) => x.id === id); if (c) return { tr, c }; }
  return null;
}
function duration() { let d = 0; for (const tr of tracks()) for (const c of tr.clips) d = Math.max(d, end(c)); return d; }
function srcDur(s) { return s.kind === "image" ? 3600 : (s.duration || 0); }
function canvas() { const st = proj.data.settings; return [st.width, st.height]; }
function accepts(tr, s) {
  if (!s) return false;
  if (tr.type === "video") return s.kind === "video" || s.kind === "image";
  return s.kind === "audio" || (s.kind === "video" && s.meta.has_audio);
}

// ---------------------------------------------------------------- mount
export function mount(el) {
  root = el;
  projSel = h("select.input", { "aria-label": "Timeline", onchange: (e) => (e.target.value === "__new" ? newProject() : openProject(e.target.value)) });
  playBtn = h("button.btn.icon", { onclick: togglePlay, "aria-label": "Play or pause", title: "Play / pause (Space)" }, icon("play"));
  timeEl = h("span.time");
  zoomEl = h("input", { type: "range", min: 0, max: 100, value: 45, "aria-label": "Zoom", style: { width: "110px" },
    oninput: (e) => { pps = zoomToPps(Number(e.target.value)); renderLanes(); } });
  presetSel = h("select.input", { "aria-label": "Render quality" },
    h("option", { value: "draft" }, "Draft (half size, fast)"), h("option", { value: "fast" }, "Fast"), h("option", { value: "final", selected: true }, "Final quality"));
  savedEl = h("span.muted", { style: { fontSize: "12px" } });
  monEl = h("div.tl-mon");
  inspEl = h("div.inspector");
  binEl = h("div.bin");
  rulerEl = h("div.ruler");
  headEl = h("div.playhead");
  lanesEl = h("div.lanes");
  wrapEl = h("div.lanes-wrap", lanesEl);

  el.append(
    h("header.view-head", h("h1", "Timeline"), h("span.sub", "Stack clips on tracks, trim, fade, picture-in-picture, render"),
      h("div.grow"), projSel),
    h("div.tl",
      h("div.tl-top", h("div.tl-mon-wrap", monEl), inspEl),
      h("div.tl-bar",
        playBtn, timeEl, h("span.sep"),
        h("button.btn.sm", { onclick: splitAtPlayhead, title: "Split at playhead (S)" }, icon("split"), "Split"),
        h("button.btn.sm", { onclick: () => deleteSelected(false), title: "Delete clip (Del). Shift+Del closes the gap." }, icon("trash"), "Delete"),
        h("button.btn.sm.icon", { onclick: undo, title: "Undo (Ctrl+Z)", "aria-label": "Undo" }, icon("undo")),
        h("button.btn.sm.icon", { onclick: redo, title: "Redo (Ctrl+Y)", "aria-label": "Redo" }, icon("redo")),
        h("span.sep"),
        h("span.zoom", "Zoom", zoomEl, h("button.btn.ghost.sm", { onclick: fit }, "Fit")),
        h("div.grow"), savedEl, presetSel,
        h("button.btn.primary.sm", { onclick: renderProject }, "Render")),
      h("div.tl-bottom", binEl, wrapEl)));

  wrapEl.addEventListener("pointerdown", onLanePointer);
  document.addEventListener("keydown", onKey);
  bus.on("sources", renderBin);
  window.addEventListener("resize", () => !root.hidden && proj && renderLanes());
  loadProjects();
}

export async function show(arg) {
  if (!proj) await loadProjects();
  renderBin();
  if (arg && arg.add) addToEnd(arg.add);
  renderLanes();
}
export function hide() { pause(); }

// ---------------------------------------------------------------- projects
async function loadProjects(selectId) {
  let list = await get("/api/projects");
  if (!list.length) { await post("/api/projects", { name: "My first edit" }); list = await get("/api/projects"); }
  let want = selectId;
  try { want ||= localStorage.getItem("lk:project"); } catch { /* */ }
  if (!list.find((p) => p.id === want)) want = list[0].id;
  clear(projSel, list.map((p) => h("option", { value: p.id }, p.name)), h("option", { value: "__new" }, "+ New timeline"));
  projSel.value = want;
  await openProject(want);
}

async function openProject(id) {
  pause();
  proj = await get(`/api/projects/${id}`);
  try { localStorage.setItem("lk:project", id); } catch { /* */ }
  projSel.value = id;
  undoStack = []; redoStack = []; selId = null; t = 0;
  for (const k of Object.keys(players)) delete players[k];
  buildMonitor();
  renderAll();
  requestAnimationFrame(fit);
}

async function newProject() {
  const p = await post("/api/projects", { name: `Edit ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}` });
  await loadProjects(p.id);
}

const save = debounce(async () => {
  if (!proj) return;
  try {
    await put(`/api/projects/${proj.id}`, { name: proj.name, data: proj.data });
    savedEl.textContent = "Saved";
  } catch (e) { savedEl.textContent = "Not saved: " + e.message; }
}, 600);

function commit(fn) {
  undoStack.push(JSON.stringify(proj.data));
  if (undoStack.length > 200) undoStack.shift();
  redoStack = [];
  fn();
  changed();
}
function changed() { savedEl.textContent = "Saving…"; renderAll(); syncPreview(true); save(); }
function undo() { if (!undoStack.length) return; redoStack.push(JSON.stringify(proj.data)); proj.data = JSON.parse(undoStack.pop()); changed(); }
function redo() { if (!redoStack.length) return; undoStack.push(JSON.stringify(proj.data)); proj.data = JSON.parse(redoStack.pop()); changed(); }

function renderAll() { renderLanes(); renderInspector(); updateTime(); }

// ---------------------------------------------------------------- bin
function renderBin() {
  if (!binEl) return;
  const list = state.sources.filter((s) => s.status === "ready" && (s.kind === "video" || s.kind === "audio" || s.kind === "image"));
  clear(binEl, h("p.hint", "Drag onto a track, or double-click to append."),
    list.length ? list.map((s) => {
      const g = h("div.glyph");
      if (s.has_thumb) g.style.backgroundImage = `url(/thumb/${s.id})`; else g.append(icon(s.kind));
      return h("div.bin-item", { draggable: "true", title: s.title,
        ondragstart: (e) => { e.dataTransfer.setData("text/lk-source", s.id); e.dataTransfer.effectAllowed = "copy"; },
        ondblclick: () => addToEnd(s.id) },
      g, h("span.nm", s.title), h("span.d", s.kind === "image" ? "still" : fmtTime(s.duration)));
    }) : h("p.muted", "Indexed video, audio and images appear here."));
}

function addToEnd(id) {
  const s = sourceById(id);
  if (!s) return;
  const trId = s.kind === "audio" ? "a1" : "v1";
  const tr = tracks().find((x) => x.id === trId);
  const start = tr.clips.reduce((m, c) => Math.max(m, end(c)), 0);
  commit(() => tr.clips.push(newClip(s, start)));
  toast(`Added ${s.title} to ${tr.name}`);
}

function newClip(s, start) {
  return { id: uid(), source_id: s.id, start: Math.max(0, start), in: 0, out: s.kind === "image" ? 4 : srcDur(s),
    volume: 1, fade_in: 0, fade_out: 0 };
}

// ---------------------------------------------------------------- lanes
function zoomToPps(v) { return 4 * Math.pow(1.055, v); }          // 4 … ~840 px/s
function ppsToZoom(p) { return Math.log(p / 4) / Math.log(1.055); }
function fit() {
  if (!proj) return;
  const w = wrapEl.clientWidth - HEAD - 40;
  const d = Math.max(10, duration());
  pps = Math.max(4, Math.min(840, w / d));
  zoomEl.value = ppsToZoom(pps);
  renderLanes();
}

function renderLanes() {
  if (!proj || !lanesEl) return;
  const sl = wrapEl.scrollLeft, st = wrapEl.scrollTop;
  const d = duration();
  const width = Math.max(wrapEl.clientWidth - HEAD, (Math.max(d, 10) + 20) * pps);
  lanesEl.style.width = `${width + HEAD}px`;

  // ruler
  const steps = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800];
  const step = steps.find((s) => s * pps >= 72) || 3600;
  clear(rulerEl);
  rulerEl.style.width = `${width}px`;
  for (let x = 0; x * pps < width; x += step) {
    rulerEl.append(h("div.tick", { style: { left: `${x * pps}px` } }, fmtTime(x, step < 1)));
  }

  const lanes = tracks().map((tr) => {
    const body = h("div.lane-body", { "data-track": tr.id });
    body.addEventListener("dragover", (e) => {
      if (![...e.dataTransfer.types].includes("text/lk-source")) return;
      e.preventDefault(); body.classList.add("drop");
    });
    body.addEventListener("dragleave", () => body.classList.remove("drop"));
    body.addEventListener("drop", (e) => {
      body.classList.remove("drop");
      const id = e.dataTransfer.getData("text/lk-source");
      if (!id) return;
      e.preventDefault();
      const s = sourceById(id);
      const r = body.getBoundingClientRect();
      let target = tr;
      if (!accepts(tr, s)) {
        target = tracks().find((x) => x.id === (s.kind === "audio" ? "a1" : "v1"));
        if (!accepts(target, s)) return toast("That item can't go on this track.", true);
        toast(`${s.kind === "audio" ? "Audio" : "Pictures"} go on ${target.name}, so it landed there.`);
      }
      commit(() => target.clips.push(newClip(s, (e.clientX - r.left) / pps)));
    });
    for (const c of tr.clips) body.append(clipEl(tr, c));
    const muteBtn = h("button", { "aria-pressed": String(!!tr.muted), title: "Mute track",
      onclick: () => commit(() => { tr.muted = !tr.muted; }) }, "M");
    const hideBtn = tr.type === "video" && h("button", { "aria-pressed": String(!!tr.hidden), title: "Hide track",
      onclick: () => commit(() => { tr.hidden = !tr.hidden; }) }, "H");
    return h(`div.lane.${tr.type}`, h("div.lane-head", h("span.ln", tr.name), h("div.lb", muteBtn, hideBtn)), body);
  });
  clear(lanesEl, rulerEl, lanes, headEl);
  updatePlayhead();
  wrapEl.scrollLeft = sl; wrapEl.scrollTop = st;
}

function clipEl(tr, c) {
  const s = sourceById(c.source_id);
  const el = h(`div.clip.${tr.id}${c.id === selId ? ".sel" : ""}${(c.muted || tr.muted) ? ".muted" : ""}`, { "data-clip": c.id });
  placeClip(el, tr, c, s);
  el.append(h("div.shade"),
    h("div.cl", s ? s.title : "Missing source", `  ${len(c).toFixed(1)}s`),
    c.fade_in > 0 && h("div.fade.in", { style: { width: `${c.fade_in * pps}px` } }),
    c.fade_out > 0 && h("div.fade.out", { style: { width: `${c.fade_out * pps}px` } }),
    h("div.h.l", { "data-handle": "l" }), h("div.h.r", { "data-handle": "r" }));
  return el;
}

function placeClip(el, tr, c, s) {
  el.style.left = `${c.start * pps}px`;
  el.style.width = `${Math.max(4, len(c) * pps)}px`;
  if (!s) return;
  if (s.kind === "image") {
    el.style.backgroundImage = s.has_thumb ? `url(/thumb/${s.id})` : "";
    el.style.backgroundSize = "auto 100%";
    el.style.backgroundRepeat = "repeat-x";
  } else if (tr.type === "video" && s.has_filmstrip) {
    el.style.backgroundImage = `url(/filmstrip/${s.id})`;
    el.style.backgroundSize = `${srcDur(s) * pps}px 100%`;
    el.style.backgroundPosition = `${-c.in * pps}px 0`;
  } else if (s.has_waveform) {
    el.style.backgroundImage = `url(/waveform/${s.id})`;
    el.style.backgroundSize = `${srcDur(s) * pps}px 70%`;
    el.style.backgroundPosition = `${-c.in * pps}px 70%`;
  }
}

function updatePlayhead() {
  if (!headEl) return;
  headEl.style.left = `${HEAD + t * pps}px`;
  headEl.style.height = `${lanesEl.scrollHeight}px`;
  updateTime();
}
function updateTime() { if (proj) timeEl.textContent = `${fmtTime(t, true)} / ${fmtTime(duration(), true)}`; }

// ---------------------------------------------------------------- pointer: select, move, trim, scrub
function snapPoints(exceptId) {
  const pts = [0, t];
  for (const tr of tracks()) for (const c of tr.clips) if (c.id !== exceptId) pts.push(c.start, end(c));
  return pts;
}
function snap(x, pts) {
  let best = x, bd = SNAP_PX / pps;
  for (const p of pts) { const d = Math.abs(p - x); if (d < bd) { bd = d; best = p; } }
  return best;
}

function onLanePointer(e) {
  const clipNode = e.target.closest(".clip");
  const inRuler = e.target.closest(".ruler");
  const laneBody = e.target.closest(".lane-body");
  if (!clipNode && (inRuler || laneBody)) {
    // scrub
    const r = (inRuler || laneBody).getBoundingClientRect();
    const scrubTo = (ev) => { t = Math.max(0, (ev.clientX - r.left) / pps); updatePlayhead(); scheduleSync(); };
    if (!inRuler) { selId = null; renderLanes(); renderInspector(); }
    pause();
    scrubTo(e);
    const mv = (ev) => scrubTo(ev);
    window.addEventListener("pointermove", mv);
    window.addEventListener("pointerup", () => window.removeEventListener("pointermove", mv), { once: true });
    return;
  }
  if (!clipNode) return;
  e.preventDefault();
  const found = find(clipNode.dataset.clip);
  if (!found) return;
  const { c } = found;
  let tr = found.tr;
  const s = sourceById(c.source_id);
  if (selId !== c.id) { selId = c.id; lanesEl.querySelectorAll(".clip.sel").forEach((n) => n.classList.remove("sel")); clipNode.classList.add("sel"); renderInspector(); }
  const handle = e.target.dataset.handle;
  const x0 = e.clientX;
  const orig = { start: c.start, in: c.in, out: c.out };
  const before = JSON.stringify(proj.data);
  const pts = snapPoints(c.id);
  let moved = false;

  const mv = (ev) => {
    const dx = (ev.clientX - x0) / pps;
    if (!moved && Math.abs(ev.clientX - x0) < 3) return;
    moved = true;
    if (handle === "l") {
      const minIn = s && s.kind === "image" ? -Infinity : 0;
      let ns = snap(orig.start + dx, pts);
      let ni = orig.in + (ns - orig.start);
      if (ni < minIn) { ni = minIn; ns = orig.start + (ni - orig.in); }
      if (ns < 0) { ni += -ns; ns = 0; }
      if (orig.out - ni < 0.1) { ni = orig.out - 0.1; ns = orig.start + (ni - orig.in); }
      if (s && s.kind === "image") { c.out = orig.out - (ni - orig.in); c.in = 0; } else c.in = ni;
      c.start = ns;
    } else if (handle === "r") {
      const ne = snap(orig.start + (orig.out - orig.in) + dx, pts);
      c.out = Math.max(orig.in + 0.1, Math.min(s ? srcDur(s) : orig.out, orig.in + (ne - orig.start)));
    } else {
      let ns = Math.max(0, orig.start + dx);
      const sStart = snap(ns, pts), sEnd = snap(ns + len(c), pts);
      if (sStart !== ns) ns = sStart; else if (sEnd !== ns + len(c)) ns = sEnd - len(c);
      c.start = Math.max(0, ns);
      const under = document.elementFromPoint(ev.clientX, ev.clientY)?.closest(".lane-body");
      const target = under && tracks().find((x) => x.id === under.dataset.track);
      if (target && target !== tr && accepts(target, s)) {
        tr.clips.splice(tr.clips.indexOf(c), 1);
        target.clips.push(c);
        tr = target;
        under.append(clipNode);
        clipNode.className = clipNode.className.replace(/\b(v1|v2|a1|a2)\b/, target.id);
      }
    }
    placeClip(clipNode, tr, c, s);
    updateTime();
  };
  const up = () => {
    window.removeEventListener("pointermove", mv);
    if (moved) {
      undoStack.push(before); redoStack = [];
      changed();
    } else if (!handle) {
      // a plain click selects the clip and parks the playhead where you clicked (then S splits there)
      const lb = clipNode.parentNode.getBoundingClientRect();
      t = Math.max(0, (x0 - lb.left) / pps);
      updatePlayhead();
      syncPreview(true);
    }
  };
  window.addEventListener("pointermove", mv);
  window.addEventListener("pointerup", up, { once: true });
}

// ---------------------------------------------------------------- editing commands
function splitAtPlayhead() {
  const targets = [];
  const sel = selId && find(selId);
  if (sel && t > sel.c.start + 0.05 && t < end(sel.c) - 0.05) targets.push(sel);
  else for (const tr of tracks()) for (const c of tr.clips) if (t > c.start + 0.05 && t < end(c) - 0.05) targets.push({ tr, c });
  if (!targets.length) return toast("Put the playhead over a clip to split it.");
  commit(() => {
    for (const { tr, c } of targets) {
      const s = sourceById(c.source_id);
      const cut = c.in + (t - c.start);
      const b = { ...c, id: uid(), start: t, in: s && s.kind === "image" ? 0 : cut, out: s && s.kind === "image" ? c.out - (t - c.start) : c.out, fade_in: 0 };
      if (s && s.kind === "image") c.out = t - c.start; else c.out = cut;
      c.fade_out = 0;
      tr.clips.push(b);
      selId = b.id;
    }
  });
}

function deleteSelected(ripple) {
  const f = selId && find(selId);
  if (!f) return;
  commit(() => {
    const gap = len(f.c), at = f.c.start;
    f.tr.clips.splice(f.tr.clips.indexOf(f.c), 1);
    if (ripple) for (const c of f.tr.clips) if (c.start >= at) c.start = Math.max(0, c.start - gap);
    selId = null;
  });
}

function onKey(e) {
  if (root.hidden || !proj) return;
  if (e.target.closest("input, textarea, select")) return;
  const k = e.key, ctrl = e.ctrlKey || e.metaKey;
  const frame = 1 / (proj.data.settings.fps || 30);
  if (k === " ") { e.preventDefault(); togglePlay(); }
  else if (k === "s" || k === "S") { if (!ctrl) splitAtPlayhead(); }
  else if (k === "Delete" || k === "Backspace") { e.preventDefault(); deleteSelected(e.shiftKey); }
  else if (ctrl && (k === "z" || k === "Z")) { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (ctrl && (k === "y" || k === "Y")) { e.preventDefault(); redo(); }
  else if (k === "ArrowLeft" || k === "ArrowRight") {
    e.preventDefault(); pause();
    t = Math.max(0, t + (k === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 1 : frame));
    updatePlayhead(); scheduleSync();
  } else if (k === "Home") { t = 0; updatePlayhead(); scheduleSync(); }
  else if (k === "End") { t = duration(); updatePlayhead(); scheduleSync(); }
  else if (k === "+" || k === "=") { zoomEl.value = Number(zoomEl.value) + 6; pps = zoomToPps(Number(zoomEl.value)); renderLanes(); }
  else if (k === "-") { zoomEl.value = Number(zoomEl.value) - 6; pps = zoomToPps(Number(zoomEl.value)); renderLanes(); }
  else if (k === "Escape") { selId = null; renderLanes(); renderInspector(); }
}

// ---------------------------------------------------------------- inspector
function renderInspector() {
  if (!inspEl || !proj) return;
  const f = selId && find(selId);
  if (!f) return projectInspector();
  const { tr, c } = f;
  const s = sourceById(c.source_id);
  const timeField = (label, key, opts = {}) => h("label.field", h("span", label),
    h("input.input", { value: fmtTime(c[key], true), disabled: opts.disabled,
      onchange: (e) => {
        const v = parseTime(e.target.value);
        if (isNaN(v)) return renderInspector();
        commit(() => {
          if (key === "start") c.start = Math.max(0, v);
          if (key === "in") c.in = Math.max(0, Math.min(c.out - 0.1, v));
          if (key === "out") c.out = Math.max(c.in + 0.1, Math.min(s ? srcDur(s) : v, v));
        });
      } }));
  const hasAudio = s && (s.kind === "audio" || s.meta.has_audio);
  const pip = c.pip || {};
  const posBtn = (p) => h("button", { disabled: !p, "aria-pressed": String((pip.pos || "br") === p), title: p,
    onclick: () => p && commit(() => { c.pip = { ...(c.pip || { scale: 0.3 }), pos: p }; }) });

  const pct = (v) => `${Math.round(v * 100)}%`;
  const volume = liveRange("Volume", 0, 2, 0.05, () => c.volume ?? 1, (v) => { c.volume = v; }, pct);
  const size = liveRange("Overlay size", 0.1, 1, 0.05, () => pip.scale ?? 0.3,
    (v) => { c.pip = { pos: "br", ...(c.pip || {}), scale: v }; }, pct);
  clear(inspEl,
    h("h2", s ? s.title : "Missing source"),
    h("p.muted", { style: { margin: "-6px 0 12px", fontSize: "12px" } }, `${tr.name}, ${len(c).toFixed(2)} s long`),
    h("div.pair", timeField("Starts at", "start"), h("label.field", h("span", "Length"), h("input.input", { value: `${len(c).toFixed(2)} s`, disabled: true }))),
    s && s.kind !== "image" && h("div.pair", timeField("Source in", "in"), timeField("Source out", "out")),
    hasAudio && volume,
    hasAudio && c.volume > 1 && h("p.muted", { style: { fontSize: "12px", marginTop: "-6px" } }, "Preview plays at 100%; the render applies the full boost."),
    h("div.pair",
      h("label.field", h("span", "Fade in (s)"), h("input.input", { type: "number", min: 0, step: 0.1, value: c.fade_in || 0,
        onchange: (e) => commit(() => { c.fade_in = Math.max(0, Math.min(len(c) / 2, Number(e.target.value) || 0)); }) })),
      h("label.field", h("span", "Fade out (s)"), h("input.input", { type: "number", min: 0, step: 0.1, value: c.fade_out || 0,
        onchange: (e) => commit(() => { c.fade_out = Math.max(0, Math.min(len(c) / 2, Number(e.target.value) || 0)); }) }))),
    tr.type === "video" && hasAudio && h("label.check", { style: { marginBottom: "12px" } },
      h("input", { type: "checkbox", checked: !!c.muted, onchange: (e) => commit(() => { c.muted = e.target.checked; }) }), "Mute this clip's sound"),
    tr.id === "v2" && h("div",
      h("label.field", h("span", "Overlay position"),
        h("div.posgrid", ["tl", null, "tr", null, "center", null, "bl", null, "br"].map(posBtn))),
      size),
    h("div", { style: { display: "flex", gap: "8px", marginTop: "6px" } },
      h("button.btn.sm", { onclick: splitAtPlayhead }, icon("split"), "Split"),
      h("button.btn.sm.danger", { onclick: () => deleteSelected(false) }, icon("trash"), "Delete")));

}

function liveRange(label, min, max, step, getV, setV, fmt) {
  const out = h("output", fmt(getV()));
  let before = null;
  const input = h("input", { type: "range", min, max, step, value: getV(),
    oninput: (e) => { before ||= JSON.stringify(proj.data); setV(Number(e.target.value)); out.textContent = fmt(Number(e.target.value)); syncPreview(false); },
    onchange: () => { if (before) { undoStack.push(before); redoStack = []; before = null; } changed(); } });
  return h("label.field", h("span", label), h("div.rv", input, out));
}

function projectInspector() {
  const st = proj.data.settings;
  const nClips = tracks().reduce((n, tr) => n + tr.clips.length, 0);
  clear(inspEl,
    h("h2", "Timeline settings"),
    h("label.field", h("span", "Name"), h("input.input", { value: proj.name,
      onchange: (e) => { proj.name = e.target.value || "Untitled"; projSel.selectedOptions[0].textContent = proj.name; save(); } })),
    h("label.field", h("span", "Canvas"), h("select.input", {
      onchange: (e) => commit(() => { const [w, hh] = e.target.value.split("x").map(Number); st.width = w; st.height = hh; buildMonitor(); }) },
    CANVAS.map(([v, label]) => h("option", { value: v, selected: v === `${st.width}x${st.height}` }, label)))),
    h("label.field", h("span", "Frame rate"), h("select.input", { onchange: (e) => commit(() => { st.fps = Number(e.target.value); }) },
      [24, 25, 30, 50, 60].map((f) => h("option", { value: f, selected: f === st.fps }, `${f} fps`)))),
    h("p.muted", { style: { fontSize: "12px", lineHeight: 1.6 } },
      `${nClips} clip${nClips === 1 ? "" : "s"}, ${fmtTime(duration(), true)} long. `,
      "V2 sits on top of V1, so put facecams and logos there. Sound from every clip is mixed together; A1 and A2 are for voice-over and music."),
    h("p.muted", { style: { fontSize: "12px", lineHeight: 1.9 } },
      h("span.kbd", "Space"), " play  ", h("span.kbd", "S"), " split  ", h("span.kbd", "Del"), " delete  ",
      h("span.kbd", "Shift"), "+", h("span.kbd", "Del"), " close gap  ", h("span.kbd", "Ctrl"), "+", h("span.kbd", "Z"), " undo  ",
      h("span.kbd", "←"), h("span.kbd", "→"), " frame step"),
    h("button.btn.sm.danger", { style: { marginTop: "8px" }, onclick: async () => {
      if (!confirm(`Delete the timeline "${proj.name}"? Rendered files are kept.`)) return;
      await del(`/api/projects/${proj.id}`);
      try { localStorage.removeItem("lk:project"); } catch { /* */ }
      proj = null; loadProjects();
    } }, icon("trash"), "Delete timeline"));
}

// ---------------------------------------------------------------- preview engine
function buildMonitor() {
  const [w, hh] = canvas();
  monEl.style.aspectRatio = `${w} / ${hh}`;
  clear(monEl);
  for (const k of Object.keys(players)) delete players[k];
  // bottom → top: V1 then V2; audio elements are invisible
  for (const tr of [...tracks()].reverse()) {
    if (tr.type === "video") {
      const v = h("video.layer", { playsInline: true, preload: "auto" });
      const img = h("img.layer", { alt: "" });
      v.hidden = img.hidden = true;
      monEl.append(v, img);
      players[tr.id] = { media: v, img, srcId: null, clipId: null };
    } else {
      const a = h("audio", { preload: "auto" });
      monEl.append(a);
      players[tr.id] = { media: a, srcId: null, clipId: null };
    }
  }
  monEl.append(h("div.empty-mon", { hidden: duration() > 0 }, "Drag clips from the left onto the tracks below."));
}

function activeClip(tr, time) {
  let best = null;
  for (const c of tr.clips) if (time >= c.start && time < end(c) && (!best || c.start >= best.start)) best = c;
  return best;
}

function fadeGain(c, local) {
  let g = 1;
  if (c.fade_in > 0 && local < c.fade_in) g = Math.min(g, local / c.fade_in);
  const rem = len(c) - local;
  if (c.fade_out > 0 && rem < c.fade_out) g = Math.min(g, rem / c.fade_out);
  return Math.max(0, Math.min(1, g));
}

function layout(el, tr, c) {
  el.className = "layer";
  el.removeAttribute("style");
  const scale = (c.pip && c.pip.scale) ?? (tr.id === "v1" ? 1 : 0.3);
  if (tr.id === "v1" || scale >= 0.999) { el.classList.add("full"); return; }
  const [w, hh] = canvas();
  const pos = (c.pip && c.pip.pos) || "br";
  el.classList.add("pip");
  el.style.width = `${scale * 100}%`;
  el.style.height = "auto";
  if (pos === "center") { el.style.left = "50%"; el.style.top = "50%"; el.style.transform = "translate(-50%, -50%)"; return; }
  el.style[pos.includes("l") ? "left" : "right"] = "2.5%";                 // render margin is 2.5% of width…
  el.style[pos.includes("t") ? "top" : "bottom"] = `${(2.5 * w) / hh}%`;   // …in pixels, so rescale for height
}

function seekMedia(P, local) {
  const m = P.media;
  if (m.readyState >= 1) { try { m.currentTime = local; } catch { /* */ } }
  else m.addEventListener("loadedmetadata", () => { try { m.currentTime = local; } catch { /* */ } }, { once: true });
}

function syncPreview(force) {
  if (!proj) return;
  const empty = monEl.querySelector(".empty-mon");
  if (empty) empty.hidden = duration() > 0;
  for (const tr of tracks()) {
    const P = players[tr.id];
    if (!P) continue;
    const c = (tr.type === "video" && tr.hidden) ? null : activeClip(tr, t);
    const s = c && sourceById(c.source_id);
    if (!c || !s) {
      if (!P.media.paused) P.media.pause();
      if (tr.type === "video") { P.media.hidden = true; P.img.hidden = true; }
      P.clipId = null;
      continue;
    }
    const local = c.in + (t - c.start);
    const g = fadeGain(c, t - c.start);
    if (s.kind === "image") {
      P.media.pause(); P.media.hidden = true;
      if (P.img.dataset.src !== s.id) { P.img.src = `/media/${s.id}`; P.img.dataset.src = s.id; }
      layout(P.img, tr, c); P.img.hidden = false; P.img.style.opacity = g;
      P.clipId = c.id;
      continue;
    }
    if (tr.type === "video") { P.img.hidden = true; layout(P.media, tr, c); P.media.hidden = false; P.media.style.opacity = g; }
    if (P.srcId !== s.id) { P.media.src = `/media/${s.id}`; P.srcId = s.id; force = true; }
    if (force || P.clipId !== c.id || Math.abs(P.media.currentTime - local) > 0.35) seekMedia(P, local);
    P.clipId = c.id;
    P.media.muted = !!(tr.muted || c.muted);
    P.media.volume = Math.max(0, Math.min(1, (c.volume ?? 1) * g));
    if (playing && P.media.paused) P.media.play().catch(() => {});
    if (!playing && !P.media.paused) P.media.pause();
  }
}

let syncQueued = false;
function scheduleSync() {
  if (syncQueued) return;
  syncQueued = true;
  requestAnimationFrame(() => { syncQueued = false; syncPreview(true); });
}

function togglePlay() { playing ? pause() : play(); }
function play() {
  if (!proj || duration() <= 0) return;
  if (t >= duration() - 0.05) t = 0;
  playing = true;
  clear(playBtn, icon("pause"));
  clock = { t0: t, p0: performance.now() };
  syncPreview(true);
  requestAnimationFrame(loop);
}
function pause() {
  if (!playing) return;
  playing = false;
  if (playBtn) clear(playBtn, icon("play"));
  for (const P of Object.values(players)) P.media.pause();
}
function loop() {
  if (!playing) return;
  t = clock.t0 + (performance.now() - clock.p0) / 1000;
  if (t >= duration()) { t = duration(); pause(); updatePlayhead(); return; }
  updatePlayhead();
  syncPreview(false);
  // keep the playhead in view
  const x = HEAD + t * pps;
  if (x > wrapEl.scrollLeft + wrapEl.clientWidth - 40) wrapEl.scrollLeft = x - HEAD - 40;
  requestAnimationFrame(loop);
}

// ---------------------------------------------------------------- render
async function renderProject() {
  if (!duration()) return toast("Add some clips first.", true);
  pause();
  await put(`/api/projects/${proj.id}`, { name: proj.name, data: proj.data });
  const hasVideo = tracks().some((tr) => tr.type === "video" && !tr.hidden && tr.clips.length);
  try {
    await runJob("render_project", { project_id: proj.id, preset: presetSel.value, audio_format: hasVideo ? undefined : "mp3" },
      `Render ${proj.name}`);
  } catch (e) { toast(e.message, true); }
}
