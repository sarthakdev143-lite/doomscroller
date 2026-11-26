// /app/new: a link (estimated) or an upload (with progress), the options, the cost, then POST /api/jobs.
import { ApiError, api, busy, clock, el, minutes, plural, upload } from "./api.js";
import { me } from "./app-shell.js";

const form = document.querySelector("[data-new-form]");
const maxBytes = Number(form.dataset.maxUploadMb || 2048) * 1024 * 1024;
const $ = (sel) => form.querySelector(sel);

const state = { source: null, account: null, brand: { logo: null, memes: [] }, uploadAbort: null, uploadId: null };

/* ---- options ---------------------------------------------------------------------------------- */

function options() {
  const f = form.elements;
  const out = {
    clips: Number(f.clips.value),
    max_clip_seconds: Number(f.max_clip_seconds.value),
    language: f.language.value,
    layout: f.layout.value,
    captions: f.captions.checked,
    broll: f.broll.checked,
    memes: f.memes.checked,
    sfx: f.sfx.checked,
    music: f.music.checked,
  };
  out.logo = Boolean(state.brand.logo) && f.logo.checked;
  out.custom_memes = state.brand.memes.length > 0 && f.custom_memes.checked;
  return out;
}

form.elements.clips.addEventListener("input", () => {
  form.querySelector('[data-out="clips"]').textContent = form.elements.clips.value;
  summarise();
});
form.elements.max_clip_seconds.addEventListener("input", () => {
  form.querySelector('[data-out="max_clip_seconds"]').textContent = `${form.elements.max_clip_seconds.value} s`;
});

/* ---- the source -------------------------------------------------------------------------------- */

function showSource({ title, duration_s, credits, thumbnail }) {
  const box = $("[data-source-result]");
  const thumb = $("[data-source-thumb]");
  thumb.textContent = "";
  if (thumbnail) {
    const img = el("img", { src: thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
    img.addEventListener("error", () => img.replaceWith(el("div", { class: "thumb-fallback", text: "No preview" })));
    thumb.append(img);
  } else {
    thumb.append(el("div", { class: "thumb-fallback", text: "Video" }));
  }
  $("[data-source-title]").textContent = title || "Untitled video";
  $("[data-source-facts]").textContent = `${clock(duration_s)} · ${plural(credits, "minute", "minutes")} to process`;
  box.hidden = false;
}

function sourceError(message) {
  $("[data-source-error]").textContent = message || "";
}

async function estimate(button) {
  const url = form.elements.url.value.trim();
  if (!/^https?:\/\/\S+\.\S+/.test(url)) {
    sourceError("Paste the full link, starting with https://");
    form.elements.url.focus();
    return;
  }
  sourceError("");
  await busy(button, async () => {
    try {
      const res = await api("/api/jobs/estimate", { method: "POST", body: { url } });
      if (res.ok === false) {
        state.source = null;
        $("[data-source-result]").hidden = true;
        sourceError(res.message || "We can’t use that link. Try another, or upload the file.");
      } else {
        state.source = { kind: "url", url, credits: res.credits, duration_s: res.duration_s, funding: res.funding };
        if (state.account) {
          state.account.balance = res.balance ?? state.account.balance;
          if (res.free) state.account.free = res.free;
        }
        showSource(res);
      }
    } catch (err) {
      sourceError(err.message);
    }
    summarise();
  });
}

$("[data-estimate]").addEventListener("click", (e) => estimate(e.currentTarget));
form.elements.url.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    estimate($("[data-estimate]"));
  }
});

async function sendFile(file) {
  sourceError("");
  if (!file) return;
  if (file.type && !file.type.startsWith("video/")) {
    sourceError("That isn’t a video file.");
    return;
  }
  if (file.size > maxBytes) {
    sourceError(`That file is larger than ${Math.round(maxBytes / 1024 / 1024 / 1024 * 10) / 10} GB.`);
    return;
  }
  const progress = $("[data-upload-progress]");
  const bar = $("[data-upload-bar]");
  const pct = $("[data-upload-pct]");
  $("[data-upload-name]").textContent = file.name;
  progress.hidden = false;
  bar.style.width = "0%";
  pct.textContent = "0%";
  const controller = new AbortController();
  state.uploadAbort = controller;
  try {
    const res = await upload("/api/uploads", file, {
      signal: controller.signal,
      onProgress: (p) => {
        bar.style.width = `${Math.round(p * 100)}%`;
        pct.textContent = p >= 1 ? "Checking the file…" : `${Math.round(p * 100)}%`;
      },
    });
    if (state.uploadId) api(`/api/uploads/${encodeURIComponent(state.uploadId)}`, { method: "DELETE" }).catch(() => {});
    state.uploadId = res.upload_id;
    state.source = { kind: "upload", upload_id: res.upload_id, credits: res.credits, duration_s: res.duration_s };
    showSource({ title: file.name, duration_s: res.duration_s, credits: res.credits });
  } catch (err) {
    if (err instanceof ApiError && err.code !== "aborted") sourceError(err.message);
  } finally {
    progress.hidden = true;
    state.uploadAbort = null;
    summarise();
  }
}

