// /app/clips/{id}/edit: the clip editor.
//
// Captions are burned into the MP4 by libass, so the browser never exports video. The customer changes an
// edit spec, sees an approximate preview over the clip, and "Render changes" asks the server to render a new
// version from the clip's saved source excerpt (app/clipper/editor.py). That version then replaces the preview.
//
// API (docs/saas/CONTRACT.md, saas/jobs/editor.py):
//   GET  /api/clips/{id}/edit      {version, duration_s, spec, options, words, memes: {in_clip, available,
//                                   editable}, hook, logo: {available, url, in_clip, showing}, active_job_id,
//                                   limits}
//   POST /api/clips/{id}/render    {spec} -> 202 the re-render job; poll GET /api/jobs/{job.id} until final
//   GET  /api/clips/{id}/versions  {current, versions: [{version, current, created_at, spec, video_url}]}
//   GET  /api/clips/{id}           the clip (download_url is the newest version)
// Spec times are on the ORIGINAL clip's timeline, whatever the trim. A stored spec lists only what differs
// from the first render ({} = as first made).
import { STATUS_LABEL, ago, api, plural } from "../api.js";
import "../app-shell.js";
import { toast } from "../site.js";
import { Panels } from "./panels.js";
import { Player } from "./player.js";
import { activeJob, failureText, followJob } from "./session.js";
import { KEYS, Store, clone, effective, memeSpans, readLimits, same, toSpec } from "./state.js";
import { Timeline } from "./timeline.js";
import { finite, stamp } from "./util.js";

const root = document.querySelector("[data-editor]");
const clipId = root.dataset.clip;
const enc = encodeURIComponent(clipId);
const $ = (sel) => root.querySelector(sel);
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

const ui = {
  undo: $("[data-undo]"),
  redo: $("[data-redo]"),
  render: $("[data-render]"),
  renderLabel: $("[data-render-label]"),
  download: $("[data-download]"),
  state: $("[data-edit-state]"),
  quota: $("[data-rerenders]"),
  progress: $("[data-render-progress]"),
  stage: $("[data-render-stage]"),
  pct: $("[data-render-pct]"),
  meter: $("[data-render-meter]"),
  bar: $("[data-render-bar]"),
  error: $("[data-ed-error]"),
  unavailable: $("[data-ed-unavailable]"),
  unavailableTitle: $("[data-ed-unavailable-title]"),
  unavailableText: $("[data-ed-unavailable-text]"),
  body: $("[data-ed-body]"),
  version: $("[data-version]"),
  compare: $("[data-compare]"),
  play: $("[data-play]"),
  playLabel: $("[data-play-label]"),
  iconPlay: $("[data-icon-play]"),
  iconPause: $("[data-icon-pause]"),
  mute: $("[data-mute]"),
  iconSound: $("[data-icon-sound]"),
  iconMuted: $("[data-icon-muted]"),
  time: $("[data-time]"),
  length: $("[data-length]"),
};

let ctx = null; // {duration, words, limits, logoAvailable, defaults, memes}
let base = null; // the state of the first render ({} spec)
let rendered = null; // the state the newest version was rendered with
let serverKeys = new Set();
let store = null;
let clip = null;
let edit = null; // readEdit() of GET /edit
let versions = [];
let view = "preview";
let rendering = false;
let selected = null;
let labels = new Map();

const player = new Player($("[data-player]"), { onTime, onPlayState });
let timeline = null;
let panels = null;

/* ---- reading the server ----------------------------------------------------------------------- */

function readWords(raw) {
  // The manifest's caption words; `start`/`end` and `start_s`/`end_s` both occur.
  return (Array.isArray(raw) ? raw : [])
    .filter(isObj)
    .map((w) => ({ i: w.i, text: w.text, start_s: Number(w.start_s ?? w.start), end_s: Number(w.end_s ?? w.end) }))
    .filter((w) => Number.isInteger(w.i) && typeof w.text === "string" && w.text && Number.isFinite(w.start_s) &&
      Number.isFinite(w.end_s))
    .map((w) => ({ ...w, end_s: Math.max(w.start_s, w.end_s) }))
    .sort((a, b) => a.start_s - b.start_s);
}

const memeLabel = (m) => String(m.label || m.title || "Meme").replace(/\.(gif|png|jpe?g|webp|mp4|mov|webm)$/i, "")
  .replace(/[-_]+/g, " ").trim().slice(0, 40) || "Meme";

