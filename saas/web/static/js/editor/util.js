// Pure helpers for the clip editor: time, word-edge snapping, caption lines. No DOM and no imports, so
// tests/saas/test_web_editor.py runs them under node.

export const EPS = 1e-3;

export function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

/** Milliseconds are the finest the engine keeps (app/clipper/editor.py rounds to 3 places). */
export function ms(value) {
  return Math.round(Number(value) * 1000) / 1000;
}

export function finite(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && value !== null && value !== "" && typeof value !== "boolean" ? n : fallback;
}

/** 0:04.2 — minutes, seconds and tenths. */
export function stamp(seconds) {
  const tenths = Math.round(Math.max(0, Number(seconds) || 0) * 10);
  const m = Math.floor(tenths / 600);
  const s = Math.floor((tenths % 600) / 10);
  return `${m}:${String(s).padStart(2, "0")}.${tenths % 10}`;
}

/** 2.5 s */
export function secs(seconds) {
  return `${(Math.round((Number(seconds) || 0) * 10) / 10).toFixed(1)} s`;
}

/** Whitespace collapsed and control characters dropped, as the engine cleans text before rendering. */
export function cleanText(value) {
  return String(value ?? "")
    .replace(/[\u0000-\u001f\u007f-\u009f\u00a0\u2028\u2029]/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
}

/**
 * The edges a trim handle may sit on. A clip may start where a word starts and end where a word ends, so
 * no cut lands inside a word; the clip's own ends are always allowed.
 */
export function edges(words, duration, which) {
  const out = new Set([which === "start" ? 0 : ms(duration)]);
  for (const w of words || []) {
    const t = ms(which === "start" ? w.start_s : w.end_s);
    if (Number.isFinite(t) && t >= 0 && t <= duration + EPS) out.add(t);
  }
  return [...out].sort((a, b) => a - b);
}

/** The allowed edge nearest to t inside [lo, hi], or null when none fits. */
export function nearestEdge(list, t, lo, hi) {
  let best = null;
  for (const e of list) {
    if (e < lo - EPS || e > hi + EPS) continue;
    if (best === null || Math.abs(e - t) < Math.abs(best - t)) best = e;
  }
  return best;
}

/** The next edge after t (dir > 0) or before it (dir < 0) inside [lo, hi], or null. */
export function stepEdge(list, t, dir, lo, hi) {
  if (dir > 0) {
    for (const e of list) if (e > t + EPS && e <= hi + EPS && e >= lo - EPS) return e;
    return null;
  }
  for (let k = list.length - 1; k >= 0; k -= 1) {
    const e = list[k];
    if (e < t - EPS && e >= lo - EPS && e <= hi + EPS) return e;
  }
  return null;
}

/**
 * Where a trim handle lands when dragged or stepped to `t`. The other handle stays put and the clip keeps at
 * least `minLen` seconds. Snaps to a word edge unless `free` (Alt held), or when no edge fits.
 */
export function placeTrim(trim, which, t, { words = [], duration, minLen = 3, free = false } = {}) {
  const lo = which === "start" ? 0 : Math.min(duration, trim.start_s + minLen);
  const hi = which === "start" ? Math.max(0, trim.end_s - minLen) : duration;
  let value = clamp(t, lo, hi);
  if (!words || !words.length) {
    // No words to snap to (a clip cut on movement, or made without captions): a tenth-of-a-second grid.
    value = clamp(Math.round(value * 10) / 10, lo, hi);
  } else if (!free) {
    const snapped = nearestEdge(edges(words, duration, which), value, lo, hi);
    if (snapped !== null) value = snapped;
  }
  return { ...trim, [which === "start" ? "start_s" : "end_s"]: ms(value) };
}

/**
 * One arrow-key step for a trim handle: the next word edge in that direction, or `fine` seconds unsnapped.
 * With no words at all, a plain step is half a second.
 */
export function stepTrim(trim, which, dir, { words = [], duration, minLen = 3, fine = 0 } = {}) {
  const key = which === "start" ? "start_s" : "end_s";
  const lo = which === "start" ? 0 : Math.min(duration, trim.start_s + minLen);
  const hi = which === "start" ? Math.max(0, trim.end_s - minLen) : duration;
  if (!fine && (!words || !words.length)) fine = 0.5;
  if (fine) return { ...trim, [key]: ms(clamp(trim[key] + dir * fine, lo, hi)) };
  const next = stepEdge(edges(words, duration, which), trim[key], dir, lo, hi);
  return next === null ? trim : { ...trim, [key]: ms(next) };
}

/** Index of the word playing at t (binary search over sorted words), or -1. */
export function wordAt(words, t) {
  let lo = 0;
  let hi = (words || []).length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const w = words[mid];
    if (t < w.start_s) hi = mid - 1;
    else if (t >= w.end_s) lo = mid + 1;
    else return mid;
  }
  return -1;
}

/**
 * Words grouped into readable caption lines for the captions panel: a new line after a sentence ends, after
 * a pause, or when the line is full. Grouping reads the original text, so fixing a word never reflows lines.
 */
export function captionLines(words, { maxWords = 7, maxChars = 34, pause = 0.7 } = {}) {
  const lines = [];
  let line = null;
  (words || []).forEach((w, k) => {
    const prev = words[k - 1];
    const text = String(w.text || "");
    const breakHere =
      !line ||
      line.words.length >= maxWords ||
      line.chars + text.length + 1 > maxChars ||
      (prev && (w.start_s - prev.end_s > pause || /[.!?…]["”’)\]]*$/.test(String(prev.text || ""))));
    if (breakHere) {
      line = { start_s: w.start_s, words: [], chars: 0 };
      lines.push(line);
    }
    line.words.push(w);
    line.chars += text.length + 1;
  });
  return lines;
}

/** Ruler spacing: the smallest step that leaves at least `minPx` between labelled ticks. */
export function tickStep(pps, minPx = 64) {
  for (const step of [0.5, 1, 2, 5, 10, 15, 30, 60, 120]) if (step * pps >= minPx) return step;
  return 300;
}
