import { bus, clear, del, desktop, fmtBytes, fmtTime, get, glyphFor, h, icon, kindLabel, patch, pickAndAddFiles,
  post, sourceById, state, toast } from "./core.js";
import { editableTranscript } from "./transcript.js";

let root, rowsEl, detailEl, countEl, scopeBtn;
let selected = null;
let filter = "all";
let query = "";
let detailKey = "";

export function mount(el) {
  root = el;
  const urlInput = h("input.input", { placeholder: "Paste a link (web page, PDF, image or media URL)", "aria-label": "Link to add" });
  const addUrl = async () => {
    const url = urlInput.value.trim();
    if (!url) return;
    try {
      const r = await post("/api/sources/url", { url });
      urlInput.value = "";
      toast(`Fetching ${r.added.length > 1 ? r.added.length + " links" : "the page"}…`);
      bus.emit("sources:refresh");
    } catch (e) { toast(e.message, true); }
  };
  urlInput.addEventListener("keydown", (e) => e.key === "Enter" && addUrl());
  countEl = h("span.sub");
  scopeBtn = h("button.btn", { onclick: () => bus.emit("go", { view: "ask" }) });

  el.append(
    h("header.view-head",
      h("h1", "Library"), countEl, h("div.grow"),
      scopeBtn,
      h("div.lib-add", urlInput, h("button.btn", { onclick: addUrl }, "Add link"),
        h("button.btn.primary", { onclick: pickAndAddFiles }, icon("plus"), "Add files"))),
    h("div.lib-body",
      h("div.lib-list",
        h("div.lib-tools",
          h("input.input", { placeholder: "Filter by name", "aria-label": "Filter by name",
            oninput: (e) => { query = e.target.value.toLowerCase(); renderRows(); } }),
          filterSeg()),
        rowsEl = h("div.lib-rows", { role: "list" })),
      detailEl = h("div.detail")));
  bus.on("sources", () => { renderRows(); renderDetail(); });
  renderRows();
  renderDetail();
}

export function show(arg) {
  if (arg && arg.select) { selected = arg.select; detailKey = ""; renderRows(); renderDetail(); }
}

function filterSeg() {
  const opts = [["all", "All"], ["video", "Video"], ["audio", "Audio"], ["image", "Images"], ["document", "Docs"], ["url", "Links"]];
  const seg = h("div.seg", { role: "group", "aria-label": "Filter by type" });
  for (const [k, label] of opts) {
    seg.append(h("button", {
      "aria-pressed": String(k === filter),
      onclick: () => { filter = k; seg.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", String(opts[i][0] === k))); renderRows(); },
    }, label));
  }
  return seg;
}

function metaLine(s) {
  const m = s.meta || {};
  if (s.status === "processing" || s.status === "queued") {
    return h("span.stage", s.stage || (s.status === "queued" ? "Waiting in line" : "Working"));
  }
  if (s.status === "error") return h("span.err", s.error || "Failed");
  const bits = [];
  if (s.duration) bits.push(fmtTime(s.duration));
  if (m.width && m.height) bits.push(`${m.width}×${m.height}`);
  if (m.pages) bits.push(`${m.pages} ${m.page_label || "page"}${m.pages > 1 ? "s" : ""}`);
  if (s.kind === "url" && s.url) bits.push(new URL(s.url).hostname.replace(/^www\./, ""));
  if (m.language && m.language !== "?") bits.push(m.language.toUpperCase());
  bits.push(`${s.n_chunks} chunk${s.n_chunks === 1 ? "" : "s"} indexed`);
  return bits.join(", ");
}

function renderRows() {
  if (!rowsEl) return;
  const top = rowsEl.scrollTop;
  const list = state.sources.filter((s) => (filter === "all" || s.kind === filter) && (!query || s.title.toLowerCase().includes(query)));
  const n = state.sources.length;
  countEl.textContent = n ? `${n} item${n > 1 ? "s" : ""}` : "";
  scopeBtn.hidden = !state.scope.size;
  scopeBtn.textContent = `Ask about ${state.scope.size} selected`;

  if (!n) {
    clear(rowsEl, h("div.empty",
      h("h2", "Your library is empty"),
      h("p", "Drop videos, recordings, screenshots, PDFs or Word docs anywhere in this window, or paste a link above. ",
        "Lorekeeper transcribes speech, describes images and frames, and indexes every word so you can ask about it."),
      h("button.btn.primary", { onclick: pickAndAddFiles }, icon("plus"), "Add files")));
    return;
  }
  if (!selected || !sourceById(selected)) selected = list[0] ? list[0].id : null;
  clear(rowsEl, list.map((s) => {
    const check = h("input", {
      type: "checkbox", checked: state.scope.has(s.id), title: "Include in Ask scope", "aria-label": `Include ${s.title} when asking`,
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? state.scope.add(s.id) : state.scope.delete(s.id); renderRows(); bus.emit("scope"); },
    });
    const busy = s.status === "processing" || s.status === "queued";
    return h(`div.row.kind-${s.kind}${s.id === selected ? ".sel" : ""}`, {
      role: "listitem", tabindex: "0",
      onclick: () => { selected = s.id; renderRows(); renderDetail(); },
      onkeydown: (e) => { if (e.key === "Enter") { selected = s.id; renderRows(); renderDetail(); } },
    },
      check, glyphFor(s),
      h("div.t", h("div.title", { title: s.title }, s.title), h("div.meta", metaLine(s)),
        busy && h("div.progress", h("i", { style: { width: `${Math.max(3, s.progress * 100)}%` } }))),
      h("div.right", s.status === "error"
        ? h("button.btn.sm", { onclick: (e) => { e.stopPropagation(); reindex(s.id); } }, "Retry")
        : kindLabel[s.kind]));
  }));
  rowsEl.scrollTop = top;
}