/** GET /edit in one shape, whichever fields the server sends. */
function readEdit(raw) {
  const memes = isObj(raw.memes) ? raw.memes : {};
  const available = (Array.isArray(memes.available) ? memes.available : Array.isArray(raw.memes_available) ? raw.memes_available : [])
    .filter((m) => isObj(m) && typeof m.id === "string" && m.id)
    .map((m) => ({ id: m.id, label: memeLabel(m), url: m.thumb_url || m.url || "", mediaType: m.media_type || "",
      custom: m.provider === "custom" || m.kind === "custom" }));
  const inClip = Array.isArray(memes.in_clip) ? memes.in_clip : Array.isArray(raw.memes) ? raw.memes : null;
  const logo = isObj(raw.logo) ? raw.logo : {};
  const logoAvailable = typeof logo.available === "boolean" ? logo.available : Boolean(raw.logo_available);
  // What the first render had: its hook, its options (captions, sfx, music, broll) and whether it had a logo.
  const defaults = { ...(isObj(raw.defaults) ? raw.defaults : {}) };
  if (typeof raw.hook === "string" && !defaults.hook) defaults.hook = { enabled: Boolean(raw.hook.trim()), text: raw.hook };
  const options = isObj(raw.options) ? raw.options : {};
  for (const key of ["captions", "sfx", "music", "broll"]) {
    if (typeof options[key] === "boolean" && !defaults[key]) defaults[key] = { enabled: options[key] };
  }
  if (typeof logo.in_clip === "boolean" && !defaults.logo) defaults.logo = { enabled: logo.in_clip };
  const words = readWords(raw.words);
  // The engine only places memes on clips cut from speech (they have an edit plan): `memes.editable`. Should
  // a server not say, a clip is locked only when it surely has no speech: captions were on, yet it has no
  // caption words and no memes.
  const flag = typeof memes.editable === "boolean" ? memes.editable
    : typeof raw.memes_editable === "boolean" ? raw.memes_editable : null;
  const memesEditable = flag ?? (words.length > 0 || (inClip || []).length > 0 || options.captions === false);
  return {
    spec: isObj(raw.spec) ? raw.spec : {},
    words,
    memesEditable,
    available,
    inClip,
    defaults,
    logoAvailable,
    logoInClip: typeof logo.in_clip === "boolean" ? logo.in_clip : null,
    logoShowing: typeof logo.showing === "boolean" ? logo.showing : null,
    activeJobId: activeJob(raw),
    logoUrl: "url" in logo ? logo.url || "" : raw.logo_url || (logoAvailable ? "/api/brand/logo/file" : ""),
    duration: finite(raw.duration_s, null),
    limits: raw.limits,
    previewUrl: raw.preview_url || "",
    previewOffset: finite(raw.preview_offset_s, 0),
  };
}

function adoptServer(data) {
  edit = data;
  ctx.limits = readLimits(data.limits);
  serverKeys = new Set(Object.keys(data.spec).filter((k) => KEYS.includes(k)));
  rendered = effective(data.spec, ctx);
  // The server says outright whether the newest version shows a logo; trust it over our reading of the spec.
  if (typeof data.logoShowing === "boolean") rendered.logo.enabled = data.logoShowing && ctx.logoAvailable;
}

/* ---- the spec --------------------------------------------------------------------------------- */

// A key the customer has changed is always sent, even when it lands back on the first render's value, so
// "as first made" can never swallow a choice they made.
let touched = new Set();
let lastState = null;
const specOf = (state) => toSpec(state, { base, serverKeys: new Set([...serverKeys, ...touched]), ctx });
const isDirty = () => Boolean(store) && !same(specOf(store.state), specOf(rendered));

/* ---- versions --------------------------------------------------------------------------------- */

function readVersions(raw) {
  const list = Array.isArray(raw) ? raw : isObj(raw) && Array.isArray(raw.versions) ? raw.versions : [];
  return list
    .map((v) => {
      if (!isObj(v)) return null;
      const n = finite(v.version ?? v.n, null);
      if (!Number.isInteger(n) || n < 1) return null;
      return {
        n,
        video_url: v.video_url || `/api/clips/${enc}/versions/${n}/video`,
        download_url: v.download_url || "",
        spec: isObj(v.spec) ? v.spec : null,
        created_at: v.created_at || null,
        current: Boolean(v.current),
      };
    })
    .filter(Boolean)
    .sort((a, b) => a.n - b.n);
}

