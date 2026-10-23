// Clips: find a person's best moments across VODs, review them, export 16:9 or vertical.
import { bus, clear, del, fmtBytes, fmtTime, get, h, icon, patch, post, reveal, state, toast, waitForJob, openPlayer, sourceById } from "./core.js";

let root, peopleEl, vodsEl, runEl, gridEl, detailEl;
let people = [];
let selPerson = null;
const selVods = new Set();
let runs = [];
let curRun = null;
let clipList = [];
let selClip = null;
let statusFilter = "";
let findJob = null;
let presenceCache = {};
const opts = { length: "short", per_vod: 6 };
const ex = { layout: "vertical", vertical_mode: "crop", focus: 0.5, captions: true, show_title: false };
const RATE = { 1080: 7.4, 720: 3.1, 480: 1.3, 360: 0.63 };  // Mbit/s, typical Twitch VOD renditions
const LENGTH = [["short", "Short", "12–30 s"], ["medium", "Medium", "25–60 s"], ["long", "Long", "50–120 s"]];

try { Object.assign(ex, JSON.parse(localStorage.getItem("lk:clipexport") || "{}")); } catch { /* */ }
const saveEx = () => { try { localStorage.setItem("lk:clipexport", JSON.stringify(ex)); } catch { /* */ } };

export function mount(el) {
  root = el;
  peopleEl = h("div.cl-people");
  vodsEl = h("div.cl-vods");
  runEl = h("div.cl-run");
  gridEl = h("div.cl-grid");
  detailEl = h("aside.cl-detail");
  el.append(
    h("header.view-head", h("h1", "Clips"), h("span.sub", "Find someone's best moments across VODs by their voice"), h("div.grow"),
      h("button.btn", { onclick: openTwitch }, icon("twitch"), "Import from Twitch")),
    h("div.cl-body",
      h("div.cl-side",
        h("div.cl-sec", h("div.cl-sec-head", h("h2", "People"), h("button.btn.sm", { onclick: () => openPersonModal() }, icon("plus"), "Add")), peopleEl),
        h("div.cl-sec.grow-sec", h("div.cl-sec-head", h("h2", "VODs"),
          h("button.btn.ghost.sm", { onclick: toggleAllVods }, "All / none")), vodsEl)),
      h("div.cl-main", runEl, gridEl),
      detailEl));
  bus.on("sources", () => { if (!root.hidden) renderVods(); });
  bus.on("jobs", onJobs);
  document.addEventListener("keydown", onKey);
}

export async function show() {
  await Promise.all([loadPeople(), loadRuns()]);
  renderVods();
}
export function hide() { detailEl.querySelector("video")?.pause(); }

// ---------------------------------------------------------------- data
async function loadPeople() {
  people = await get("/api/people");
  if (selPerson && !people.find((p) => p.id === selPerson)) selPerson = null;
  if (!selPerson && people.length) selPerson = people[people.length - 1].id;
  renderPeople(); renderRun();
}
async function loadRuns(pick) {
  runs = await get("/api/clips/runs");
  if (pick) curRun = pick;
  if (!curRun || !runs.find((r) => r.run_id === curRun)) curRun = runs[0]?.run_id || null;
  await loadClips();
}
async function loadClips() {
  clipList = curRun ? await get(`/api/clips?run_id=${curRun}${statusFilter ? `&status=${statusFilter}` : ""}`) : [];
  if (selClip && !clipList.find((c) => c.id === selClip.id)) selClip = null;
  renderRun(); renderGrid(); renderDetail();
}
const person = () => people.find((p) => p.id === selPerson);
const vodItems = () => state.sources.filter((s) => (s.kind === "video" || s.kind === "audio"));

function onJobs(list) {
  if (!findJob) return;
  const j = list.find((x) => x.id === findJob);
  if (!j) return;
  renderProgress(j);
  if (["done", "error", "cancelled"].includes(j.status)) {
    findJob = null;
    if (j.status === "done") {
      const r = j.result;
      toast(r.clips ? `Found ${r.clips} clip${r.clips > 1 ? "s" : ""} of ${r.person}.` : `No clips of ${r.person} this time.`);
      for (const n of r.notes || []) toast(n, false, 7000);
      loadRuns(r.run_id);
    } else if (j.status === "error") toast(j.error, true, 8000);
    renderRun();
  }
}

