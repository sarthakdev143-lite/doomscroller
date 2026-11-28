// Landing direction D, the opening. Decides between the film and the stills, runs the loader, and hands the film
// to the story clock. The mode is stamped on <html data-d="film|still"> before anything is drawn, so styles (and
// anyone recording the page) can tell which one is showing.

import { FrameStore } from "./frames.js";
import { Film } from "./film.js";
import { Loader } from "./loader.js";
import { Story } from "./story.js";

const root = document.documentElement;
const storyEl = document.querySelector("[data-d-story]");
const loaderEl = document.querySelector("[data-d-loader]");
const canvas = document.querySelector("[data-d-canvas]");
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
const capture = new URLSearchParams(window.location.search).has("capture");
const hover = window.matchMedia("(hover: hover) and (pointer: fine)").matches;

const probe = { mode: null, phase: null, error: null, marks: {}, bytes: { critical: 0, all: 0, light: 0 } };
window.__d = probe;

function mark(name) {
  try {
    performance.mark(`d:${name}`);
  } catch (_) {
    /* marks are for measuring only */
  }
  probe.marks[name] = Math.round(performance.now());
}

function webgl2() {
  try {
    return !!document.createElement("canvas").getContext("webgl2");
  } catch (_) {
    return false;
  }
}

function toStill(reason) {
  probe.mode = "still";
  probe.reason = reason;
  root.dataset.d = "still";
  delete root.dataset.dPhase;
  if (loaderEl) loaderEl.hidden = true;
  if (probe.loop) cancelAnimationFrame(probe.loop);
  probe.loop = null;
}

async function boot() {
  if (!storyEl || !loaderEl || !canvas) return;
  if (reduce.matches) return toStill("reduced-motion");
  if (!webgl2() || !("createImageBitmap" in window) || !window.fetch) return toStill("no-webgl2");
  probe.mode = "film";
  root.dataset.d = "film";
  root.dataset.dPhase = "load";
  probe.phase = "load";
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  window.scrollTo(0, 0);
  reduce.addEventListener?.("change", (e) => {
    if (e.matches) toStill("reduced-motion");
  });

  const giveUp = setTimeout(() => {
    if (probe.phase === "load") toStill("timeout");
  }, 25000);

  const loader = new Loader(loaderEl);
  let loaderRaf = 0;
  const spin = (now) => {
    if (probe.mode !== "film") return;
    loader.tick(now);
    loaderRaf = requestAnimationFrame(spin);
  };
  loaderRaf = requestAnimationFrame(spin);

  const manifestUrl = storyEl.dataset.dFilm;
  const base = manifestUrl.slice(0, manifestUrl.lastIndexOf("/") + 1);
  const manifest = await (await fetch(manifestUrl, { credentials: "same-origin" })).json();
  const portrait = () => window.innerHeight > window.innerWidth * 1.05;
  const camName = portrait() && manifest.cams.phone ? "phone" : "desk";
  const cam = manifest.cams[camName] || Object.values(manifest.cams)[0];
  probe.cam = camName;
  const store = new FrameStore(base, cam, hover ? 28 : 18);

  const critical = store.packBytes(0);
  await store.loadPack(0, (n) => {
    probe.bytes.critical = n;
    loader.set(n / critical);
  });
  loader.set(1);
  mark("critical-loaded");

  const film = new Film(canvas, { w: cam.w, h: cam.h }, { preserve: capture });
  const first = await store.decode(1);
  film.frames(1, first, 1, first, 0);
  film.draw();
  mark("first-frame");
  if (document.fonts?.ready) await Promise.race([document.fonts.ready, new Promise((r) => setTimeout(r, 1500))]);

  // wait for the number to reach 100 on its own clock, hold a beat, then cut
  await new Promise((resolve) => {
    const wait = (now) => {
      if (probe.mode !== "film") return;
      if (loader.tick(now)) resolve();
      else requestAnimationFrame(wait);
    };
    requestAnimationFrame(wait);
  });
  cancelAnimationFrame(loaderRaf);
  await new Promise((r) => setTimeout(r, 280));
  if (probe.mode !== "film") return;
  clearTimeout(giveUp);

  const lines = {};
  storyEl.querySelectorAll("[data-d-line]").forEach((el) => {
    lines[el.dataset.dLine] = el.querySelector(".d-line__in");
  });
  const story = new Story({
    film,
    store,
    manifest,
    story: storyEl,
    lines,
    cue: storyEl.querySelector("[data-d-cue]"),
    hover,
  });
  probe.story = story;
  probe.store = store;

  // the cut: the loader goes and the film is there, in the same frame
  const go = (now) => {
    story.startIntro(now);
    story.tick(now);
    loader.cut();
    root.dataset.dPhase = "film";
    probe.phase = "film";
    mark("cut");
    const loop = (t) => {
      if (probe.mode !== "film") return;
      story.tick(t);
      probe.loop = requestAnimationFrame(loop);
    };
    probe.loop = requestAnimationFrame(loop);
  };
  requestAnimationFrame(go);

  // the rest of the film, then the light pass (pointer screens only). The film already plays from the critical
  // pack (every fourth frame, blended), so a pack that fails to arrive costs smoothness, never the film.
  for (let i = 1; i < cam.packs.length; i++) {
    try {
      await store.loadPack(i, (n) => {
        probe.bytes.fill = n;
      });
    } catch (err) {
      probe.error = `pack ${i}: ${err && err.message ? err.message : err}`;
    }
  }
  probe.bytes.all = cam.packs.reduce((s, p) => s + p.bytes, 0);
  mark("film-loaded");
  if (hover) {
    try {
      await store.loadLight();
      probe.bytes.light = cam.light ? cam.light.bytes : 0;
      mark("light-loaded");
    } catch (_) {
      /* no light pass: the pointer still warms the air, it just cannot find the glass */
    }
  }

  // turning a phone (or resizing across portrait) swaps to the other camera's frames, without a second loader
  let swapping = false;
  window.addEventListener("resize", async () => {
    const want = portrait() && manifest.cams.phone ? "phone" : "desk";
    if (swapping || want === probe.cam || !manifest.cams[want] || probe.mode !== "film") return;
    swapping = true;
    try {
      const next = new FrameStore(base, manifest.cams[want], hover ? 28 : 18);
      await next.loadPack(0);
      probe.cam = want;
      story.store = next;
      probe.store = next;
      film.size = { w: next.cam.w, h: next.cam.h };
      film.view = null;
      film.resize();
      story.drawn = "";
      for (let i = 1; i < next.cam.packs.length; i++) await next.loadPack(i).catch(() => {});
      if (hover) await next.loadLight().catch(() => {});
    } catch (_) {
      /* keep the frames we have */
    } finally {
      swapping = false;
    }
  });
}

/** For recordings and measurements: the state, and a way to draw now. */
probe.snapshot = () => {
  const s = probe.story;
  const st = probe.store;
  return {
    mode: probe.mode,
    phase: probe.phase,
    cam: probe.cam,
    head: s ? s.head : null,
    target: s ? s.target : null,
    settled: s ? Math.abs(s.head - s.target) < 0.01 && !s.busy && st.pending.size === 0 : false,
    pending: st ? st.pending.size : 0,
    packs: st ? st.packs.map(Boolean) : [],
    light: st ? !!st.light : false,
    marks: probe.marks,
    bytes: probe.bytes,
    error: probe.error,
  };
};
probe.draw = () => probe.story?.render(true);

boot().catch((err) => {
  probe.error = String(err && err.message ? err.message : err);
  toStill("error");
});