async function loadVersions() {
  try {
    const list = readVersions(await api(`/api/clips/${enc}/versions`));
    if (list.length) return list;
  } catch (_) {
    /* no list: the clip itself is the one version we know of */
  }
  const n = Number.isInteger(clip.version) && clip.version > 0 ? clip.version : 1;
  return [{ n, video_url: clip.video_url, download_url: "", spec: null, created_at: clip.created_at, current: true }];
}

const latest = () => versions.find((v) => v.current) || versions[versions.length - 1];

function versionOffset(v) {
  if (v.spec) return effective(v.spec, ctx).trim.start_s;
  return v === latest() ? rendered.trim.start_s : 0;
}

/** A download link for a version. The newest goes through the clip's download (an attachment, named);
 *  ?v= keeps a browser from handing back the file it cached before the last render. */
function downloadUrl(v) {
  if (v.download_url) return v.download_url;
  return v === latest() ? `${clip.download_url}?v=${v.n}` : v.video_url;
}

/**
 * What the preview plays. Best is a clean picture the server offers (preview_url); next the first render,
 * which covers the whole timeline and shows the clip as first made; last the newest version, which may be
 * trimmed (it starts at its own trim) and already shows its edits.
 */
function previewSource() {
  if (edit.previewUrl) return { url: edit.previewUrl, offset: edit.previewOffset, burned: null };
  const first = versions.find((v) => v.n === 1);
  // The first render's pixels have a logo only when the server says so (`logo.in_clip`).
  const firstBurned = edit.logoInClip === true ? base : { ...base, logo: { enabled: false, position: null, scale: null } };
  if (first) return { url: first.video_url, offset: 0, burned: firstBurned };
  const top = latest();
  return { url: top.video_url || clip.video_url, offset: rendered.trim.start_s, burned: rendered };
}

function fillVersions() {
  const select = ui.version;
  select.textContent = "";
  select.append(new Option("Preview of your edits", "preview"));
  const top = latest();
  [...versions].reverse().forEach((v) => {
    const tags = [v === top ? "latest" : "", v.n === 1 ? "original" : ""].filter(Boolean).join(", ");
    const when = v.created_at ? ` · ${ago(v.created_at)}` : "";
    select.append(new Option(`Version ${v.n}${tags ? ` (${tags})` : ""}${when}`, String(v.n)));
  });
  select.disabled = false;
  select.value = String(view);
}

async function showView(next, { at = null } = {}) {
  view = next;
  ui.version.value = String(next);
  if (next === "preview") {
    const src = previewSource();
    await player.load({ ...src, mode: "preview", label: "Preview", poster: clip.thumbnail_url || "", at });
  } else {
    const v = versions.find((x) => x.n === next) || latest();
    await player.load({ url: v.video_url, offset: versionOffset(v), mode: "version", label: `Version ${v.n}`, at });
  }
  chrome();
}

/* ---- the page chrome -------------------------------------------------------------------------- */

function quotaText() {
  const { freeRerendersLeft: left, creditCost } = ctx.limits;
  if (left === null) return "";
  if (left > 0) return `${plural(left, "free re-render", "free re-renders")} left`;
  return creditCost > 0 ? `Each render now uses ${creditCost} min of your balance` : "No free re-renders left";
}

function chrome() {
  if (!store) return;
  const dirty = isDirty();
  const clash = memeSpans(store.state.memes, ctx.limits.memeMin).some((s) => s.clash);
  ui.undo.disabled = !store.canUndo;
  ui.redo.disabled = !store.canRedo;
  ui.render.disabled = rendering || !dirty || clash;
  ui.renderLabel.textContent = rendering ? "Rendering…" : "Render changes";
  root.dataset.dirty = String(dirty);
  ui.state.textContent = rendering
    ? "Rendering your changes."
    : clash
      ? "Two memes start too close together: move one to render."
      : dirty
      ? "Unsaved changes: render to apply them."
      : versions.length > 1 ? "All changes rendered." : "No changes yet.";
  ui.quota.textContent = quotaText();

  const top = latest();
  const shown = view === "preview" ? top : versions.find((v) => v.n === view) || top;
  ui.download.href = downloadUrl(shown);
  ui.download.setAttribute("download", `clip-${clipId}-v${shown.n}.mp4`);
  ui.download.textContent = versions.length > 1 ? `Download v${shown.n}` : "Download";

  const older = versions.filter((v) => v.n < top.n);
  ui.compare.hidden = older.length === 0;
  if (older.length) {
    const onOld = view !== "preview" && view !== top.n;
    ui.compare.textContent = onOld ? "Latest version" : "Previous version";
    ui.compare.dataset.target = String(onOld ? top.n : older[older.length - 1].n);
  }
}

