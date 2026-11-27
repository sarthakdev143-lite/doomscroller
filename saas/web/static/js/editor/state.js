// The edit spec and its history. Pure (no DOM), so tests/saas/test_web_editor.py runs it under node.
//
// Two shapes:
// * the STATE the editor works on: every key filled, so each control always has a value;
// * the SPEC the server stores and renders (POST /api/clips/{id}/render). A clip's stored spec lists only
//   what differs from its first render ({} = as first made), and the engine rejects unknown keys
//   (app/clipper/editor.py validate_spec). So toSpec() sends a key only when the server's spec had it or the
//   customer changed it from the first render, and never invents a value the clip did not have.
import { EPS, clamp, cleanText, finite, ms } from "./util.js";

export const CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"];
export const KEYS = ["trim", "captions", "hook", "logo", "memes", "sfx", "music", "broll"];

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const obj = (v) => (isObj(v) ? v : {});
const bool = (v, fallback) => (typeof v === "boolean" ? v : fallback);

/**
 * What the editor allows, from GET /api/clips/{id}/edit `limits` (saas/jobs/editor.py): ranges come as
 * [lo, hi] pairs, re-renders as free_left / credit_cost_after_free. Defaults are the engine's own limits
 * (app/clipper/editor.py), so a missing field never loosens a rule.
 */
export function readLimits(raw) {
  const r = obj(raw);
  const n = (key, fallback) => finite(r[key], fallback);
  const range = (key, i, fallback) => (Array.isArray(r[key]) ? finite(r[key][i], fallback) : fallback);
  const left = r.free_left ?? r.free_rerenders_left;
  return {
    freeRerendersLeft: left === undefined || left === null ? null : finite(left, null),
    creditCost: n("credit_cost_after_free", 0),
    balance: finite(r.balance, null),
    hookChars: n("max_hook_chars", 80),
    captionChars: n("max_caption_chars", 40),
    maxMemes: n("max_memes", 10),
    memeMin: range("meme_duration_s", 0, n("meme_min_s", 0.5)),
    memeMax: range("meme_duration_s", 1, n("meme_max_s", 4)),
    memeDefault: n("meme_default_s", 1.5),
    minClip: n("min_clip_s", 3),
    logoMin: range("logo_scale", 0, n("logo_scale_min", 0.06)),
    logoMax: range("logo_scale", 1, n("logo_scale_max", 0.25)),
    logoDefault: n("logo_scale_default", 0.2),
    dbMin: range("music_volume_db", 0, n("music_db_min", -30)),
    dbMax: range("music_volume_db", 1, n("music_db_max", -6)),
    dbDefault: n("music_db_default", -16),
    hookSeconds: n("hook_seconds", 2.5),
  };
}

/** Caption fixes in either form the server may hold ([{i, text}] or {"3": "text"}), sorted by word index. */
function readEdits(raw, words, limit) {
  const pairs = Array.isArray(raw)
    ? raw.filter(isObj).map((e) => [e.i, e.text])
    : Object.entries(obj(raw)).map(([i, text]) => [Number(i), text]);
  const out = new Map();
  for (const [i, text] of pairs) {
    if (!Number.isInteger(i) || typeof text !== "string") continue;
    const clean = cleanText(text).slice(0, limit);
    const word = words.find((w) => w.i === i);
    if (!clean || !word || clean === word.text) continue;
    out.set(i, clean);
  }
  return [...out.entries()].sort((a, b) => a[0] - b[0]).map(([i, text]) => ({ i, text }));
}

function readMeme(m, ctx) {
  const { limits, duration } = ctx;
  if (!isObj(m) || typeof m.id !== "string" || !m.id) return null;
  const length = clamp(finite(m.duration_s, limits.memeDefault), limits.memeMin, limits.memeMax);
  const start = clamp(finite(m.start_s, 0), 0, Math.max(0, duration - limits.memeMin));
  return { id: m.id, start_s: ms(start), duration_s: ms(length), sound: bool(m.sound, true) };
}

/**
 * The full state of a clip from a stored spec. `ctx` = {duration, words, limits, logoAvailable, defaults,
 * memes}: `defaults` (GET /edit, optional) is what the first render had, `memes` (optional) its memes.
 */