async function reindex(id) {
  try { await post(`/api/sources/${id}/reindex`); bus.emit("sources:refresh"); } catch (e) { toast(e.message, true); }
}

async function renderDetail() {
  if (!detailEl) return;
  const s = sourceById(selected);
  if (!s) { detailKey = ""; clear(detailEl); return; }
  const key = `${s.id}|${s.status}|${s.n_chunks}|${s.title}`;
  if (key === detailKey) return;
  detailKey = key;
  let d;
  try { d = await get(`/api/sources/${s.id}`); } catch { return; }
  const m = d.meta || {};

  const title = h("input.d-title", { value: d.title, "aria-label": "Title", spellcheck: false,
    onchange: async (e) => { await patch(`/api/sources/${d.id}`, { title: e.target.value }); bus.emit("sources:refresh"); },
    onkeydown: (e) => e.key === "Enter" && e.target.blur() });
  title.style.width = "100%";
  title.style.background = "none";

  const facts = [];
  const fact = (k, v) => v && facts.push(h("span", `${k} `, h("b", v)));
  fact("Type", kindLabel[d.kind]);
  fact("Length", d.duration && fmtTime(d.duration));
  fact("Frame", m.width && `${m.width}×${m.height}${m.fps ? " @ " + Math.round(m.fps) + " fps" : ""}`);
  fact("Codecs", [m.vcodec, m.acodec].filter(Boolean).join(" + "));
  fact("Pages", m.pages);
  fact("Size", fmtBytes(d.size));
  fact("Speech", m.language && m.language !== "?" && `${m.language.toUpperCase()}, ${m.words || 0} words`);
  fact("Indexed", `${d.chunks.length} chunks`);
  fact("Embeddings", m.embed_model && `${m.embed_model} (${m.embed_dim}-d)`);

  let media = null;
  let preview = null;
  if (d.kind === "video") {
    media = h("video", { src: `/media/${d.id}`, controls: true, preload: "metadata" });
    preview = h("div.preview", media);
  } else if (d.kind === "audio") {
    media = h("audio", { src: `/media/${d.id}`, controls: true, preload: "metadata" });
    preview = h("div.preview.audio", h("div.wave", { style: { backgroundImage: `url(/waveform/${d.id})` } }), media);
  } else if (d.kind === "image") {
    preview = h("div.preview", h("img", { src: `/media/${d.id}`, alt: d.title }));
  }
  const seek = (t) => { if (media) { media.currentTime = t; media.play(); } };

  const warns = [];
  if (d.status === "error") warns.push(h("div.warn.bad", d.error));
  for (const w of d.warnings || []) warns.push(h("div.warn", w));

  const isAV = d.kind === "video" || d.kind === "audio";
  const actions = h("div.actions",
    d.status === "ready" && h("button.btn.primary", {
      onclick: () => { state.scope = new Set([d.id]); bus.emit("scope"); bus.emit("go", { view: "ask" }); },
    }, icon("ask"), "Ask about this"),
    isAV && d.status === "ready" && h("button.btn", { onclick: () => bus.emit("go", { view: "studio", arg: { source: d.id } }) }, icon("studio"), "Edit in Studio"),
    (isAV || d.kind === "image") && d.status === "ready" && h("button.btn", { onclick: () => bus.emit("go", { view: "timeline", arg: { add: d.id } }) }, icon("timeline"), "Add to timeline"),
    d.kind === "url" && h("button.btn", { onclick: () => window.open(d.url, "_blank") }, icon("url"), "Open page"),
    h("button.btn", { onclick: () => reindex(d.id), title: "Run extraction, transcription and embedding again" }, icon("refresh"), "Re-index"),
    h("button.btn.danger", {
      onclick: async () => {
        if (!confirm(`Remove "${d.title}" from the library?${d.owned ? " Its stored copy will be deleted too." : " Your original file stays where it is."}`)) return;
        await del(`/api/sources/${d.id}`);
        selected = null; bus.emit("sources:refresh");
      },
    }, icon("trash"), "Remove"));

  // tabs
  const tabs = [];
  if (d.transcript) tabs.push(["transcript", "Transcript", () => transcriptTab(d, seek)]);
  if (d.kind === "image" && m.description) tabs.push(["desc", "What the vision model saw", () => h("div.chunk", h("div.cx", m.description))]);
  if (d.frames && d.frames.length) tabs.push(["frames", `Frames (${d.frames.length})`, () => framesTab(d, seek)]);
  tabs.push(["chunks", "What's indexed", () => chunksTab(d, seek)]);
  const pane = h("div");
  const tabBar = h("div.tabs", { role: "tablist" });
  const pick = (k) => {
    tabBar.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.k === k)));
    clear(pane, tabs.find((t) => t[0] === k)[2]());
  };
  for (const [k, label] of tabs) tabBar.append(h("button", { role: "tab", "data-k": k, onclick: () => pick(k) }, label));

  clear(detailEl, title, h("div.facts", facts), preview, warns, actions,
    d.status === "ready" || d.chunks.length ? [tabBar, pane] : h("p.muted", "Details appear once indexing finishes."));
  if (tabs.length && (d.status === "ready" || d.chunks.length)) pick(tabs[0][0]);
}

