import { bus, clear, fmtTime, h, icon, sourceById, state, toast } from "./core.js";

let threadEl, innerEl, sourcesEl, input, scopeEl, sendBtn;
const items = [];          // {q, text, hits, telemetry, stats, error, done}
let active = -1;           // which answer the sources panel is showing
let focusTag = null;
let streaming = false;

export function mount(el) {
  input = h("textarea", { rows: 1, placeholder: "Ask about anything in your library…", "aria-label": "Your question",
    oninput: autosize, onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } } });
  sendBtn = h("button.btn.primary", { onclick: () => send() }, "Ask");
  scopeEl = h("div.scope");
  el.append(
    h("header.view-head", h("h1", "Ask"), h("span.sub", "Answers come only from your library, with sources"), h("div.grow"),
      h("button.btn", { onclick: reset }, icon("plus"), "New conversation")),
    h("div.ask-body",
      h("div.thread-wrap",
        threadEl = h("div.thread", innerEl = h("div.thread-inner")),
        h("div.composer", h("div.composer-inner", h("div.box", input, sendBtn), scopeEl))),
      sourcesEl = h("aside.sources", { "aria-label": "Sources" })));
  bus.on("scope", renderScope);
  bus.on("sources", renderScope);
  renderThread();
  renderSources();
  renderScope();
}

export function show() { renderScope(); setTimeout(() => input.focus(), 50); }

function autosize() { input.style.height = "auto"; input.style.height = Math.min(200, input.scrollHeight) + "px"; }

function reset() {
  if (streaming) return;
  items.length = 0; active = -1; focusTag = null;
  renderThread(); renderSources(); input.focus();
}

function renderScope() {
  if (!scopeEl) return;
  const ready = state.sources.filter((s) => s.status === "ready").length;
  if (state.scope.size) {
    const names = [...state.scope].map((id) => sourceById(id)?.title).filter(Boolean);
    clear(scopeEl, "Searching ", h("b", names.length === 1 ? names[0] : `${names.length} selected items`),
      h("button.btn.ghost.sm", { onclick: () => { state.scope.clear(); bus.emit("scope"); bus.emit("sources", state.sources); } }, "Search everything instead"));
  } else {
    clear(scopeEl, "Searching ", h("b", `your whole library (${ready} item${ready === 1 ? "" : "s"})`),
      h("span.muted", " — tick items in the Library to narrow it"));
  }
}

// ---------------------------------------------------------------- markdown (small, safe)
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function inline(s) {
  s = esc(s);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|\W)_([^_\n]+)_(?=\W|$)/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
  s = s.replace(/[[(]((?:S\d+\s*(?:[,;]|and)?\s*)+)[\])]/g, (m, inner) =>
    inner.split(/[,;]|and/).map((t) => t.trim()).filter(Boolean)
      .map((t) => `<button class="cite" data-tag="${t}" title="Source ${t.slice(1)}">${t.slice(1)}</button>`).join(""));
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
  return s;
}