export function effective(raw, ctx) {
  const s = obj(raw);
  const d = obj(ctx.defaults);
  const part = (key) => ({ ...obj(d[key]), ...obj(s[key]) });
  const { duration, limits } = ctx;

  const trim = part("trim");
  let start = clamp(finite(trim.start_s, 0), 0, duration);
  let end = clamp(finite(trim.end_s, duration), 0, duration);
  if (end - start < Math.min(limits.minClip, duration) - EPS) {
    start = 0;
    end = duration;
  }

  const captions = part("captions");
  const hook = part("hook");
  const hookText = cleanText(hook.text).slice(0, limits.hookChars);
  const logo = part("logo");
  const scale = finite(logo.scale, null);
  const music = part("music");
  const db = finite(music.volume_db, null);
  const memeList = Array.isArray(s.memes) ? s.memes : Array.isArray(ctx.memes) ? ctx.memes : Array.isArray(d.memes) ? d.memes : [];

  return {
    trim: { start_s: ms(start), end_s: ms(end) },
    captions: { enabled: bool(captions.enabled, true), edits: readEdits(captions.edits, ctx.words || [], limits.captionChars) },
    hook: { enabled: bool(hook.enabled, Boolean(hookText)), text: hookText },
    logo: {
      enabled: bool(logo.enabled, Boolean(ctx.logoAvailable)),
      position: CORNERS.includes(logo.position) ? logo.position : null, // null: the default spot, centred near the top
      scale: scale === null ? null : ms(clamp(scale, limits.logoMin, limits.logoMax)), // null: the default size
    },
    memes: memeList.map((m) => readMeme(m, ctx)).filter(Boolean).slice(0, limits.maxMemes),
    sfx: { enabled: bool(part("sfx").enabled, true) },
    music: { enabled: bool(music.enabled, false), volume_db: db === null ? null : Math.round(clamp(db, limits.dbMin, limits.dbMax)) },
    broll: { enabled: bool(part("broll").enabled, true) },
  };
}

/** Stable text for comparing states: object keys sorted, memes in time order. */
export function canonical(value) {
  const sort = (v) => {
    if (Array.isArray(v)) return v.map(sort);
    if (isObj(v)) return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sort(v[k])]));
    return v;
  };
  const copy = isObj(value) && Array.isArray(value.memes)
    ? { ...value, memes: [...value.memes].sort((a, b) => a.start_s - b.start_s || String(a.id).localeCompare(String(b.id))) }
    : value;
  return JSON.stringify(sort(copy));
}

export function same(a, b) {
  return canonical(a) === canonical(b);
}

function specPart(key, value, ctx) {
  switch (key) {
    case "trim":
      return { start_s: value.start_s, end_s: value.end_s };
    case "captions":
      return { enabled: value.enabled, edits: value.edits.map((e) => ({ i: e.i, text: e.text })) };
    case "hook":
      // The field keeps what is typed (spaces included); the engine gets it cleaned, as it would clean it.
      return { enabled: value.enabled, text: cleanText(value.text).slice(0, ctx.limits.hookChars) };
    case "logo": {
      // The engine refuses a logo setting for a clip with no logo; "off" is always accepted.
      if (!ctx.logoAvailable) return { enabled: false };
      const out = { enabled: value.enabled };
      if (value.position) out.position = value.position;
      if (value.scale !== null) out.scale = value.scale;
      return out;
    }
    case "memes":
      return value.map((m) => ({ id: m.id, start_s: m.start_s, duration_s: m.duration_s, sound: m.sound }));
    case "music":
      return value.volume_db === null ? { enabled: value.enabled } : { enabled: value.enabled, volume_db: value.volume_db };
    default:
      return { enabled: value.enabled };
  }
}

/**
 * The spec to POST for a state: each key the server's stored spec already had (`serverKeys`), plus each key
 * whose value now differs from the first render (`base`). Exactly the engine's shape, nothing else.
 */
export function toSpec(state, { base, serverKeys, ctx }) {
  const out = {};
  for (const key of KEYS) {
    if (!serverKeys.has(key) && same(state[key], base[key])) continue;
    out[key] = specPart(key, state[key], ctx);
  }
  return out;
}

export function editedText(state, i) {
  const hit = state.captions.edits.find((e) => e.i === i);
  return hit ? hit.text : null;
}

/** Set (or, when it matches the original word, clear) one caption fix. */
export function setEdit(state, word, text, limit) {
  const clean = cleanText(text).slice(0, limit);
  const rest = state.captions.edits.filter((e) => e.i !== word.i);
  if (clean && clean !== word.text) rest.push({ i: word.i, text: clean });
  state.captions.edits = rest.sort((a, b) => a.i - b.i);
}