// ---------------------------------------------------------------- people
function renderPeople() {
  if (!people.length) {
    clear(peopleEl, h("p.cl-empty", "Add the person you want clips of. Lorekeeper learns their voice from a VOD they're in, in about ten seconds of clicking."),
      h("button.btn.primary.sm", { onclick: () => openPersonModal() }, icon("person"), "Add a person"));
    return;
  }
  clear(peopleEl, people.map((p) => {
    const on = p.id === selPerson;
    const card = h(`div.pcard${on ? ".on" : ""}`, { onclick: () => { selPerson = p.id; presenceCache = {}; renderPeople(); renderVods(); renderRun(); } },
      h("div.pc-dot", p.name.slice(0, 1).toUpperCase()),
      h("div.pc-t", h("b", p.name), h("small", `${Math.round(p.seconds)} s of voice${p.threshold ? (p.threshold > 0 ? ", looser" : ", stricter") : ""}`)),
      h("button.btn.ghost.sm.icon", { "aria-label": `Edit ${p.name}`, onclick: (e) => { e.stopPropagation(); card.classList.toggle("edit"); } }, icon("gear")));
    const sens = h("input", { type: "range", min: -0.12, max: 0.12, step: 0.02, value: -p.threshold, "aria-label": "Sensitivity",
      onchange: async (e) => { await patch(`/api/people/${p.id}`, { sensitivity: -Number(e.target.value) }); presenceCache = {}; loadPeople(); renderVods(); } });
    card.append(h("div.pc-edit", { onclick: (e) => e.stopPropagation() },
      h("label.field", h("span", "Name"), h("input.input", { value: p.name, onchange: async (e) => { await patch(`/api/people/${p.id}`, { name: e.target.value }); loadPeople(); } })),
      h("label.field", h("span", "How picky to be about their voice"), h("div.sens", h("small", "Looser"), sens, h("small", "Stricter"))),
      h("div.row-actions",
        h("button.btn.sm", { onclick: () => openPersonModal(p) }, icon("wave"), "Teach more"),
        h("button.btn.sm.danger", { onclick: async () => { if (confirm(`Forget ${p.name}'s voice? Their clips stay.`)) { await del(`/api/people/${p.id}`); loadPeople(); } } }, icon("trash"), "Forget"))));
    return card;
  }));
}

// ---------------------------------------------------------------- VODs
function renderVods() {
  if (!vodsEl) return;
  const items = vodItems();
  for (const id of [...selVods]) if (!items.find((s) => s.id === id)) selVods.delete(id);
  if (!items.length) {
    clear(vodsEl, h("p.cl-empty", "No VODs yet. Import a channel's past broadcasts from Twitch, or add recordings in the Library."),
      h("button.btn.sm", { onclick: openTwitch }, icon("twitch"), "Import from Twitch"));
    renderRun();
    return;
  }
  clear(vodsEl, items.map((s) => {
    const ready = s.status === "ready";
    const cb = h("input", { type: "checkbox", checked: selVods.has(s.id), disabled: !ready, "aria-label": `Use ${s.title}`,
      onchange: (e) => { e.target.checked ? selVods.add(s.id) : selVods.delete(s.id); renderRun(); } });
    const strip = h("canvas.strip", { width: 260, height: 14, title: "Where they're talking" });
    const row = h("label.vod", cb,
      h("div.vod-t", h("span.vod-n", { title: s.title }, s.title),
        h("small", ready ? [fmtTime(s.duration), s.meta.voice_windows ? "" : ", voices not analysed yet"]
          : h("span.stage", s.stage || "Indexing…")),
        ready && selPerson && strip));
    if (ready && selPerson) drawPresence(strip, s.id);
    return row;
  }));
  renderRun();
}

