// First-run "pick your AI" screen, also reachable from Settings and Ask.
import { bus, clear, desktop, fmtBytes, get, h, icon, post, state, toast } from "./core.js";

let overlay = null;
let timer = null;
let dismissed = false;

export function maybeShowSetup() {
  const ai = state.status?.ai;
  if (dismissed || overlay || !ai || ai.provider !== "builtin") return;
  if (ai.state === "no-model") openSetup();
}

export async function openSetup() {
  if (overlay) return;
  overlay = h("div.modal.setup", { role: "dialog", "aria-modal": "true", "aria-label": "Set up the AI" });
  document.body.append(overlay);
  await render();
  timer = setInterval(render, 1000);
}

export function closeSetup() {
  dismissed = true;
  clearInterval(timer);
  overlay?.remove();
  overlay = null;
}

export async function addOwnModel() {
  const d = desktop();
  let paths = [];
  if (d && d.pick_models) paths = (await d.pick_models()) || [];
  else {
    const p = prompt("Full path to a .gguf file (or a folder of them). Add its mmproj file too for vision.");
    if (p) paths = [p.trim().replace(/^"|"$/g, "")];
  }
  if (!paths.length) return false;
  try {
    const r = await post("/api/ai/import", { paths });
    if (r.errors.length) toast(r.errors.join("\n"), true, 8000);
    const chat = r.added.find((a) => a.role === "chat");
    const proj = r.added.find((a) => a.role === "mmproj");
    if (chat) toast(`Using ${chat.name}${proj ? " with vision" : ""}. Loading it now…`);
    else if (r.added.length) toast(`Added ${r.added.map((a) => a.file).join(", ")}`);
    bus.emit("status:refresh");
    bus.emit("models:refresh");
    return !!chat;
  } catch (e) { toast(e.message, true); return false; }
}

async function render() {
  if (!overlay) return;
  let m;
  try { m = await get("/api/ai/models"); } catch { return; }
  const ai = state.status?.ai;
  if (ai && ai.state !== "no-model" && !Object.values(m.downloads).some((d) => d.status === "running" || d.status === "queued")) {
    if (ai.chat_model) { toast(`${ai.chat_model.name} is ready. Ask away.`); closeSetup(); return; }
  }
  const downloading = Object.entries(m.downloads).find(([, d]) => d.status === "running" || d.status === "queued");
  const failed = Object.entries(m.downloads).find(([, d]) => d.status === "error");
  const ram = m.ram_gb ? `${Math.round(m.ram_gb)} GB of RAM` : "this PC";

  const cards = m.catalog.map((c) => {
    const job = m.downloads[c.id];
    const active = job && (job.status === "running" || job.status === "queued");
    return h(`div.pick${c.recommended ? ".rec" : ""}`,
      h("div.pick-top", h("b", c.name), c.recommended && h("span.pill.ok", `Best fit for ${ram}`),
        !c.fits && h("span.pill.no", "Needs more RAM")),
      h("p", c.blurb),
      h("div.pick-foot",
        h("span.muted", `${fmtBytes(c.size)} download, once`),
        active ? h("span.muted", "Downloading…")
          : c.installed ? h("span.pill.ok", "Installed")
            : h("button.btn" + (c.recommended ? ".primary" : ""), {
              disabled: !!downloading,
              onclick: async () => { try { await post("/api/ai/download", { preset: c.id }); render(); } catch (e) { toast(e.message, true); } },
            }, c.partial ? "Resume download" : "Download")));
  });

  let progress = null;
  if (downloading) {
    const [pid, j] = downloading;
    const name = m.catalog.find((c) => c.id === pid)?.name || pid;
    progress = h("div.dl",
      h("div.dl-top", h("b", `Downloading ${name}`), h("span", `${Math.round(j.progress * 100)}%`)),
      h("div.progress.big", h("i", { style: { width: `${j.progress * 100}%` } })),
      h("div.muted", j.message || "Starting…"),
      h("p.muted", "Search, transcription and editing already work while this finishes. Closing this window doesn't stop the download."),
      h("button.btn.ghost.sm", { onclick: async () => { await post(`/api/jobs/${j.id}/cancel`); render(); } }, "Pause download"));
  } else if (failed) {
    progress = h("div.warn.bad", `Download stopped: ${failed[1].error}. Press Resume to pick up where it left off.`);
  }

  clear(overlay, h("div.card.setup-card",
    h("header", icon("mark", "setup-mark"), h("b", "Give Lorekeeper a brain"),
      h("button.btn.ghost.sm.icon", { onclick: closeSetup, "aria-label": "Close" }, icon("x"))),
    h("p.lede", "Answers, image reading and video-frame descriptions run on a model that lives on this PC. ",
      "Nothing you add ever leaves your machine. Pick one; you can switch any time in Settings."),
    progress,
    h("div.picks", cards),
    h("div.setup-alt",
      h("button.btn", { onclick: async () => { if (await addOwnModel()) closeSetup(); } }, icon("folder"), "Use a GGUF model I already have"),
      h("button.btn.ghost", { onclick: () => { closeSetup(); bus.emit("go", { view: "settings" }); } }, "Use an Ollama server instead"),
      h("div.grow"),
      h("button.btn.ghost", { onclick: closeSetup }, downloading ? "Hide" : "Set up later"))));
}