/** A meme the customer places: inside the engine's length range and wholly inside the clip. */
export function fitMeme(meme, { duration, limits }) {
  const length = ms(clamp(meme.duration_s, limits.memeMin, Math.min(limits.memeMax, Math.max(limits.memeMin, duration))));
  const start = ms(clamp(meme.start_s, 0, Math.max(0, duration - length)));
  return { ...meme, start_s: start, duration_s: length };
}

/**
 * When each meme really shows, by the engine's rule (app/clipper/editor.py validate_spec): memes share one
 * corner, so an earlier meme is cut off where the next one starts, and two that start less than `minGap`
 * apart cannot render at all (`clash`). Indexes match `memes`; `cut` marks a meme that ends early.
 */
export function memeSpans(memes, minGap = 0.5) {
  const spans = memes.map((m) => ({ start: m.start_s, end: m.start_s + m.duration_s, clash: false, cut: false }));
  const order = memes.map((_, k) => k).sort((a, b) => memes[a].start_s - memes[b].start_s);
  for (let n = 0; n + 1 < order.length; n += 1) {
    const a = order[n];
    const b = order[n + 1];
    if (memes[b].start_s - memes[a].start_s < minGap - EPS) {
      spans[a].clash = true;
      spans[b].clash = true;
    }
    if (spans[a].end > memes[b].start_s + EPS) {
      spans[a].end = memes[b].start_s;
      spans[a].cut = true;
    }
  }
  return spans;
}

/** A start for a new meme at `t` that keeps clear of every other meme's start. */
export function clearStart(memes, t, minGap = 0.5) {
  let start = t;
  for (let pass = 0; pass <= memes.length; pass += 1) {
    const hit = memes.find((m) => Math.abs(m.start_s - start) < minGap - EPS);
    if (!hit) return start;
    start = hit.start_s + minGap;
  }
  return start;
}

export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/**
 * Undo history over whole-state snapshots. change() is one step; a burst of changes to one control
 * (`key`, within a second) merges into one step; begin()/end() make a whole drag one step.
 */
export class Store {
  constructor(state, { limit = 200, mergeMs = 1000, now = () => Date.now() } = {}) {
    this.state = clone(state);
    this.past = [];
    this.future = [];
    this.limit = limit;
    this.mergeMs = mergeMs;
    this.now = now;
    this.held = null;
    this.last = null;
    this.listeners = new Set();
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(reason) {
    this.listeners.forEach((fn) => fn(this.state, reason));
  }

  get canUndo() {
    return this.past.length > 0;
  }

  /** Untouched since loading: no step to undo or redo, no drag under way. */
  get pristine() {
    return !this.past.length && !this.future.length && this.held === null;
  }

  /** Start over from `state`, with no history (a render the customer didn't make in this page finished). */
  reset(state, reason = "load") {
    this.state = clone(state);
    this.past = [];
    this.future = [];
    this.held = null;
    this.last = null;
    this.emit(reason);
  }

  get canRedo() {
    return this.future.length > 0;
  }

  push(snapshot) {
    this.past.push(snapshot);
    if (this.past.length > this.limit) this.past.shift();
    this.future = [];
  }

  /** Apply `mutate` to a copy of the state. Returns false when it changed nothing. */
  change(mutate, { key = null, reason = "change" } = {}) {
    const before = JSON.stringify(this.state);
    const next = clone(this.state);
    mutate(next);
    if (JSON.stringify(next) === before) return false;
    if (this.held === null) {
      const at = this.now();
      const merge = key !== null && this.last && this.last.key === key && at - this.last.at < this.mergeMs;
      if (!merge) this.push(before);
      this.last = key !== null ? { key, at } : null;
    }
    this.state = next;
    this.emit(reason);
    return true;
  }

  begin() {
    if (this.held === null) this.held = JSON.stringify(this.state);
    this.last = null;
  }

  end() {
    if (this.held === null) return;
    const held = this.held;
    this.held = null;
    if (held !== JSON.stringify(this.state)) this.push(held);
    this.emit("history");
  }

  undo() {
    if (this.held !== null || !this.past.length) return false;
    this.future.push(JSON.stringify(this.state));
    this.state = JSON.parse(this.past.pop());
    this.last = null;
    this.emit("undo");
    return true;
  }

  redo() {
    if (this.held !== null || !this.future.length) return false;
    this.past.push(JSON.stringify(this.state));
    this.state = JSON.parse(this.future.pop());
    this.last = null;
    this.emit("redo");
    return true;
  }
}