async function drawPresence(canvas, sid) {
  const key = `${selPerson}|${sid}`;
  let d = presenceCache[key];
  if (!d) {
    try { d = presenceCache[key] = await get(`/api/people/${selPerson}/presence?source_id=${sid}&bins=260`); } catch { return; }
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#2e2c3b";
  ctx.fillRect(0, 6, canvas.width, 2);
  if (!d.ready) return;
  const lo = d.threshold - 0.15;
  d.curve.forEach((v, i) => {
    if (v <= lo) return;
    const hgt = Math.max(1, Math.min(14, ((v - lo) / 0.4) * 14));
    ctx.fillStyle = v >= d.threshold ? "#ddb65c" : "#4a4760";
    ctx.fillRect(i, 14 - hgt, 1, hgt);
  });
  canvas.title = `${Math.round(d.seconds)} s of them talking`;
}

function toggleAllVods() {
  const ready = vodItems().filter((s) => s.status === "ready");
  if (ready.every((s) => selVods.has(s.id))) selVods.clear(); else ready.forEach((s) => selVods.add(s.id));
  renderVods();
}

// ---------------------------------------------------------------- run bar + results
function renderRun() {
  if (!runEl) return;
  const p = person();
  const n = selVods.size;
  const lenSeg = h("div.seg", LENGTH.map(([k, label, hint]) => h("button", { "aria-pressed": String(opts.length === k), title: hint,
    onclick: () => { opts.length = k; renderRun(); } }, label)));
  const perSel = h("select.input", { "aria-label": "Clips per VOD", onchange: (e) => { opts.per_vod = Number(e.target.value); } },
    [3, 6, 10, 15].map((v) => h("option", { value: v, selected: v === opts.per_vod }, `${v} per VOD`)));
  const go = h("button.btn.primary", { disabled: !p || !n || !!findJob, onclick: startFind }, icon("clips"),
    findJob ? "Finding…" : "Find clips");
  const runSel = runs.length ? h("select.input", { "aria-label": "Results", onchange: (e) => { curRun = e.target.value; selClip = null; loadClips(); } },
    runs.map((r) => h("option", { value: r.run_id, selected: r.run_id === curRun },
      `${r.person || "Someone"}: ${r.n} clip${r.n > 1 ? "s" : ""} from ${r.vods} VOD${r.vods > 1 ? "s" : ""}, ${new Date(r.created * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`))) : null;
  const filt = h("div.seg", [["", "To review"], ["kept", "Kept"], ["dismissed", "Dismissed"]].map(([k, l]) =>
    h("button", { "aria-pressed": String(statusFilter === k), onclick: () => { statusFilter = k; loadClips(); } }, l)));
  clear(runEl,
    h("div.cl-runbar",
      h("div.cl-q", p ? ["Find ", h("b", p.name), ` in ${n || "no"} VOD${n === 1 ? "" : "s"}`] : h("span.muted", "Pick a person on the left")),
      lenSeg, perSel, h("div.grow"), go),
    h("div.cl-prog#cl-prog", { hidden: !findJob }),
    runs.length ? h("div.cl-resbar", runSel, h("div.grow"), filt) : null);
  if (findJob) { const j = state.jobs.find((x) => x.id === findJob); if (j) renderProgress(j); }
}

function renderProgress(j) {
  const el = document.getElementById("cl-prog");
  if (!el) return;
  el.hidden = !(j.status === "running" || j.status === "queued");
  clear(el, h("div.progress", h("i", { style: { width: `${Math.max(2, j.progress * 100)}%` } })),
    h("div.cl-progmsg", h("span", j.message || "Waiting in line…"),
      h("button.btn.ghost.sm", { onclick: () => post(`/api/jobs/${j.id}/cancel`) }, "Stop")));
}

async function startFind() {
  try {
    const j = await post("/api/clips/find", { person_id: selPerson, source_ids: [...selVods], length: opts.length, per_vod: opts.per_vod });
    findJob = j.id;
    bus.emit("jobs:refresh");
    renderRun();
  } catch (e) { toast(e.message, true); }
}

function renderGrid() {
  if (!gridEl) return;
  if (!clipList.length) {
    clear(gridEl, h("div.empty",
      h("h2", runs.length ? "Nothing here" : "No clips yet"),
      runs.length ? "Try another filter, or run a new search." :
        "Pick a person and some VODs, then Find clips. Lorekeeper listens for their voice, scores every moment they're in for hype, laughs and pace, and has the AI pick and title the best ones."));
    return;
  }
  clear(gridEl, clipList.map((c, i) => {
    const on = selClip && selClip.id === c.id;
    return h(`button.ccard${on ? ".on" : ""}${c.status === "kept" ? ".kept" : ""}`, { onclick: () => selectClip(c), "data-i": i },
      h("div.cthumb", { style: { backgroundImage: `url(/clipthumb/${c.id})` } },
        h("span.cdur", fmtTime(c.end - c.start)),
        h("span.cscore", { title: c.ai_score ? `AI ${c.ai_score}/10` : "Sound + talk-time score" }, Math.round(c.score * 100)),
        c.exports.length ? h("span.cexp", icon("check"), c.exports.length) : null),
      h("div.ctitle", c.title),
      h("div.cmeta", `${c.source_title}, ${fmtTime(c.start)}`),
      c.why && h("div.cwhy", c.why));
  }));
}

function selectClip(c) { selClip = c; renderGrid(); renderDetail(true); }

function onKey(e) {
  if (root.hidden || e.target.closest("input, textarea, select") || document.querySelector(".modal")) return;
  if (!clipList.length) return;
  const i = selClip ? clipList.findIndex((c) => c.id === selClip.id) : -1;
  if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); selectClip(clipList[Math.min(clipList.length - 1, i + 1)]); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); selectClip(clipList[Math.max(0, i - 1)]); }
  else if (e.key === " " && selClip) { e.preventDefault(); const v = detailEl.querySelector("video"); if (v) v.paused ? v.play() : v.pause(); }
}

