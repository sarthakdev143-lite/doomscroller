// Landing page: the live clip demo and the source timeline under it.
import { track } from "./site.js";

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

/* ---- timeline placement --------------------------------------------------------------------- */
// Positions come from the server as data attributes; setting them through CSSOM keeps the page free
// of inline style attributes (the CSP may forbid them).
function placeTimeline() {
  document.querySelectorAll("[data-pos-x]").forEach((el) => {
    const x = parseFloat(el.dataset.posX);
    const w = parseFloat(el.dataset.posW || "0");
    if (!Number.isFinite(x)) return;
    el.style.setProperty("--x", String(x));
    el.style.setProperty("--w", String(w));
    if (el.hasAttribute("data-zoom")) {
      el.style.setProperty("--x1", `${x}%`);
      el.style.setProperty("--x2", `${x + w}%`);
    }
    el.setAttribute("data-placed", "");
  });
}

/* ---- the clip demo ----------------------------------------------------------------------------- */
const CLIP_SECONDS = 15.6; // matches the clip's length in the hero head
const HOOK_SECONDS = 2.5; // app/clipper/captions.py _HOOK_SECONDS
const HOLD_SECONDS = 0.35; // last caption lingers this long
const LOOP_PAUSE = 1.4;

// app/clipper/captions.py group_words: at most 3 words, 32 characters, and a break after punctuation.
function groupWords(words) {
  const groups = [];
  let current = [];
  for (const word of words) {
    const tooFar = current.length && word.start - current[current.length - 1].end > 0.5;
    const tooLong = current.length && current.reduce((n, w) => n + w.text.length + 1, 0) + word.text.length > 32;
    if (current.length && (tooFar || tooLong || current.length >= 3)) {
      groups.push(current);
      current = [];
    }
    current.push(word);
    if (/[.?!,]$/.test(word.text)) {
      groups.push(current);
      current = [];
    }
  }
  if (current.length) groups.push(current);
  return groups;
}

// app/clipper/captions.py _line_breaks: one line up to 16 characters, else two lines split where the
// halves are closest in length.
const MAX_LINE_CHARS = 16;
function breakLines(caption) {
  caption.querySelectorAll("br").forEach((br) => br.remove());
  const words = Array.from(caption.querySelectorAll("span"));
  const lengths = words.map((w) => w.textContent.length + 1);
  const total = lengths.reduce((a, b) => a + b, 0) - 1;
  if (total <= MAX_LINE_CHARS || words.length < 2) return;
  let best = 1;
  let bestDiff = Infinity;
  for (let split = 1; split < words.length; split++) {
    const left = lengths.slice(0, split).reduce((a, b) => a + b, 0);
    const diff = Math.abs(left - (total + 1 - left));
    if (diff < bestDiff) {
      best = split;
      bestDiff = diff;
    }
  }
  words[best].before(document.createElement("br"));
}

function buildTranscript(sentences) {
  const words = [];
  let t = 0.25;
  sentences.forEach((li) => {
    const p = li.querySelector("p");
    const tokens = p.textContent.trim().split(/\s+/);
    p.textContent = "";
    tokens.forEach((text, i) => {
      const span = document.createElement("span");
      span.className = "w";
      span.textContent = text;
      p.append(span);
      if (i < tokens.length - 1) p.append(" ");
      const letters = text.replace(/[^\p{L}\p{N}]/gu, "").length;
      const dur = 0.2 + Math.min(letters, 10) * 0.032;
      words.push({ text, start: t, end: t + dur, speaker: li.dataset.speaker || "a", el: span });
      t += dur + 0.06;
    });
    t += 0.18; // pauses are cut out of speech clips, so sentences sit close together
  });
  // Stretch to the clip's stated length, so the clock and the captions agree.
  const scale = (CLIP_SECONDS - HOLD_SECONDS - 0.2) / t;
  words.forEach((w) => {
    w.start *= scale;
    w.end *= scale;
  });
  return words;
}

