// /app/brand: one logo (upload, preview, remove) and up to N memes (upload several, preview, delete).
import { api, busy, el, fail, plural, upload } from "./api.js";
import "./app-shell.js";

const $ = (sel) => document.querySelector(sel);
let limits = {};

function logoView(logo) {
  const preview = $("[data-logo-preview]");
  preview.textContent = "";
  if (logo) {
    preview.append(el("img", { src: `${logo.url}?v=${encodeURIComponent(logo.id)}`, alt: "Your logo" }));
    $("[data-logo-choose]").textContent = "Replace the logo";
  } else {
    preview.append(el("span", { class: "muted small", text: "No logo yet" }));
    $("[data-logo-choose]").textContent = "Upload a logo";
  }
  $("[data-logo-delete]").hidden = !logo;
}

function memeTile(meme) {
  const isVideo = (meme.media_type || "").startsWith("video/");
  const media = isVideo
    ? el("video", { src: meme.url, muted: true, loop: true, autoplay: true, playsinline: true, "aria-label": meme.name || "Meme" })
    : el("img", { src: meme.url, alt: meme.name || "Meme", loading: "lazy" });
  if (isVideo) media.muted = true;
  const remove = el("button", { class: "link-btn small", type: "button", "data-track": "brand-meme-delete", "aria-label": `Delete ${meme.name || "meme"}`, text: "Delete" });
  const tile = el("figure", { class: "meme" }, el("div", { class: "meme__media" }, media),
    el("figcaption", { class: "meme__foot" }, el("span", { text: meme.name || "meme" }), remove));
  remove.addEventListener("click", async () => {
    await busy(remove, async () => {
      try {
        await api(`/api/brand/memes/${encodeURIComponent(meme.id)}`, { method: "DELETE" });
        tile.remove();
        count(-1);
      } catch (err) {
        fail(err);
      }
    });
  });
  return tile;
}

let memeCount = 0;
function count(delta = 0) {
  memeCount += delta;
  $("[data-meme-count]").textContent = limits.max_memes ? `${memeCount} of ${limits.max_memes}` : plural(memeCount, "meme", "memes");
}

async function load() {
  try {
    const brand = await api("/api/brand");
    limits = brand.limits || {};
    logoView(brand.logo);
    if (limits.logo_max_mb) $("[data-logo-rules]").textContent = `PNG, JPEG or WebP, up to ${limits.logo_max_mb} MB. A transparent PNG looks best; it is shown up to 220×88 on the clip.`;
    if (limits.meme_max_mb) $("[data-meme-rules]").textContent = `GIF, PNG, MP4, MOV or WebM. Up to ${limits.meme_max_mb} MB and ${limits.meme_max_seconds} seconds each.`;
    const grid = $("[data-meme-grid]");
    grid.textContent = "";
    (brand.memes || []).forEach((m) => grid.append(memeTile(m)));
    memeCount = (brand.memes || []).length;
    count();
  } catch (err) {
    fail(err);
  }
}

$("[data-logo-input]").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const out = $("[data-logo-error]");
  out.textContent = "";
  await busy($("[data-logo-delete]"), async () => {
    try {
      const logo = await upload("/api/brand/logo", file);
      logoView(logo.logo || logo);
    } catch (err) {
      out.textContent = err.message;
    }
  });
});

$("[data-logo-delete]").addEventListener("click", async (e) => {
  await busy(e.currentTarget, async () => {
    try {
      await api("/api/brand/logo", { method: "DELETE" });
      logoView(null);
    } catch (err) {
      fail(err);
    }
  });
});

async function addMemes(files) {
  const out = $("[data-meme-error]");
  out.textContent = "";
  const progress = $("[data-meme-progress]");
  const errors = [];
  for (const file of files) {
    progress.hidden = false;
    $("[data-meme-progress-name]").textContent = file.name;
    $("[data-meme-progress-bar]").style.width = "0%";
    try {
      const res = await upload("/api/brand/memes", file, {
        onProgress: (p) => {
          $("[data-meme-progress-bar]").style.width = `${Math.round(p * 100)}%`;
          $("[data-meme-progress-pct]").textContent = p >= 1 ? "Checking…" : `${Math.round(p * 100)}%`;
        },
      });
      $("[data-meme-grid]").prepend(memeTile(res.meme || res));
      count(1);
    } catch (err) {
      errors.push(`${file.name}: ${err.message}`);
    }
  }
  progress.hidden = true;
  out.textContent = errors.join(" ");
}

$("[data-meme-input]").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  addMemes(files);
});
const drop = $("[data-meme-drop]");
["dragenter", "dragover"].forEach((n) => drop.addEventListener(n, (e) => {
  e.preventDefault();
  drop.dataset.over = "true";
}));
["dragleave", "drop"].forEach((n) => drop.addEventListener(n, (e) => {
  e.preventDefault();
  drop.dataset.over = "false";
}));
drop.addEventListener("drop", (e) => addMemes([...(e.dataTransfer?.files || [])]));

load();