// ---------------------------------------------------------------- detail / export
function renderDetail(autoplay) {
  if (!detailEl) return;
  const c = selClip;
  if (!c) {
    clear(detailEl, h("div.empty", h("h2", "Pick a clip"), "Preview it, tighten the start and end, rename it, then export it wide or vertical. Arrow keys step through clips."));
    return;
  }
  const v = h("video", { src: `/media/${c.source_id}`, preload: "auto", playsInline: true });
  const frame = h("div.vframe");
  const stage = h("div.cl-stage", v, frame);
  const timeEl = h("span.time");
  const play = h("button.btn.icon", { "aria-label": "Play or pause", onclick: () => (v.paused ? v.play() : v.pause()) }, icon("play"));
  v.addEventListener("loadedmetadata", () => { v.currentTime = c.start; if (autoplay) v.play().catch(() => {}); layoutFrame(); });
  v.addEventListener("timeupdate", () => {
    if (v.currentTime >= c.end || v.currentTime < c.start - 0.5) { v.currentTime = c.start; }
    timeEl.textContent = `${fmtTime(Math.max(0, v.currentTime - c.start), true)} / ${fmtTime(c.end - c.start, true)}`;
  });
  v.addEventListener("play", () => clear(play, icon("pause")));
  v.addEventListener("pause", () => clear(play, icon("play")));
  new ResizeObserver(() => layoutFrame()).observe(stage);

  function layoutFrame() {
    const vertical = ex.layout === "vertical";
    frame.hidden = !vertical;
    const meta = sourceById(c.source_id)?.meta || {};
    const vwid = v.videoWidth || meta.width, vhei = v.videoHeight || meta.height;
    if (!vertical || !vwid) return;
    const box = stage.getBoundingClientRect();
    const ar = vwid / vhei;
    let vw = box.width, vh = box.width / ar;
    if (vh > box.height) { vh = box.height; vw = vh * ar; }
    const ox = (box.width - vw) / 2, oy = (box.height - vh) / 2;
    if (ex.vertical_mode === "blur") {
      frame.className = "vframe blur";
      Object.assign(frame.style, { left: `${ox}px`, top: `${oy}px`, width: `${vw}px`, height: `${vh}px` });
      return;
    }
    frame.className = "vframe";
    const fw = vh * 9 / 16;
    const fx = Math.max(0, Math.min(vw - fw, ex.focus * vw - fw / 2));
    Object.assign(frame.style, { left: `${ox + fx}px`, top: `${oy}px`, width: `${fw}px`, height: `${vh}px` });
  }

  const nudge = (key, d) => h("button.btn.sm", { onclick: () => setRange(key, c[key] + d) }, d > 0 ? `+${d}s` : `${d}s`);
  async function setRange(key, val) {
    try {
      const upd = await patch(`/api/clips/${c.id}`, { [key]: Math.round(val * 10) / 10 });
      Object.assign(c, upd); selClip = c;
      v.currentTime = key === "start" ? c.start : Math.max(c.start, c.end - 3);
      renderGrid(); renderTrim();
    } catch (e) { toast(e.message, true); }
  }
  const trimEl = h("div.cl-trim");
  function renderTrim() {
    clear(trimEl,
      h("div.tr-col", h("small", "Start"), h("b", fmtTime(c.start, true)), h("div", nudge("start", -1), nudge("start", 1),
        h("button.btn.ghost.sm", { title: "Start at the playhead", onclick: () => setRange("start", v.currentTime) }, "Here"))),
      h("div.tr-col", h("small", "Length"), h("b", `${(c.end - c.start).toFixed(1)} s`)),
      h("div.tr-col", h("small", "End"), h("b", fmtTime(c.end, true)), h("div", nudge("end", -1), nudge("end", 1),
        h("button.btn.ghost.sm", { title: "End at the playhead", onclick: () => setRange("end", v.currentTime) }, "Here"))));
  }
  renderTrim();

  const sig = c.signals || {};
  const pills = [
    c.ai_score && h("span.pill.ok", `AI ${c.ai_score}/10`),
    sig.share !== undefined && h("span.pill.plain", `${Math.round(sig.share * 100)}% ${c.person || "them"} talking`),
    sig.hype_z > 2 && h("span.pill.warm", "Loud reaction"),
    sig.pace > 1.2 && h("span.pill.plain", `${sig.pace.toFixed(1)}× usual pace`),
  ];
  const transcript = h("div.cl-trans", (c.transcript || "").split("\n").filter(Boolean).map((line) => {
    const m = line.match(/^\d+ \[([^\]]+)\] ([^:]+): (.*)$/);
    if (!m) return h("p", line);
    const mine = m[2] === c.person;
    return h(`p${mine ? ".mine" : ""}`, h("span.ts", m[1]), h("b", m[2]), " ", m[3].replace(/\s+\(LOUD\)$/, ""),
      /\(LOUD\)$/.test(m[3]) ? h("span.loud", "LOUD") : null);
  }));

  const layoutSeg = h("div.seg", [["landscape", "16:9", "wide"], ["vertical", "Vertical", "phone"]].map(([k, l, ic]) =>
    h("button", { "aria-pressed": String(ex.layout === k), onclick: () => { ex.layout = k; saveEx(); renderExport(); layoutFrame(); } }, icon(ic), l)));
  const exportEl = h("div.cl-export");
  function renderExport() {
    layoutSeg.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", String(["landscape", "vertical"][i] === ex.layout)));
    const vertical = ex.layout === "vertical";
    const fill = vertical && h("div.seg", [["crop", "Crop to fill"], ["blur", "Whole frame, blurred fill"]].map(([k, l]) =>
      h("button", { "aria-pressed": String(ex.vertical_mode === k), onclick: () => { ex.vertical_mode = k; saveEx(); renderExport(); layoutFrame(); } }, l)));
    const focus = vertical && ex.vertical_mode === "crop" && h("label.field", h("span", "Crop focus"),
      h("div.sens", h("small", "Left"), h("input", { type: "range", min: 0, max: 1, step: 0.01, value: ex.focus,
        oninput: (e) => { ex.focus = Number(e.target.value); layoutFrame(); }, onchange: saveEx }), h("small", "Right")));
    const capt = h("label.check", h("input", { type: "checkbox", checked: ex.captions, onchange: (e) => { ex.captions = e.target.checked; saveEx(); } }),
      h("span", "Burn in captions", h("small", "Word-by-word, the spoken word lit up")));
    const ttl = vertical && h("label.check", h("input", { type: "checkbox", checked: ex.show_title, onchange: (e) => { ex.show_title = e.target.checked; saveEx(); } }),
      h("span", "Title on top"));
    const st = state.status?.settings || {};
    const side = (st.watermark_corner || "bottom-right").split("-")[1];
    const wmCorner = vertical ? `top ${side}` : (st.watermark_corner || "bottom-right").replace("-", " ");
    const wm = st.watermark && st.watermark_text && h("p.wm-line", `Adds ${st.watermark_text} small in the ${wmCorner} corner. `,
      h("button.linkish", { onclick: () => bus.emit("go", { view: "settings" }) }, "Change"));
    clear(exportEl, h("div.ex-row", layoutSeg, fill), focus, h("div.ex-row", capt, ttl), wm,
      h("div.ex-row", h("button.btn.primary", { onclick: doExport }, "Export clip"),
        h("button.btn", { onclick: () => setStatus(c.status === "kept" ? "new" : "kept") }, icon("check"), c.status === "kept" ? "Kept" : "Keep"),
        h("button.btn.ghost", { onclick: () => setStatus(c.status === "dismissed" ? "new" : "dismissed") }, c.status === "dismissed" ? "Restore" : "Dismiss")),
      c.exports.length ? h("div.cl-exports", c.exports.slice().reverse().map((x) => {
        const name = x.path.split(/[\\/]/).pop();
        return h("div.out", h("span", { title: x.path }, icon(x.layout === "vertical" ? "phone" : "wide"), name),
          h("button.btn.sm", { onclick: () => openPlayer(`/exports/file?path=${encodeURIComponent(x.path)}`, name) }, "Play"),
          h("button.btn.sm.icon", { title: "Show in folder", onclick: () => reveal(x.path) }, icon("folder")));
      })) : null);
  }
  async function doExport() {
    try {
      const j = await post("/api/edit/export_clip", { params: { clip_id: c.id, ...ex }, title: `Export: ${c.title}` });
      toast(`Exporting ${ex.layout === "vertical" ? "vertical" : "16:9"} clip…`);
      bus.emit("jobs:refresh");
      const done = await waitForJob(j.id);
      if (done?.status === "done") { toast("Clip exported"); await loadClips(); }
      else if (done) toast(done.error || "Export stopped", true);
    } catch (e) { toast(e.message, true); }
  }
  async function setStatus(st) {
    const upd = await patch(`/api/clips/${c.id}`, { status: st });
    Object.assign(c, upd);
    if (st === "dismissed" && statusFilter !== "dismissed") {
      const i = clipList.findIndex((x) => x.id === c.id);
      clipList.splice(i, 1);
      selClip = clipList[Math.min(i, clipList.length - 1)] || null;
      renderGrid(); renderDetail(true);
      return;
    }
    renderGrid(); renderExport();
  }
  renderExport();

  const title = h("input.input.cl-title", { value: c.title, "aria-label": "Clip title",
    onchange: async (e) => { Object.assign(c, await patch(`/api/clips/${c.id}`, { title: e.target.value })); renderGrid(); } });
  clear(detailEl,
    stage,
    h("div.cl-transport", play, timeEl, h("div.grow"), h("span.muted", `${c.source_title}, ${fmtTime(c.start)}`)),
    title, trimEl,
    h("div.cl-why", pills, c.why && h("p", c.why)),
    h("h3.sub-h", "Export"), exportEl,
    h("h3.sub-h", "What was said"), transcript);
}