function onTime(t) {
  const text = stamp(t);
  if (ui.time.textContent !== text) ui.time.textContent = text;
  timeline?.setTime(t, player.playing);
  panels?.setTime(t);
}

function onPlayState(playing) {
  ui.iconPlay.hidden = playing;
  ui.iconPause.hidden = !playing;
  ui.playLabel.textContent = playing ? "Pause" : "Play";
}

function selectMeme(k) {
  selected = k;
  timeline.select(k);
  panels.select(k);
  player.select(k);
}

function onChange(state, reason) {
  if (lastState && reason !== "load") KEYS.forEach((k) => { if (!same(lastState[k], state[k])) touched.add(k); });
  lastState = clone(state);
  if (selected !== null && selected >= state.memes.length) selectMeme(null);
  timeline.update(state, labels);
  panels.sync(state);
  player.setState(state, ctx);
  if (view !== "preview" && reason !== "load") showView("preview");
  chrome();
}

function showError(message) {
  ui.error.textContent = message;
  ui.error.hidden = !message;
}

function unavailable(err) {
  ui.body.hidden = true;
  ui.render.disabled = true;
  ui.download.hidden = err.status === 404;
  ui.state.textContent = "";
  ui.unavailable.hidden = false;
  ui.unavailableTitle.textContent = err.status === 404 ? "This clip isn’t here." : "This clip can’t be edited.";
  ui.unavailableText.textContent = err.status === 404
    ? "It may have been deleted, or it belongs to another account."
    : err.message || "Try again in a minute.";
}

/* ---- loading ---------------------------------------------------------------------------------- */

async function load() {
  let raw;
  try {
    [raw, clip] = await Promise.all([api(`/api/clips/${enc}/edit`), api(`/api/clips/${enc}`)]);
  } catch (err) {
    unavailable(err);
    return;
  }
  edit = readEdit(raw);
  const duration = edit.duration ?? finite(clip.duration_s, 0);
  if (!(duration > 0)) {
    unavailable({ status: 409, message: "This clip has no length to edit." });
    return;
  }
  ctx = {
    duration,
    words: edit.words,
    limits: readLimits(edit.limits),
    logoAvailable: edit.logoAvailable,
    memesEditable: edit.memesEditable,
    defaults: edit.defaults,
    memes: edit.inClip,
  };
  base = effective({}, ctx);
  adoptServer(edit);
  store = new Store(rendered);
  versions = await loadVersions();

  labels = new Map(edit.available.map((m) => [m.id, m.label]));
  player.setThumbs(edit.available);
  player.setLogoUrl(edit.logoUrl);

  timeline = new Timeline($("[data-timeline]"), {
    store,
    onSeek: (t) => player.seek(t),
    onSelectMeme: selectMeme,
    onSelectWord: (i) => panels?.rove(i),
  });
  panels = new Panels(root.querySelector(".ed-inspector"), {
    store,
    player,
    onSeek: (t) => player.seek(t),
    onSelectMeme: selectMeme,
    getTime: () => player.time,
  });
  timeline.setup(ctx);
  panels.setup(ctx, edit.available, labels);
  ui.length.textContent = stamp(duration);
  store.subscribe(onChange);
  onChange(store.state, "load");
  fillVersions();
  ui.play.disabled = false;
  await showView("preview", { at: rendered.trim.start_s });
  // A render started before this page loaded (another tab, or before a reload): follow it here.
  if (edit.activeJobId) watchRender(Promise.resolve({ id: edit.activeJobId }), null);
}

/* ---- rendering -------------------------------------------------------------------------------- */

