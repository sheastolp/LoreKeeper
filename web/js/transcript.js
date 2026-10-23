// Editable transcript lines: click a line to fix what Whisper misheard. Saving re-times the words,
// so captions, subtitles, search and clip transcripts all pick up the correction.
import { bus, fmtTime, h, patch, toast } from "./core.js";

// segments: the source's transcript segments (updated in place). from/to limit which lines are shown.
export function editableTranscript(sid, segments, { seek, from = -Infinity, to = Infinity, onSaved } = {}) {
  const rows = [];
  segments.forEach((sg, i) => {
    if (sg.end <= from || sg.start >= to) return;
    const text = h("span.tx", { title: "Click to correct", tabindex: 0 }, sg.text);
    let before = sg.text;
    const edit = () => {
      if (text.isContentEditable) return;
      before = sg.text;
      text.contentEditable = "plaintext-only";
      if (text.contentEditable !== "plaintext-only") text.contentEditable = "true";
      text.focus();
      const r = document.createRange(); r.selectNodeContents(text); r.collapse(false);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    };
    const finish = async (keep) => {
      if (!text.isContentEditable) return;
      text.contentEditable = "false";
      const val = text.textContent.replace(/\s+/g, " ").trim();
      if (!keep || !val || val === before.trim()) { text.textContent = before; return; }
      row.classList.add("saving");
      try {
        const tr = await patch(`/api/sources/${sid}/transcript`, { edits: [{ i, start: sg.start, text: val }] });
        Object.assign(sg, tr.segments[i]);
        text.textContent = sg.text;
        row.classList.add("edited");
        bus.emit("transcript", { id: sid, segments: tr.segments, old: before.trim(), text: sg.text });
        onSaved?.(sg, before.trim());
      } catch (e) { text.textContent = before; toast(e.message, true); }
      row.classList.remove("saving");
    };
    text.addEventListener("click", edit);
    text.addEventListener("keydown", (e) => {
      if (!text.isContentEditable) { if (e.key === "Enter") { e.preventDefault(); edit(); } return; }
      e.stopPropagation();                         // don't trigger view shortcuts (space, arrows) while typing
      if (e.key === "Enter") { e.preventDefault(); text.blur(); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    text.addEventListener("blur", () => finish(true));
    const row = h(`p${sg.edited ? ".edited" : ""}`,
      h("button.ts", { onclick: () => seek?.(sg.start), title: "Play from here" }, fmtTime(sg.start)), text);
    rows.push(row);
  });
  return h("div.seglist.editable", rows.length ? rows : h("p.muted", "No speech in this stretch."));
}
