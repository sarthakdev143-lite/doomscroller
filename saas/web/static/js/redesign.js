// Landing previews (redesign/a.html, redesign/b.html): the before/after cut, the three drawn Shorts, and the
// waitlist count slot. The hero clip itself is landing.js (data-demo).
import { track } from "./site.js";

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const SVG = "http://www.w3.org/2000/svg";

/* ---- waitlist count ---------------------------------------------------------------------------------------- */
// Fed later by the edge: either it writes data-count into the page, or it calls setWaitlistCount(n). Nothing is
// shown below 50 people; a small number reads as an empty room.
export const WAITLIST_COUNT_MIN = 50;

export function setWaitlistCount(n) {
  const count = Number.parseInt(String(n), 10);
  document.querySelectorAll("[data-waitlist-count]").forEach((el) => {
    const show = Number.isFinite(count) && count >= WAITLIST_COUNT_MIN;
    el.hidden = !show;
    if (show) el.querySelector("[data-waitlist-count-value]").textContent = count.toLocaleString("en");
  });
}

document.querySelectorAll("[data-waitlist-count]").forEach((el) => setWaitlistCount(el.dataset.count));
window.dsSetWaitlistCount = setWaitlistCount;

/* ---- the three drawn Shorts ------------------------------------------------------------------------------- */
// Each frame steps through its caption groups, lighting one word at a time, like the renderer's captions.
const WORD_MS = 380;
const HOOK_MS = 2400;

function animateFrame(holder) {
  const groups = holder.dataset.baWords.split("|").map((g) => g.split(" "));
  const caption = holder.querySelector(".clip__caption");
  const hook = holder.querySelector(".clip__hook");
  const bar = holder.querySelector(".clip__bar");
  const steps = groups.flatMap((words, g) => words.map((_, w) => [g, w]));
  const total = steps.length * WORD_MS + 900;
  bar.style.width = "100%";
  bar.style.transformOrigin = "left center";

  function draw(time) {
    // A frame's timestamp can precede the moment play started, so clamp before indexing.
    const t = Math.max(0, time);
    const i = Math.min(steps.length - 1, Math.floor(t / WORD_MS));
    const [g, w] = steps[i];
    caption.textContent = "";
    groups[g].forEach((text, k) => {
      const span = document.createElement("span");
      span.textContent = text;
      if (k === w) span.className = "is-active";
      caption.append(span);
      if (k < groups[g].length - 1) caption.append(" ");
    });
    if (hook) hook.dataset.hidden = String(t > HOOK_MS);
    bar.style.transform = `scaleX(${Math.min(1, t / total).toFixed(4)})`;
  }
  draw(WORD_MS * 1.2); // the still: first group, second word lit
  return { draw, total };
}

/* ---- before / after ---------------------------------------------------------------------------------------- */
function curve(from, to) {
  const midY = (from.y + to.y) / 2;
  return `M${from.x.toFixed(1)} ${from.y.toFixed(1)} C${from.x.toFixed(1)} ${midY.toFixed(1)}, ${to.x.toFixed(1)} ${midY.toFixed(1)}, ${to.x.toFixed(1)} ${to.y.toFixed(1)}`;
}

function initBeforeAfter(stage) {
  const links = stage.querySelector("[data-ba-links]");
  const pins = Array.from(stage.querySelectorAll("[data-ba-pin]"));
  const clips = Array.from(stage.querySelectorAll("[data-ba-clip]"));
  const replay = stage.querySelector("[data-ba-replay]");
  const pause = stage.querySelector("[data-ba-pause]");
  const frames = clips.map((c) => c.querySelector("[data-ba-words]")).filter(Boolean).map(animateFrame);
  // Real clips, when the owner has added them: played from here (no autoplay attribute), so reduced motion and
  // the Pause button govern them exactly like the drawn frames.
  const videos = Array.from(stage.querySelectorAll("video"));
  const moving = frames.length > 0 || videos.length > 0;

  // Lines from each cut in the source to the Short it became. Drawn from the layout, so they follow any width.
  const scale = stage.querySelector(".rd-ba__scale");
  function drawLinks() {
    const box = stage.getBoundingClientRect();
    links.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    links.textContent = "";
    // Lines start under the timecode row, so they never run through its labels.
    const top = (scale ? scale.getBoundingClientRect().bottom : 0) - box.top + 8;
    pins.forEach((pin, i) => {
      const clip = clips[i];
      if (!clip) return;
      const p = pin.getBoundingClientRect();
      const c = clip.getBoundingClientRect();
      const path = document.createElementNS(SVG, "path");
      path.setAttribute("d", curve(
        { x: p.left + p.width / 2 - box.left, y: Math.max(top, p.bottom - box.top + 2) },
        { x: c.left + c.width / 2 - box.left, y: c.top - box.top - 6 },
      ));
      path.setAttribute("pathLength", "1");
      path.style.setProperty("--i", String(i));
      links.append(path);
    });
  }

  let playing = false;
  let userPaused = reduceMotion.matches;
  let visible = false;
  let start = 0;

  function loop(now) {
    if (!playing) return;
    frames.forEach((f, i) => f.draw((now - start + i * 700) % (f.total + 1200)));
    requestAnimationFrame(loop);
  }

  function sync() {
    const shouldPlay = moving && !userPaused && visible && !document.hidden;
    if (shouldPlay && !playing) {
      playing = true;
      start = performance.now();
      if (frames.length) requestAnimationFrame(loop);
      videos.forEach((v) => v.play().catch(() => {}));
    } else if (!shouldPlay) {
      playing = false;
      videos.forEach((v) => v.pause());
    }
    if (pause) pause.textContent = userPaused ? "Play" : "Pause";
  }

  function cut() {
    stage.dataset.baState = "cut";
  }

  drawLinks();
  if ("ResizeObserver" in window) new ResizeObserver(drawLinks).observe(stage);
  window.addEventListener("load", drawLinks, { once: true });

  // With motion allowed, the section starts as the uncut source and collapses when it comes into view.
  // Without it (or without IntersectionObserver), it simply shows the result.
  if (!reduceMotion.matches && "IntersectionObserver" in window) {
    stage.dataset.baState = "source";
    new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        visible = e.isIntersecting;
        if (e.isIntersecting && e.intersectionRatio >= 0.3 && stage.dataset.baState === "source") {
          window.setTimeout(cut, 350);
        }
      });
      sync();
    }, { threshold: [0, 0.3] }).observe(stage);
  } else {
    visible = true;
  }

  if (replay) {
    replay.hidden = false;
    replay.addEventListener("click", () => {
      track("ba_replay", {});
      stage.dataset.baState = "source";
      window.setTimeout(cut, 600);
    });
  }
  if (pause && moving) {
    pause.hidden = false;
    pause.addEventListener("click", () => {
      userPaused = !userPaused;
      track("ba_toggle", { playing: !userPaused });
      sync();
    });
  }
  document.addEventListener("visibilitychange", sync);
  reduceMotion.addEventListener?.("change", (e) => {
    if (e.matches) {
      userPaused = true;
      cut();
    }
    sync();
  });
  sync();
}

document.querySelectorAll("[data-ba]").forEach(initBeforeAfter);