function clock(seconds) {
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}

function initDemo() {
  const root = document.querySelector("[data-demo]");
  const sentences = Array.from(document.querySelectorAll("[data-demo-sentence]"));
  if (!root || !sentences.length) return;
  const frame = root.querySelector("[data-demo-frame]");
  const caption = frame.querySelector("[data-demo-caption]");
  const hook = frame.querySelector("[data-demo-hook]");
  const bar = frame.querySelector("[data-demo-progress]");
  const clockEl = root.querySelector("[data-demo-clock]");
  const toggle = root.querySelector("[data-demo-toggle]");
  const list = sentences[0].closest("ol");

  const words = buildTranscript(sentences);
  const groups = groupWords(words);
  const groupOf = new Map();
  groups.forEach((g) => g.forEach((w) => groupOf.set(w, g)));
  bar.style.width = "100%";
  bar.style.transformOrigin = "left center";

  let shownIndex = -2;
  function render(t) {
    let index = -1;
    for (let i = 0; i < words.length; i++) {
      if (words[i].start <= t) index = i;
      else break;
    }
    const last = words[words.length - 1];
    if (index === words.length - 1 && t > last.end + HOLD_SECONDS) index = -1;

    bar.style.transform = `scaleX(${Math.min(1, t / CLIP_SECONDS).toFixed(4)})`;
    if (clockEl) clockEl.textContent = clock(Math.min(t, CLIP_SECONDS));
    if (hook) hook.dataset.hidden = String(t > HOOK_SECONDS);
    if (index === shownIndex) return;
    shownIndex = index;

    words.forEach((w, i) => {
      w.el.classList.toggle("is-said", i <= index);
      w.el.classList.toggle("is-active", i === index);
    });
    caption.textContent = "";
    if (index < 0) return;
    const current = words[index];
    frame.classList.toggle("clip--focus-a", current.speaker === "a");
    frame.classList.toggle("clip--focus-b", current.speaker === "b");
    groupOf.get(current).forEach((w, i, g) => {
      const span = document.createElement("span");
      span.textContent = w.text;
      if (w === current) span.className = "is-active";
      caption.append(span);
      if (i < g.length - 1) caption.append(" ");
    });
    breakLines(caption);
  }

  let playing = false;
  let userPaused = reduceMotion.matches; // reduced motion: never start on its own
  let visible = true;
  let elapsed = 0;
  let lastFrame = 0;

  function frameLoop(now) {
    if (!playing) return;
    elapsed += (now - lastFrame) / 1000;
    lastFrame = now;
    if (elapsed > CLIP_SECONDS + LOOP_PAUSE) elapsed = 0;
    render(elapsed);
    requestAnimationFrame(frameLoop);
  }

  function sync() {
    const shouldPlay = !userPaused && visible && !document.hidden;
    if (shouldPlay && !playing) {
      playing = true;
      list.setAttribute("data-playing", "");
      lastFrame = performance.now();
      requestAnimationFrame(frameLoop);
    } else if (!shouldPlay) {
      playing = false;
    }
    if (toggle) {
      toggle.textContent = userPaused ? "Play" : "Pause";
    }
  }

  // A still frame first: the second word lit, which is also the reduced-motion state.
  list.setAttribute("data-playing", "");
  elapsed = words[1].start + 0.01;
  render(elapsed);

  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      userPaused = !userPaused;
      track("demo_toggle", { playing: !userPaused });
      sync();
    });
  }
  document.addEventListener("visibilitychange", sync);
  if ("IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      visible = entries.some((e) => e.isIntersecting);
      sync();
    }).observe(root);
  }
  reduceMotion.addEventListener?.("change", (e) => {
    if (e.matches) userPaused = true;
    sync();
  });
  sync();
}

placeTimeline();
document.querySelectorAll(".caption-type:not([lang])").forEach(breakLines);
initDemo();
