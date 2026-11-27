// Direction C's scroll story: smooth scroll (Lenis), section triggers (GSAP ScrollTrigger), the WebGL stage
// (scene.js) and the words on the page, all driven from one number.
//
//   S  the story position: 0 at the top of the hero, i when chapter i's top reaches the top of the screen
//      (1 listen, 2 cut, 3 frame, 4 caption, 5 shorts, 6 package, 7 pricing).
//   J  the closing section: 0 until it enters, 1 once it is mostly on screen.
//
// Started by main.js only when WebGL works and motion is allowed. stop() undoes everything it did.
import { gsap } from "../../vendor/gsap/index.js";
import { ScrollTrigger } from "../../vendor/gsap/ScrollTrigger.js";
import Lenis from "../../vendor/lenis/lenis.min.js";
import { Stage } from "./scene.js";
import { Sound } from "./sound.js";
import { track } from "../site.js";

const html = document.documentElement;
const CHAPTERS = ["listen", "cut", "frame", "caption", "shorts", "package"];
const LABELS = ["Source", "Listen", "Cut", "Frame", "Caption", "Shorts", "Package", "Pricing"];
const SOURCE_SECONDS = 72 * 60 + 40; // the demo episode, routes.DEMO_SOURCE_SECONDS
const CLIP_INS = ["00:14:02.3", "00:31:18.0", "00:58:40.1"];

const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));

function clock(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
}

function waitForFonts() {
  // The Shorts bake text into textures: draw them in Archivo, not in whatever was there first.
  if (!document.fonts?.load) return Promise.resolve();
  return Promise.race([
    Promise.all([document.fonts.load('800 40px "Archivo"'), document.fonts.load('700 20px "Archivo"')]),
    new Promise((r) => setTimeout(r, 2500)),
  ]).catch(() => {});
}