function blocks(md) {
  const out = [];
  const lines = md.split("\n");
  let para = [], list = null, fence = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (fence !== null) {
      if (/^```/.test(line)) { out.push(`<pre><code>${esc(fence.join("\n"))}</code></pre>`); fence = null; } else fence.push(raw);
      continue;
    }
    if (/^```/.test(line)) { flushPara(); flushList(); fence = []; continue; }
    const hd = line.match(/^#{3,6}\s+(.*)/);
    if (hd) { flushPara(); flushList(); out.push(`<p><strong>${inline(hd[1])}</strong></p>`); continue; }
    const ul = line.match(/^\s*[-*•]\s+(.*)/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (ul || ol) {
      flushPara();
      const tag = ul ? "ul" : "ol";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push((ul || ol)[1]);
      continue;
    }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if (list && /^\s{2,}/.test(raw)) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
    flushList();
    para.push(line.trim());
  }
  if (fence !== null) out.push(`<pre><code>${esc(fence.join("\n"))}</code></pre>`);
  flushPara(); flushList();
  return out.join("");
}

const SECTION = [
  [/answer|tl;?dr|short version/i, "s-answer", "bolt", "The answer"],
  [/how/i, "s-how", "gear", "The how"],
  [/why/i, "s-why", "why", "The why"],
  [/deep|cut|nerd|bonus|insight/i, "s-deep", "d20", "Deep cut"],
];

function renderLore(text, live) {
  const parts = [];
  let cur = { head: null, body: [] };
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*#{1,2}\s+(.+?)\s*#*\s*$/) || line.match(/^\s*\*\*(The Answer|The How|The Why|Deep Cut)\*\*:?\s*$/i);
    if (m) { if (cur.head || cur.body.join("").trim()) parts.push(cur); cur = { head: m[1].replace(/[*_]/g, ""), body: [] }; }
    else cur.body.push(line);
  }
  parts.push(cur);
  const wrap = h("div.lore");
  parts.forEach((p, i) => {
    const kind = SECTION.find(([re]) => p.head && re.test(p.head)) || (i === 0 && !p.head ? SECTION[0] : null);
    const sec = h(`section.${kind ? kind[1] : "s-other"}`);
    if (p.head || kind) sec.append(h("h3", kind ? icon(kind[2]) : null, kind ? kind[3] : p.head));
    const body = h("div.body", { html: blocks(p.body.join("\n")) });
    if (live && i === parts.length - 1) body.classList.add("caret");
    sec.append(body);
    wrap.append(sec);
  });
  return wrap;
}

// ---------------------------------------------------------------- thread
function renderThread() {
  if (!items.length) {
    const starters = [
      "Summarize everything in my library in five sharp bullet points",
      "What are the biggest ideas across these sources, and how do they connect?",
      "Explain the trickiest concept in here like I'm a curious engineer",
      "What questions do these sources leave unanswered?",
    ];
    clear(innerEl, h("div.hero-ask",
      h("h2", "Ask your library anything"),
      h("p", "Every answer is built only from what you've added: transcripts, documents, web pages and what the vision model saw in your images and video frames. You get the answer, how it works, why it's that way, and one deep cut, each claim tagged with its source."),
      h("div.starters", starters.map((s) => h("button", { onclick: () => { input.value = s; send(); } }, s)))));
    return;
  }
  clear(innerEl, items.map((it, i) => [
    h("div.q", it.q),
    h("div", { "data-i": i, onclick: (e) => onAnswerClick(e, i) },
      it.error ? h("div.ans-err", it.error)
        : it.text ? renderLore(it.text, !it.done)
          : h("p.muted", { style: { paddingLeft: "22px" } }, it.hits ? `Found ${it.hits.length} passages, thinking…` : "Searching your library…"),
      it.done && telemetryLine(it)),
  ]));
}

function telemetryLine(it) {
  const t = it.telemetry || {}, s = it.stats || {};
  const bits = [];
  if (it.hits) bits.push(h("span", h("b", it.hits.length), " sources"));
  if (t.retrieval_ms !== undefined) bits.push(h("span", "retrieval ", h("b", `${t.retrieval_ms} ms`)));
  if (s.model) bits.push(h("span", h("b", s.model)));
  if (s.tok_per_s) bits.push(h("span", h("b", `${s.tok_per_s} tok/s`)));
  if (s.ttft_ms) bits.push(h("span", "first token ", h("b", `${(s.ttft_ms / 1000).toFixed(1)} s`)));
  const copy = h("button.btn.ghost.sm", { onclick: (e) => { e.stopPropagation(); navigator.clipboard?.writeText(it.text); toast("Answer copied"); } }, "Copy");
  return h("div.telemetry", bits, copy);
}

function onAnswerClick(e, i) {
  const cite = e.target.closest(".cite");
  if (active !== i) { active = i; focusTag = null; }
  if (cite) focusTag = cite.dataset.tag;
  syncCites();
  renderSources();
}

let raf = 0;
function scheduleRender() {
  if (raf) return;
  raf = requestAnimationFrame(() => {
    raf = 0;
    const nearBottom = threadEl.scrollHeight - threadEl.scrollTop - threadEl.clientHeight < 140;
    renderThread();
    if (nearBottom) threadEl.scrollTop = threadEl.scrollHeight;
  });
}

async function send(text) {
  const q = (text ?? input.value).trim();
  if (!q || streaming) return;
  input.value = ""; autosize();
  const history = [];
  for (const it of items) if (it.done && !it.error) history.push({ role: "user", content: it.q }, { role: "assistant", content: it.text });
  const it = { q, text: "", hits: null, telemetry: null, stats: null, error: null, done: false };
  items.push(it);
  active = items.length - 1; focusTag = null;
  streaming = true; sendBtn.disabled = true;
  renderThread(); renderSources();
  threadEl.scrollTop = threadEl.scrollHeight;
  try {
    const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, history, source_ids: state.scope.size ? [...state.scope] : null }) });
    if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail; } catch { /* */ } throw new Error(m); }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "sources") { it.hits = ev.hits; it.telemetry = ev.telemetry; renderSources(); }
        else if (ev.type === "token") it.text += ev.text;
        else if (ev.type === "done") it.stats = ev.stats;
        else if (ev.type === "error") it.error = ev.error;
        scheduleRender();
      }
    }
  } catch (e) {
    it.error = e.message;
  }
  it.done = true; streaming = false; sendBtn.disabled = false;
  renderThread(); renderSources();
  threadEl.scrollTop = threadEl.scrollHeight;
}