function transcriptTab(d, seek) {
  return h("div", h("p.muted.tr-hint", "Click any line to correct it. Enter saves, Esc cancels. Captions, subtitles and search use the fix."),
    editableTranscript(d.id, d.transcript.segments, { seek }));
}

function framesTab(d, seek) {
  const captions = {};
  for (const c of d.chunks) if (c.kind === "frame") captions[Math.round(c.start * 1000)] = c.text.replace(/^\[On screen at [^\]]+\]\s*/, "");
  return h("div.frames", d.frames.map((f) => {
    const ms = parseInt(f, 10);
    const cap = captions[ms] || "";
    return h("figure", { onclick: () => seek(ms / 1000), title: cap },
      h("img", { src: `/frames/${d.id}/${f}`, loading: "lazy", alt: cap.slice(0, 120) }),
      h("figcaption", fmtTime(ms / 1000)));
  }));
}

function chunksTab(d, seek) {
  if (!d.chunks.length) return h("p.muted", "Nothing was extracted from this item.");
  const label = { text: "Text", transcript: "Speech", frame: "On screen", image: "Image description", ocr: "OCR" };
  return h("div", h("p.muted", { style: { marginTop: 0 } },
    "These are the exact passages the search engine sees. Each one is indexed twice: as words (BM25) and as a meaning vector (embedding)."),
  d.chunks.map((c) => h("div.chunk",
    h("div.ck", label[c.kind] || c.kind,
      c.start !== null && c.start !== undefined ? [" at ", h("button.ts", { onclick: () => seek(c.start) }, fmtTime(c.start))] : "",
      c.page ? `, page ${c.page}` : "", c.embedded ? "" : ", keyword only"),
    h("div.cx", c.text))));
}