export async function start({ onFail, refreshMs } = {}) {
  gsap.registerPlugin(ScrollTrigger);
  const stageEl = document.querySelector("[data-rc-stage]");
  let canvas = stageEl.querySelector("[data-rc-canvas]");
  const tiles = JSON.parse(stageEl.dataset.rcTiles || "[]");
  const clips = stageEl.dataset.rcClips ? stageEl.dataset.rcClips.split("|") : null;
  const hud = document.querySelector("[data-rc-hud]");
  const tcEl = hud?.querySelector("[data-rc-timecode]");
  const chEl = hud?.querySelector("[data-rc-chapter]");
  const soundBtn = document.querySelector("[data-rc-sound]");
  const soundLabel = soundBtn?.querySelector("[data-rc-sound-label]");
  const hero = document.querySelector(".rc-hero");
  const pricing = document.querySelector("#pricing");
  const join = document.querySelector("#join");
  const dock = document.querySelector("[data-rc-dock]");
  const chapterEls = CHAPTERS.map((c) => document.querySelector(`[data-rc-ch="${c}"]`));
  const words = Array.from(document.querySelectorAll("[data-rc-tx] .rc-w"));
  const tx = document.querySelector("[data-rc-tx]");
  const cutRows = Array.from(document.querySelectorAll(".rc-cuts__row"));
  const specRows = Array.from(document.querySelectorAll("[data-rc-spec] dl > div"));
  const fine = window.matchMedia("(pointer: fine)").matches;

  await waitForFonts();

  const sound = new Sound();
  const triggers = [];
  const cleanups = [];
  let stage = null;
  let S = 0;
  let J = 0;
  let velocity = 0;
  let tops = [];
  let joinTop = 0;
  let vh = window.innerHeight;
  let lastWords = -1;
  let docked = false;
  let painted = false;
  let stopped = false;
  let lenis = null;
  let raf = null;
  let tick = null;
  let ro = null;
  let heroTitle = null;
  let heroBody = null;
  let mark = null;
  let markTween = null;

  /* ---- the stage ------------------------------------------------------------------------------------------ */
  const onEvent = (kind, j) => {
    if (kind === "snap") {
      sound.snap();
      if (j === 0) {
        cutRows[0]?.classList.add("is-snapped");
        setTimeout(() => cutRows[1]?.classList.add("is-snapped"), 140);
      }
    } else if (kind === "lock") {
      sound.lock();
    } else if (kind === "word") {
      sound.tick();
    } else if (kind === "relayout") {
      rebuild();
    }
  };

  // A context the browser takes away (GPU reset, too many contexts) ends the story. Our own disposals also lose
  // the context (forceContextLoss), so the listener is detached first: a rebuild must not read as a failure.
  const onLost = (e) => {
    e.preventDefault();
    fail(new Error("WebGL context lost"));
  };

  function makeStage() {
    const s = new Stage(canvas, { tiles, clipSources: clips, onEvent, refreshMs }).init();
    canvas.addEventListener("webglcontextlost", onLost, { once: true });
    s.start();
    return s;
  }

  function disposeStage() {
    canvas.removeEventListener("webglcontextlost", onLost);
    stage?.dispose();
    stage = null;
  }

  function rebuild() {
    // Crossing the phone/desktop line changes the scene itself: build it again on a fresh canvas.
    disposeStage();
    const fresh = canvas.cloneNode(false);
    canvas.replaceWith(fresh);
    canvas = fresh;
    stage = makeStage();
    stage.setStory(S, J);
  }

  const controller = {
    stop() {
      if (stopped) return;
      stopped = true;
      if (tick) gsap.ticker.remove(tick);
      if (raf) gsap.ticker.remove(raf);
      triggers.forEach((t) => t?.kill());
      markTween?.kill();
      const touched = [heroTitle, heroBody, dock].filter(Boolean);
      if (touched.length) gsap.set(touched, { clearProps: "all" });
      mark?.style.removeProperty("--rc-mark");
      cleanups.forEach((fn) => fn());
      ro?.disconnect();
      lenis?.destroy();
      disposeStage();
      sound.close();
      if (hud) hud.hidden = true;
      if (soundBtn) soundBtn.hidden = true;
      document.querySelectorAll("[data-rc-in]").forEach((el) => el.removeAttribute("data-rc-in"));
      html.classList.remove("rc-live", "rc-gl");
      stageEl.style.removeProperty("--rc-stage-o");
      hud?.style.removeProperty("--rc-hud-o");
      soundBtn?.style.removeProperty("--rc-hud-o");
      delete window.__rcStory;
    },
  };

  function fail(err) {
    console.warn("[rc] story stopped", err);
    controller.stop();
    onFail?.();
  }

  try {
    stage = makeStage();
    if (new URLSearchParams(location.search).has("rcdebug")) window.__rcStage = stage;
  } catch (err) {
    fail(err);
    return controller;
  }

  /* ---- smooth scroll -------------------------------------------------------------------------------------- */
  lenis = new Lenis({ lerp: 0.085, wheelMultiplier: 0.9, smoothWheel: true, syncTouch: false, anchors: true });
  lenis.on("scroll", (e) => {
    velocity = e.velocity || 0;
    ScrollTrigger.update();
  });
  raf = (time) => lenis.raf(time * 1000);
  gsap.ticker.add(raf);
  gsap.ticker.lagSmoothing(0);

  /* ---- measuring the story -------------------------------------------------------------------------------- */
  function measure() {
    vh = window.innerHeight;
    const abs = (el) => el.getBoundingClientRect().top + window.scrollY;
    tops = [abs(hero), ...chapterEls.map(abs), abs(pricing)];
    joinTop = join ? abs(join) : Infinity;
  }

  function storyAt(y) {
    if (y >= tops[7]) return Math.min(8, 7 + (y - tops[7]) / vh);
    for (let i = 6; i >= 0; i--) {
      if (y >= tops[i]) return i + (y - tops[i]) / (tops[i + 1] - tops[i]);
    }
    return 0;
  }

  /* ---- the words on the page ------------------------------------------------------------------------------ */
  // Every chapter's words rise in when it arrives; markup stays plain, these attributes are added here.
  chapterEls.forEach((sec) => {
    const parts = [...sec.querySelectorAll(".rc-ch__text > *"), ...sec.querySelectorAll(".rc-tx, .rc-cuts, .rc-anatomy, .rc-shorts, .rc-spec")];
    parts.forEach((el, i) => {
      el.setAttribute("data-rc-in", "");
      el.style.setProperty("--rc-d", String(i));
    });
    triggers.push(ScrollTrigger.create({ trigger: sec, start: "top 62%", end: "bottom 38%", toggleClass: "is-on" }));
  });

  // the hero lifts away as the story begins
  heroTitle = hero.querySelector(".rc-hero__title");
  heroBody = hero.querySelector(".rc-hero__body");
  const heroTween = gsap.timeline({
    scrollTrigger: { trigger: hero, start: "top top", end: "bottom top", scrub: true },
  });
  heroTween.to(heroTitle, { yPercent: -18, opacity: 0.15, ease: "none" }, 0).to(heroBody, { y: -60, opacity: 0, ease: "none" }, 0);
  triggers.push(heroTween.scrollTrigger);

  // the caption underline is drawn as the light comes on
  mark = hero.querySelector(".rc-mark");
  markTween = gsap.fromTo(mark, { "--rc-mark": 0 }, { "--rc-mark": 1, duration: 1.6, ease: "expo.out", delay: 0.5 });

  function applyWords() {
    // listen: the transcript lights up word by word as the light reads the episode
    const u = clamp((S - 1.0) / 0.52);
    const lit = S < 1 ? 0 : Math.round(u * words.length);
    if (lit !== lastWords) {
      words.forEach((w, i) => {
        w.classList.toggle("is-lit", i < lit);
        w.classList.toggle("is-now", i === lit - 1 && u < 1);
      });
      if (lit > lastWords && lastWords >= 0 && u < 1) sound.tick();
      lastWords = lit;
    }
    write("cut", S >= 1.53, (v) => tx?.classList.toggle("is-cut", v));
    write("snap", S < 2.05 ? "off" : S > 2.12 ? "on" : "mid", (v) => {
      if (v !== "mid") cutRows.forEach((r) => r.classList.toggle("is-snapped", v === "on"));
    });
    write("spec", specRows.filter((_, k) => S >= 5.98 + k * 0.045).length, (n) => specRows.forEach((row, k) => row.classList.toggle("is-set", k < n)));
  }

  // Writes to the page only when a value changes: a custom property set on <html> restyles the whole document,
  // so each value is written once, where it is used, and only when it changes.
  const written = new Map();
  const write = (key, value, fn) => {
    if (written.get(key) === value) return;
    written.set(key, value);
    fn(value);
  };

  function applyHud() {
    if (!hud) return;
    const idx = Math.min(7, Math.floor(S + 0.02));
    write("ch", LABELS[idx], (v) => (chEl.textContent = v));
    let tc;
    if (S < 1) tc = 0;
    else if (S < 1.55) tc = clamp((stage.scanX(S) + stage.ribbonLen / 2) / stage.ribbonLen) * SOURCE_SECONDS;
    else tc = SOURCE_SECONDS;
    if (S >= 2.06 && S < 2.7) {
      const j = S >= 2.3 ? 2 : S >= 2.18 ? 1 : 0;
      write("tc", `IN ${CLIP_INS[j]}`, (v) => (tcEl.textContent = v));
    } else {
      write("tc", clock(tc), (v) => (tcEl.textContent = v));
    }
    write("hud", S < 6.6 && J === 0 ? "1" : "0", (v) => {
      hud.style.setProperty("--rc-hud-o", v);
      soundBtn?.style.setProperty("--rc-hud-o", v);
    });
  }

  function applyJoin() {
    if (!dock || docked || J < 0.35) return;
    docked = true;
    gsap.fromTo(dock, { y: 110, rotation: -2.2, opacity: 0 }, {
      y: 0, rotation: 0, opacity: 1, duration: 1.5, ease: "elastic.out(1, 0.6)",
      onStart: () => sound.lock(),
    });
  }

  /* ---- pointer -------------------------------------------------------------------------------------------- */
  const onMove = (e) => {
    if (e.pointerType && e.pointerType !== "mouse") return;
    stage?.setPointer((e.clientX / window.innerWidth) * 2 - 1, -(e.clientY / window.innerHeight) * 2 + 1, true);
  };
  const onLeave = () => stage?.setPointer(0, 0, false);
  if (fine) {
    window.addEventListener("pointermove", onMove, { passive: true });
    document.documentElement.addEventListener("pointerleave", onLeave);
    cleanups.push(() => {
      window.removeEventListener("pointermove", onMove);
      document.documentElement.removeEventListener("pointerleave", onLeave);
    });
  }

  // magnetic buttons: the yellow ones lean toward the cursor, and the label leans a little further
  if (fine) {
    const magnets = document.querySelectorAll(".site-nav .btn, .rc-dock .btn--accent, [data-rc-magnetic]");
    magnets.forEach((btn) => {
      const label = btn.querySelector("[data-label]") || btn;
      const toX = gsap.quickTo(btn, "x", { duration: 0.5, ease: "power3.out" });
      const toY = gsap.quickTo(btn, "y", { duration: 0.5, ease: "power3.out" });
      const lx = label !== btn ? gsap.quickTo(label, "x", { duration: 0.6, ease: "power3.out" }) : null;
      const ly = label !== btn ? gsap.quickTo(label, "y", { duration: 0.6, ease: "power3.out" }) : null;
      const move = (e) => {
        const r = btn.getBoundingClientRect();
        const dx = e.clientX - (r.left + r.width / 2);
        const dy = e.clientY - (r.top + r.height / 2);
        toX(dx * 0.22);
        toY(dy * 0.3);
        lx?.(dx * 0.1);
        ly?.(dy * 0.12);
      };
      const leave = () => {
        gsap.to(btn, { x: 0, y: 0, duration: 0.9, ease: "elastic.out(1, 0.35)" });
        if (label !== btn) gsap.to(label, { x: 0, y: 0, duration: 0.9, ease: "elastic.out(1, 0.35)" });
      };
      btn.addEventListener("pointermove", move);
      btn.addEventListener("pointerleave", leave);
      cleanups.push(() => {
        btn.removeEventListener("pointermove", move);
        btn.removeEventListener("pointerleave", leave);
        gsap.set([btn, label], { clearProps: "transform" });
      });
    });
  }

  /* ---- sound ---------------------------------------------------------------------------------------------- */
  const onSound = async () => {
    const on = await sound.toggle();
    soundBtn.setAttribute("aria-pressed", String(on));
    soundLabel.textContent = on ? "Sound on" : "Sound off";
    track("rc_sound", { on });
  };
  if (soundBtn) {
    soundBtn.hidden = false;
    soundBtn.addEventListener("click", onSound);
    cleanups.push(() => soundBtn.removeEventListener("click", onSound));
  }
  if (hud) hud.hidden = false;

  /* ---- the loop -------------------------------------------------------------------------------------------- */
  let last = performance.now();
  tick = () => {
    if (stopped) return;
    const now = performance.now();
    const dt = (now - last) / 1000;
    last = now;
    const y = lenis.animatedScroll ?? window.scrollY;
    S = storyAt(y);
    J = clamp((y + vh - joinTop) / (vh * 0.75));
    stage.setStory(S, J);
    stage.velocity = velocity * 60;
    sound.speed(velocity * 60);
    applyWords();
    applyHud();
    applyJoin();
    // the stage is covered by the paper sections from pricing on: no drawing until the closing section
    const show = S < 7.02 || J > 0;
    const stageO = S < 7 ? 1 : Math.round(J * 50) / 50;
    write("stage", stageO, (v) => stageEl.style.setProperty("--rc-stage-o", String(v)));
    if (show && !document.hidden) {
      stage.frame(dt, now);
      if (!painted) {
        painted = true;
        html.classList.add("rc-gl");
      }
    }
    stage.playVideos(show && S > 2.9);
  };
  gsap.ticker.add(tick);

  const onResize = () => {
    measure();
    stage?.resize();
    ScrollTrigger.refresh();
  };
  window.addEventListener("resize", onResize);
  cleanups.push(() => window.removeEventListener("resize", onResize));
  ro = "ResizeObserver" in window ? new ResizeObserver(() => measure()) : null;
  ro?.observe(document.body);

  html.classList.add("rc-live");
  measure();
  ScrollTrigger.refresh();

  // for the checks in tests and recordings: where the story is, and the stage's frame time and quality
  window.__rcStory = {
    get S() { return S; },
    get J() { return J; },
    get frameMs() { return stage?.frameMs; },
    get workMs() { return stage?.workMs; },
    get vsyncMs() { return stage?.vsyncMs; },
    get quality() { return stage?.quality; },
    lenis,
    /** Jump to story position s (0..8); `smooth` scrolls there instead. */
    scrollToS(s, smooth = false) {
      const i = Math.min(7, Math.floor(s));
      const y = i >= 7 ? tops[7] + (s - 7) * vh : tops[i] + (s - i) * (tops[i + 1] - tops[i]);
      lenis.scrollTo(y, smooth ? { duration: 1.2 } : { immediate: true, force: true });
    },
  };

  return controller;
}