// ---------------------------------------------------------------- add / teach a person
function openPersonModal(teach) {
  const vids = vodItems().filter((s) => s.status === "ready");
  if (!vids.length) return toast("Add a VOD or recording with them in it first.", true);
  let sid = [...selVods][0] || vids[0].id;
  let tab = "voices";
  let voices = null;
  let busy = false;
  const picked = new Set();
  const player = h("video", { preload: "auto", style: { display: "none" } });
  let stopAt = 0;
  player.addEventListener("timeupdate", () => { if (stopAt && player.currentTime >= stopAt) { player.pause(); stopAt = 0; } });
  const playAt = (t, len = 3.5) => {
    if (!player.src.endsWith(`/media/${sid}`)) player.src = `/media/${sid}`;
    const go = () => { player.currentTime = t; stopAt = t + len; player.play().catch(() => {}); };
    player.readyState >= 1 ? go() : player.addEventListener("loadedmetadata", go, { once: true });
  };
  const nameIn = h("input.input", { placeholder: "Their name, e.g. Dave", value: teach ? teach.name : "", disabled: !!teach });
  const body = h("div.pm-body");
  const close = () => { player.pause(); m.remove(); };
  const m = h("div.modal", { onclick: (e) => e.target === m && close() },
    h("div.card.pm-card",
      h("header", h("b", teach ? `Teach Lorekeeper more of ${teach.name}'s voice` : "Add a person"),
        h("button.btn.ghost.sm.icon", { onclick: close, "aria-label": "Close" }, icon("x"))),
      h("p.muted", { style: { margin: "0 0 12px" } }, teach ? "Pick more moments of them talking, ideally from another VOD or a different mic setup." :
        "Lorekeeper recognises people by voice. Pick a VOD they're in, then either let it sort out the voices, or tick a few of their lines."),
      h("div.pm-top", !teach && h("label.field", h("span", "Name"), nameIn),
        h("label.field", h("span", "VOD or recording with them in it"),
          h("select.input", { onchange: (e) => { sid = e.target.value; voices = null; picked.clear(); render(); } },
            vids.map((s) => h("option", { value: s.id, selected: s.id === sid }, s.title))))),
      h("div.tabs", { role: "tablist" },
        h("button", { "aria-selected": "true", "data-t": "voices", onclick: () => { tab = "voices"; render(); } }, "Voices in this VOD"),
        h("button", { "aria-selected": "false", "data-t": "lines", onclick: () => { tab = "lines"; render(); } }, "Pick their lines")),
      body, player));
  document.body.append(m);
  render();

  const needName = () => { if (teach) return teach.name; const n = nameIn.value.trim(); if (!n) { nameIn.focus(); toast("Give them a name first.", true); } return n; };

  async function render() {
    m.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.t === tab)));
    if (tab === "voices") {
      if (busy) return;
      if (!voices) {
        clear(body, h("div.pm-find", h("p", "Lorekeeper groups every stretch of talking in this VOD by voice. Listen to a few samples and click the one that's them."),
          h("button.btn.primary", { onclick: findVoices }, icon("wave"), "Find the voices")));
        return;
      }
      if (!voices.length) { clear(body, h("p.muted", "Couldn't find distinct voices here. Try picking their lines instead.")); return; }
      clear(body, h("div.voices", voices.map((v) => h("div.voice",
        h("div.v-top", h("b", v.label), h("span.muted", v.solo ? `${fmtTime(v.seconds)} of talking, the only voice here` : `${fmtTime(v.seconds)} of talking, ${Math.round(v.share * 100)}%`),
          v.match && h("span.pill.ok", `Sounds like ${v.match.name}`)),
        h("div.v-samples", v.samples.map((t) => h("button.btn.sm", { onclick: () => playAt(t) }, icon("play"), fmtTime(t)))),
        h("button.btn.sm.primary", { onclick: () => useVoice(v) }, teach ? `Add to ${teach.name}` : "This is them")))));
    } else {
      clear(body, h("p.muted", "Loading transcript…"));
      let tr;
      try { tr = await get(`/api/sources/${sid}/transcript`); } catch { clear(body, h("p.muted", "No transcript for this one.")); return; }
      const secs = () => [...picked].reduce((a, i) => a + (tr.segments[i].end - tr.segments[i].start), 0);
      const counter = h("span.muted");
      const upd = () => { counter.textContent = `${Math.round(secs())} s picked${secs() < 10 ? ", aim for 10 or more" : ""}`; };
      upd();
      clear(body, h("div.lines", tr.segments.map((sg, i) => h("label.line",
        h("input", { type: "checkbox", checked: picked.has(i), onchange: (e) => { e.target.checked ? picked.add(i) : picked.delete(i); upd(); } }),
        h("button.ts", { type: "button", onclick: (e) => { e.preventDefault(); playAt(sg.start, sg.end - sg.start); } }, fmtTime(sg.start)),
        h("span", sg.text)))),
      h("div.row-actions", counter, h("div.grow"), h("button.btn.primary", { onclick: saveLines }, teach ? "Add these lines" : "Save voice")));
    }
  }
  async function findVoices() {
    busy = true;
    const prog = h("div.progress", h("i"));
    const msg = h("p.muted", "Listening…");
    clear(body, msg, prog);
    try {
      const j = await post("/api/voices/discover", { source_id: sid });
      const done = await waitForJob(j.id, (jj) => { prog.firstChild.style.width = `${jj.progress * 100}%`; msg.textContent = jj.message || "Listening…"; });
      voices = done.status === "done" ? done.result : [];
      if (done.status !== "done") toast(done.error || "Couldn't analyse that VOD", true);
    } catch (e) { toast(e.message, true); voices = []; }
    busy = false;
    render();
  }
  async function useVoice(v) {
    const name = needName(); if (!name) return;
    try {
      if (teach) { await waitForJob((await post(`/api/people/${teach.id}/teach`, { source_id: sid, windows: v.windows })).id); toast(`${teach.name}'s voiceprint got sharper.`); }
      else { const p = await post("/api/people/from-voice", { source_id: sid, windows: v.windows, name }); selPerson = p.id; toast(`Learned ${p.name}'s voice from ${fmtTime(p.seconds)} of talking.`); }
      presenceCache = {}; close(); loadPeople(); renderVods();
    } catch (e) { toast(e.message, true); }
  }
  async function saveLines() {
    const name = needName(); if (!name) return;
    const tr = await get(`/api/sources/${sid}/transcript`);
    const samples = [...picked].sort((a, b) => a - b).map((i) => ({ source_id: sid, start: tr.segments[i].start, end: tr.segments[i].end }));
    if (!samples.length) return toast("Tick some of their lines first.", true);
    try {
      const j = teach ? await post(`/api/people/${teach.id}/teach`, { samples }) : await post("/api/people", { name, samples });
      const done = await waitForJob(j.id);
      if (done.status !== "done") return toast(done.error || "Couldn't learn that voice", true);
      if (!teach) selPerson = done.result.id;
      toast(teach ? `${teach.name}'s voiceprint got sharper.` : `Learned ${name}'s voice.`);
      presenceCache = {}; close(); loadPeople(); renderVods();
    } catch (e) { toast(e.message, true); }
  }
}