const drop = $("[data-dropzone]");
const fileInput = $("[data-file-input]");
fileInput.addEventListener("change", () => sendFile(fileInput.files[0]));
["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (e) => {
  e.preventDefault();
  drop.dataset.over = "true";
}));
["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (e) => {
  e.preventDefault();
  drop.dataset.over = "false";
}));
drop.addEventListener("drop", (e) => sendFile(e.dataTransfer?.files?.[0]));
$("[data-upload-cancel]").addEventListener("click", () => state.uploadAbort?.abort());

/* ---- brand kit -------------------------------------------------------------------------------- */

api("/api/brand").then((brand) => {
  state.brand = { logo: brand.logo, memes: brand.memes || [] };
  if (!brand.logo && !state.brand.memes.length) {
    $("[data-brand-none]").hidden = false;
    return;
  }
  $("[data-brand-has]").hidden = false;
  const img = $("[data-brand-logo]");
  if (brand.logo) {
    img.src = brand.logo.url;
    $("[data-brand-logo-name]").textContent = brand.logo.name || "Your logo";
  } else {
    img.hidden = true;
    form.elements.logo.closest(".switch").hidden = true;
  }
  if (state.brand.memes.length) {
    $("[data-brand-memes-count]").textContent = plural(state.brand.memes.length, "meme", "memes");
  } else {
    $("[data-brand-memes-row]").hidden = true;
  }
}).catch(() => {
  $("[data-brand-none]").hidden = false;
});

/* ---- cost and submit -------------------------------------------------------------------------- */

function summarise() {
  const clips = Number(form.elements.clips.value);
  $("[data-sum-clips]").textContent = String(clips);
  $("[data-submit-label]").textContent = `Make ${plural(clips, "clip", "clips")}`;
  const submit = $("[data-submit]");
  const shortBox = $("[data-sum-short]");
  const note = $("[data-sum-note]");
  shortBox.hidden = true;
  const account = state.account;
  const source = state.source;
  if (account) $("[data-sum-balance]").textContent = minutes(account.balance);
  if (!source) {
    $("[data-sum-length]").textContent = "–";
    $("[data-sum-cost]").textContent = "–";
    note.textContent = "Check a link or upload a file to see the cost.";
    submit.disabled = true;
    return;
  }
  $("[data-sum-length]").textContent = clock(source.duration_s);
  const free = account?.free || { minutes: 0, shorts: 0 };
  const coveredByFree = free.minutes >= source.credits && free.shorts >= clips;
  if (coveredByFree) {
    $("[data-sum-cost]").textContent = "Free";
    note.textContent = `Uses ${plural(source.credits, "free minute", "free minutes")} and up to ${plural(clips, "free Short", "free Shorts")}. Clips on the free tier carry a small watermark.`;
  } else {
    $("[data-sum-cost]").textContent = minutes(source.credits);
    note.textContent = clips > free.shorts && free.shorts > 0 && free.minutes >= source.credits
      ? `Ask for ${free.shorts} clips or fewer to use your free tier.`
      : "Charged from your balance when the job finishes; failed jobs cost nothing.";
  }
  let blocked = "";
  if (account && !account.email_verified) blocked = "Confirm your email first, then you can start.";
  else if (account && account.billing_hold) blocked = "New jobs are paused on this account. Contact support.";
  else if (!coveredByFree && account && account.balance < source.credits) {
    shortBox.textContent = "";
    shortBox.append(`You need ${minutes(source.credits - account.balance)} more. `,
      el("a", { href: "/app/billing", "data-track": "new-buy-minutes", text: "Buy minutes" }));
    shortBox.hidden = false;
  }
  if (blocked) {
    shortBox.textContent = blocked;
    shortBox.hidden = false;
  }
  submit.disabled = Boolean(blocked) || !shortBox.hidden;
}

me.then((account) => {
  state.account = account;
  summarise();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const source = state.source;
  if (!source) return;
  const out = $("[data-submit-error]");
  out.textContent = "";
  const body = { options: options() };
  if (source.kind === "url") body.source_url = source.url;
  else body.upload_id = source.upload_id;
  await busy($("[data-submit]"), async () => {
    try {
      const job = await api("/api/jobs", { method: "POST", body });
      location.assign(`/app/jobs/${encodeURIComponent(job.id)}`);
    } catch (err) {
      out.textContent = err.message;
    }
  });
});

summarise();
