// Direction C entry (templates/redesign/c.html). Small on purpose: it picks the mode before anything heavy loads.
//
//   rc-static  no WebGL, or prefers-reduced-motion: the page stays plain, with the still poster.
//   rc-motion  WebGL and motion allowed: chapters become tall pinned beats, and once the hero has painted, the
//              story (three.js, GSAP, Lenis) is fetched and started. Any failure falls back to rc-static.
//
// ?rc=static forces the poster, for checking it without changing system settings.
import { initPricing } from "./pricing.js";

const html = document.documentElement;
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
const forced = new URLSearchParams(location.search).get("rc");

function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2"); // three.js r163+ draws with WebGL 2 only
    if (!gl) return false;
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    return true;
  } catch (_) {
    return false;
  }
}

let story = null;

function toStatic() {
  if (story) {
    try {
      story.stop();
    } catch (_) {
      /* already torn down */
    }
    story = null;
  }
  html.classList.remove("rc-motion", "rc-live", "rc-gl");
  html.classList.add("rc-static");
}

/** After the hero has painted and the browser has a moment: LCP is the headline, never the canvas. */
function afterFirstPaint() {
  return new Promise((resolve) => {
    const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 200));
    requestAnimationFrame(() => requestAnimationFrame(() => idle(() => resolve(), { timeout: 1500 })));
  });
}

/** The display's refresh interval, measured while the page is still quiet: the median of ~24 frame intervals.
 *  The stage only lowers its quality when it drops frames against this, so a 40 or 50 Hz screen is not "slow". */
function measureRefresh(frames = 24) {
  return new Promise((resolve) => {
    const deltas = [];
    let last = 0;
    const step = (t) => {
      if (last) deltas.push(t - last);
      last = t;
      if (deltas.length < frames) requestAnimationFrame(step);
      else resolve(deltas.sort((a, b) => a - b)[Math.floor(deltas.length / 2)]);
    };
    requestAnimationFrame(step);
  });
}

async function startStory() {
  html.classList.add("rc-motion");
  await afterFirstPaint();
  if (reduce.matches) return;
  try {
    const [mod, refreshMs] = await Promise.all([import("./story.js"), measureRefresh()]);
    story = await mod.start({ onFail: toStatic, refreshMs });
  } catch (err) {
    console.warn("[rc] story unavailable, showing the still version", err);
    toStatic();
  }
}

initPricing(document);

if (forced === "static" || reduce.matches || !webglAvailable()) {
  toStatic();
} else {
  startStory();
}

// Turning reduced motion on mid-visit stops everything at once.
reduce.addEventListener?.("change", (e) => {
  if (e.matches) toStatic();
});