// ---------------------------------------------------------------- Twitch import
function openTwitch() {
  let vods = [];
  const chosen = new Map();     // id -> {start, end}
  let quality = "720";
  const input = h("input.input", { placeholder: "Channel name or link, or a VOD link", style: { flex: 1 },
    onkeydown: (e) => e.key === "Enter" && load() });
  const listEl = h("div.tw-list");
  const foot = h("div.tw-foot");
  const close = () => m.remove();
  const m = h("div.modal", { onclick: (e) => e.target === m && close() },
    h("div.card.tw-card",
      h("header", icon("twitch", "tw-ic"), h("b", "Import VODs from Twitch"), h("button.btn.ghost.sm.icon", { onclick: close, "aria-label": "Close" }, icon("x"))),
      h("div.tw-search", input, h("button.btn.primary", { onclick: load }, "Show VODs")),
      listEl, foot));
  document.body.append(m);
  input.focus();
  renderList();

  async function load() {
    const q = input.value.trim();
    if (!q) return;
    clear(listEl, h("p.muted", "Asking Twitch…"));
    try {
      const r = await post("/api/twitch/vods", { channel: q, limit: 20 });
      vods = r.vods;
      chosen.clear();
      if (r.single) chosen.set(vods[0].id, {});
      renderList();
    } catch (e) { clear(listEl, h("p.err", e.message)); }
  }
  function renderList() {
    if (!vods.length) {
      clear(listEl, h("p.muted.tw-hint", "Lists a channel's past broadcasts. Downloads go into your library and get transcribed and voice-indexed automatically. ",
        "Subscriber-only VODs can't be downloaded, and Twitch deletes VODs after 7 to 60 days."));
    } else {
      clear(listEl, vods.map((v) => {
        const on = chosen.has(v.id);
        const rng = chosen.get(v.id) || {};
        const row = h(`div.tw-vod${on ? ".on" : ""}`,
          h("input", { type: "checkbox", checked: on, "aria-label": `Download ${v.title}`,
            onchange: (e) => { e.target.checked ? chosen.set(v.id, {}) : chosen.delete(v.id); renderList(); } }),
          h("div.tw-thumb", { style: v.thumbnail ? { backgroundImage: `url(${v.thumbnail})` } : {} }, v.duration && h("span.cdur", fmtTime(v.duration))),
          h("div.tw-t", h("b", { title: v.title }, v.title),
            h("small", [v.date && `${v.date.slice(0, 4)}-${v.date.slice(4, 6)}-${v.date.slice(6)}`, v.views && `${Number(v.views).toLocaleString()} views`].filter(Boolean).join(", ")),
            on && h("div.tw-range", h("span", "Only part:"),
              h("input.input", { placeholder: "from 1:30:00", value: rng.from || "", onchange: (e) => { rng.from = e.target.value; chosen.set(v.id, rng); renderFoot(); } }),
              h("input.input", { placeholder: "to 2:15:00", value: rng.to || "", onchange: (e) => { rng.to = e.target.value; chosen.set(v.id, rng); renderFoot(); } }))));
        return row;
      }));
    }
    renderFoot();
  }
  const parse = (s) => { if (!s) return null; const p = s.trim().split(":").map(Number); return p.some(isNaN) ? null : p.reduce((a, x) => a * 60 + x, 0); };
  function renderFoot() {
    let secs = 0;
    for (const [id, r] of chosen) {
      const v = vods.find((x) => x.id === id);
      const a = parse(r.from) ?? 0, b = parse(r.to) ?? (v?.duration || 0);
      secs += Math.max(0, b - a);
    }
    const size = secs * RATE[quality] * 1e6 / 8;
    clear(foot,
      h("label.field", h("span", "Quality"), h("select.input", { onchange: (e) => { quality = e.target.value; renderFoot(); } },
        [["1080", "1080p60 (source)"], ["720", "720p (recommended)"], ["480", "480p"], ["360", "360p (smallest)"]].map(([k, l]) => h("option", { value: k, selected: k === quality }, l)))),
      h("span.muted", chosen.size ? `${chosen.size} VOD${chosen.size > 1 ? "s" : ""}${secs ? `, about ${fmtBytes(size)}` : ""}` : ""),
      h("div.grow"),
      h("button.btn.primary", { disabled: !chosen.size, onclick: go }, icon("twitch"), `Download ${chosen.size || ""}`));
  }
  async function go() {
    const items = [...chosen].map(([id, r]) => {
      const v = vods.find((x) => x.id === id);
      return { id, url: v.url, title: v.title, start: parse(r.from), end: parse(r.to) };
    });
    try {
      await post("/api/twitch/download", { vods: items, quality });
      toast(`Downloading ${items.length} VOD${items.length > 1 ? "s" : ""}. Progress is under Jobs; they appear here once indexed.`, false, 7000);
      bus.emit("jobs:refresh");
      close();
    } catch (e) { toast(e.message, true); }
  }
}
