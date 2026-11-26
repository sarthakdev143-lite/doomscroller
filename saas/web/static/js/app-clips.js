// /app/clips: every clip as a 9:16 thumbnail, paged with the API's `next` cursor.
import { api, busy, clock, el } from "./api.js";
import "./app-shell.js";

const grid = document.querySelector("[data-clips]");
const more = document.querySelector("[data-clips-more]");
let cursor = null;
let first = true;

function thumb(clip) {
  const frame = el("div", { class: "thumb__frame" });
  const img = el("img", { src: clip.thumbnail_url, alt: "", loading: "lazy" });
  img.addEventListener("error", () => img.replaceWith(el("div", { class: "thumb-fallback", text: "No preview" })));
  frame.append(img, el("span", { class: "thumb__dur", text: clock(clip.duration_s) }));
  return el("a", { class: "thumb", href: `/app/jobs/${encodeURIComponent(clip.job_id)}#clip-${clip.id}`, "data-track": "clips-open" },
    frame, el("p", { class: "thumb__title", text: clip.title || `Clip ${clip.index + 1}` }));
}

async function load() {
  const query = new URLSearchParams({ limit: "24" });
  if (cursor) query.set("before", cursor);
  try {
    const { clips, next } = await api(`/api/clips?${query}`);
    if (first) {
      grid.textContent = "";
      grid.removeAttribute("aria-busy");
      first = false;
      if (!clips.length) {
        grid.hidden = true;
        document.querySelector("[data-clips-empty]").hidden = false;
      }
    }
    clips.forEach((c) => grid.append(thumb(c)));
    cursor = next;
    more.hidden = !next;
  } catch (err) {
    const out = document.querySelector("[data-clips-error]");
    out.textContent = `Couldn’t load your clips. ${err.message}`;
    out.hidden = false;
    if (first) grid.hidden = true;
  }
}

more.addEventListener("click", () => busy(more, load));
load();