// ---------------------------------------------------------------- sources panel
function renderSources() {
  const it = items[active];
  if (!it || !it.hits) {
    clear(sourcesEl, h("h2", "Sources"),
      h("p.hint", "When you ask, the passages Lorekeeper pulled from your library appear here, with how each one ranked."),
      howItWorks(null));
    return;
  }
  const maxRrf = Math.max(...it.hits.map((x) => x.rrf), 1e-9);
  const cards = it.hits.map((hit) => {
    const on = focusTag === hit.tag;
    const where = hit.start !== null && hit.start !== undefined && (hit.source_kind === "video" || hit.source_kind === "audio")
      ? fmtTime(hit.start) + (hit.kind === "frame" ? " on screen" : "")
      : hit.page ? `page ${hit.page}` : "";
    const card = h(`div.src-card${on ? ".on" : ""}`, {
      onclick: () => { focusTag = on ? null : hit.tag; renderSources(); syncCites(); },
    },
      h("div.top", h("span.tag", hit.tag.slice(1)), h("span.name", { title: hit.title }, hit.title), where && h("span.where", where)),
      h("div.ex", hit.text),
      h("div.ranks",
        "Keyword", bar("bm", hit.bm25_rank ? Math.max(6, 100 - (hit.bm25_rank - 1) * 9) : 0, hit.bm25_rank ? `#${hit.bm25_rank}` : "no match"),
        "Meaning", bar("vec", hit.dense_rank ? Math.max(6, 100 - (hit.dense_rank - 1) * 9) : 0,
          hit.dense_rank ? `#${hit.dense_rank}${hit.cosine !== null ? `, cos ${hit.cosine.toFixed(2)}` : ""}` : "no vector"),
        "Fused", bar("rrf", (hit.rrf / maxRrf) * 100, hit.rrf.toFixed(4))));
    if (on) card.append(peek(hit));
    return card;
  });
  clear(sourcesEl, h("h2", `Sources for this answer`),
    h("p.hint", "Click a gold number in the answer, or a card, to inspect the passage. Video and audio sources play from the cited moment."),
    cards, howItWorks(it));
  if (focusTag) sourcesEl.querySelector(".src-card.on")?.scrollIntoView({ block: "nearest" });
}

function syncCites() {
  innerEl.querySelectorAll(".cite").forEach((c) =>
    c.classList.toggle("on", c.dataset.tag === focusTag && c.closest("[data-i]")?.dataset.i === String(active)));
}

function bar(cls, pct, label) {
  return h("div", { style: { display: "flex", gap: "8px", alignItems: "center" } },
    h(`div.bar.${cls}`, { style: { flex: "1" } }, h("i", { style: { width: `${pct}%` } })),
    h("span", { style: { minWidth: "84px" } }, label));
}

function peek(hit) {
  const src = sourceById(hit.source_id);
  if (!src) return null;
  if (hit.source_kind === "video" || hit.source_kind === "audio") {
    const tag = hit.source_kind === "video" ? "video" : "audio";
    const m = h(tag, { src: `/media/${hit.source_id}`, controls: true, preload: "metadata", onclick: (e) => e.stopPropagation() });
    m.addEventListener("loadedmetadata", () => { m.currentTime = Math.max(0, (hit.start || 0) - 0.3); }, { once: true });
    return h("div.peek", m, h("button.btn.ghost.sm", { onclick: (e) => { e.stopPropagation(); bus.emit("go", { view: "library", arg: { select: hit.source_id } }); } }, "Open in Library"));
  }
  if (hit.source_kind === "image") return h("div.peek", h("img", { src: `/media/${hit.source_id}`, alt: hit.title }));
  return h("div.peek", h("button.btn.ghost.sm", { onclick: (e) => { e.stopPropagation(); bus.emit("go", { view: "library", arg: { select: hit.source_id } }); } }, "Open in Library"));
}

function howItWorks(it) {
  if (!it || !it.telemetry) {
    return h("div.how-retrieval",
      h("b", "How retrieval works. "),
      "Your question runs two searches at once. A keyword search (BM25 over SQLite's FTS5 index) catches exact names, numbers and jargon. ",
      "A meaning search compares your question's embedding vector with every passage's, catching paraphrases. ",
      "Reciprocal Rank Fusion then merges both lists using only rank positions, score = Σ 1/(60 + rank), so neither search has to trust the other's scoring.");
  }
  const t = it.telemetry, s = it.stats || {};
  return h("div.how-retrieval",
    h("p", { style: { margin: "0 0 8px" } }, h("b", "Under the hood. "),
      t.retrieval_query && t.retrieval_query !== t.query ? ["Follow-up detected, so the search also used your previous question. "] : "",
      t.sparse ? ["Keyword query ", h("code", t.fts_query || ""), ". "] : "Keyword search unavailable. ",
      t.dense ? `Compared against ${t.vectors} vectors (${t.dim}-dimensional). ` : (t.dense_error ? `Meaning search skipped: ${t.dense_error}. ` : ""),
      `${t.candidates || 0} candidates fused with RRF (k=${t.rrf_k}), top ${it.hits.length} kept, at most 4 per item so one long file can't drown out the rest.`),
    s.tokens ? h("p", { style: { margin: 0 } },
      `${s.model} read ${s.prompt_tokens?.toLocaleString() || "?"} prompt tokens` +
      (s.prompt_tok_per_s ? ` (${Math.round(s.prompt_tok_per_s)} tok/s)` : "") +
      ` and wrote ${s.tokens} at ${s.tok_per_s} tok/s` + (s.load_s > 0.5 ? `, after ${s.load_s}s loading the model into memory.` : ".")) : null);
}