function progress(job) {
  const pct = Math.round(Math.min(1, Math.max(0, Number(job.progress) || 0)) * 100);
  ui.stage.textContent = job.status === "queued" ? "Waiting for a free renderer"
    : STATUS_LABEL[job.stage] || STATUS_LABEL[job.status] || "Rendering";
  ui.pct.textContent = `${pct}%`;
  ui.bar.style.width = `${pct}%`;
  ui.meter.setAttribute("aria-valuenow", String(pct));
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function render() {
  if (rendering || !isDirty()) return;
  const spec = specOf(store.state);
  await watchRender(api(`/api/clips/${enc}/render`, { method: "POST", body: { spec } }), { state: clone(store.state), spec });
}

/**
 * Show a render's progress until it ends, then adopt the new version. `started` resolves to the job (POST
 * /render's answer, or the clip's `active_job_id` after a reload); `mine` is what this page sent, or null
 * for a render started elsewhere.
 */
async function watchRender(started, mine) {
  rendering = true;
  showError("");
  progress({ status: "queued", progress: 0 });
  ui.progress.hidden = false;
  ui.render.setAttribute("aria-busy", "true");
  chrome();
  try {
    const job = await started;
    const jobId = job.id || job.job_id;
    if (!jobId) throw new Error("The server didn’t start a render. Try again.");
    const done = await followJob(jobId, {
      getJob: (id) => api(`/api/jobs/${encodeURIComponent(id)}`),
      wait,
      onProgress: progress,
      hidden: () => document.hidden,
    });
    const why = failureText(done);
    if (why) throw new Error(why);
    await adoptRender(mine);
  } catch (err) {
    showError(err && err.message ? err.message : "The render didn’t finish. Try again.");
  } finally {
    rendering = false;
    ui.progress.hidden = true;
    ui.render.removeAttribute("aria-busy");
    chrome();
  }
}

/** A render finished: the new version becomes what "unsaved" is measured against, and it is shown. */
async function adoptRender(mine) {
  const [fresh, freshClip] = await Promise.all([
    api(`/api/clips/${enc}/edit`).catch(() => null),
    api(`/api/clips/${enc}`).catch(() => clip),
  ]);
  clip = freshClip;
  if (fresh) {
    adoptServer(readEdit(fresh));
  } else if (mine) {
    serverKeys = new Set(Object.keys(mine.spec));
    rendered = mine.state;
  }
  // A render this page didn't send (resumed after a reload): if nothing was edited meanwhile, the editor
  // starts from the new version; otherwise the customer's edits stay and are measured against it.
  if (!mine && fresh && store.pristine) {
    touched = new Set();
    lastState = null;
    store.reset(rendered);
  }
  versions = await loadVersions();
  fillVersions();
  const top = latest();
  await showView(top.n, { at: store.state.trim.start_s });
  toast(`Version ${top.n} is ready and playing. Choose “Preview of your edits” to keep editing.`, "info", 5000);
}

/* ---- wiring ----------------------------------------------------------------------------------- */

ui.undo.addEventListener("click", () => store?.undo());
ui.redo.addEventListener("click", () => store?.redo());
ui.render.addEventListener("click", render);
ui.play.addEventListener("click", () => player.toggle());
ui.mute.addEventListener("click", () => {
  const muted = !player.video.muted;
  player.video.muted = muted;
  ui.mute.setAttribute("aria-pressed", String(muted));
  ui.iconSound.hidden = muted;
  ui.iconMuted.hidden = !muted;
});
ui.version.addEventListener("change", () => {
  showView(ui.version.value === "preview" ? "preview" : Number(ui.version.value));
});
ui.compare.addEventListener("click", () => {
  const target = Number(ui.compare.dataset.target);
  if (Number.isInteger(target)) showView(target);
});

const TYPING = "input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=button]), textarea, select, [contenteditable]";

document.addEventListener("keydown", (e) => {
  if (!store) return;
  const target = e.target instanceof Element ? e.target : document.body;
  const typing = Boolean(target.closest(TYPING));
  const mod = e.ctrlKey || e.metaKey;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (mod && !e.altKey && (key === "z" || key === "y")) {
    if (typing) return; // a text field keeps its own undo
    e.preventDefault();
    if (key === "y" || e.shiftKey) store.redo();
    else store.undo();
    return;
  }
  if (mod || e.altKey || typing || document.querySelector("dialog[open]")) return;
  const onControl = Boolean(target.closest("button, a, summary, [role=slider], [role=tab], input"));
  if ((key === " " && !onControl) || key === "k") {
    e.preventDefault();
    player.toggle();
  } else if (key === "j") {
    player.nudge(-1);
  } else if (key === "l") {
    player.nudge(1);
  }
});

// Coming back through the back/forward cache: put the edit state back into every control.
window.addEventListener("pageshow", (e) => {
  if (e.persisted && store) panels.sync(store.state, true);
});

window.addEventListener("beforeunload", (e) => {
  if (rendering || isDirty()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

load();
