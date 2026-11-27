// The timeline: the clip's whole length (spec time), the trim handles, the caption words, the memes and the
// playhead. Everything is placed in percent of the clip, so zooming only changes the canvas width.
//
// Pointer: drag a handle to trim (snaps to word edges; Alt places it freely), drag a meme to move it, drag its
// right edge to resize, click or drag elsewhere to scrub. Keyboard: handles are sliders that step word by
// word; a focused meme moves with the arrows, resizes with [ and ], toggles sound with S, goes with Delete.
// A whole drag is one undo step (store.begin/end).
import { fitMeme, memeSpans } from "./state.js";
import { clamp, ms, placeTrim, secs, stamp, stepTrim, tickStep } from "./util.js";

const MIN_PPS = 4;
const MAX_PPS = 480;
const ZOOM = 1.6;
const GUTTER = 16; // px either side of the canvas, so a handle at 0 s or at the end is never clipped

const pct = (t, d) => `${((t / d) * 100).toFixed(4)}%`;

export class Timeline {
  constructor(root, { store, onSeek, onSelectMeme, onSelectWord }) {
    this.root = root;
    this.store = store;
    this.onSeek = onSeek;
    this.onSelectMeme = onSelectMeme;
    this.onSelectWord = onSelectWord;
    this.scroll = root.querySelector("[data-tl-scroll]");
    this.track = root.querySelector("[data-tl-track]");
    this.canvas = root.querySelector("[data-tl-canvas]");
    this.ruler = root.querySelector("[data-tl-ruler]");
    this.wordsRow = root.querySelector("[data-tl-words]");
    this.memesRow = root.querySelector("[data-tl-memes]");
    this.shades = { start: root.querySelector('[data-tl-shade="start"]'), end: root.querySelector('[data-tl-shade="end"]') };
    this.handles = { start: root.querySelector('[data-handle="start"]'), end: root.querySelector('[data-handle="end"]') };
    this.playhead = root.querySelector("[data-tl-playhead]");
    this.readout = root.querySelector("[data-trim-readout]");
    this.ctx = null;
    this.pps = 0;
    this.fit = true;
    this.selected = null;
    this.time = 0;
    this.wordNodes = [];
    this.memeNodes = [];
    this.renderedStep = 0;

    root.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => this.zoom(b.dataset.zoom)));
    this.bindScrub();
    this.bindHandles();
    this.bindMemes();
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(() => this.layout()).observe(this.scroll);
    } else {
      window.addEventListener("resize", () => this.layout());
    }
  }

  /* ---- setup and layout ---------------------------------------------------------------------- */

  setup(ctx) {
    this.ctx = ctx;
    const d = ctx.duration;
    this.wordsRow.textContent = "";
    this.wordNodes = ctx.words.map((w) => {
      const node = document.createElement("span");
      node.className = "ed-tl__word";
      node.dataset.i = String(w.i);
      node.style.left = pct(w.start_s, d);
      node.style.width = pct(Math.max(0.04, w.end_s - w.start_s), d);
      node.textContent = w.text;
      this.wordsRow.append(node);
      return node;
    });
    const hint = this.root.querySelector("[data-trim-words-hint]");
    if (hint) hint.hidden = ctx.words.length === 0; // nothing to snap to: the handles move on a 0.1 s grid
    for (const which of ["start", "end"]) {
      this.handles[which].setAttribute("aria-valuemin", "0");
      this.handles[which].setAttribute("aria-valuemax", d.toFixed(1));
    }
    this.layout(true);
  }

  fitPps() {
    const width = (this.scroll.clientWidth || 300) - 2 * GUTTER;
    return Math.max(MIN_PPS, width / this.ctx.duration);
  }

  layout(initial = false) {
    if (!this.ctx) return;
    const fitPps = this.fitPps();
    if (initial) {
      // Show the whole clip (both handles in view) unless that crushes it: then start zoomed in and scroll.
      this.fit = fitPps >= 16;
      this.pps = this.fit ? fitPps : 40;
    } else if (this.fit) {
      this.pps = fitPps;
    } else {
      this.pps = clamp(this.pps, fitPps, MAX_PPS);
      if (this.pps <= fitPps + 0.01) this.fit = true;
    }
    this.track.style.width = this.fit ? "100%" : `${Math.round(this.ctx.duration * this.pps) + 2 * GUTTER}px`;
    this.root.dataset.zoomed = String(!this.fit);
    // A word gets its text only where the text fits; the rest are plain blocks until you zoom in.
    this.ctx.words.forEach((w, n) => {
      this.wordNodes[n].dataset.narrow = String((w.end_s - w.start_s) * this.pps < 7 + 6.5 * w.text.length);
    });
    this.memeNodes.forEach((node) => {
      const m = this.store.state.memes[Number(node.dataset.k)];
      if (m) node.dataset.narrow = String(node.getBoundingClientRect().width < 48);
    });
    this.drawRuler();
  }

  zoom(how) {
    if (!this.ctx) return;
    const fitPps = this.fitPps();
    const anchor = this.time;
    if (how === "fit") {
      this.fit = true;
    } else {
      const next = how === "in" ? this.pps * ZOOM : this.pps / ZOOM;
      this.pps = clamp(next, fitPps, MAX_PPS);
      this.fit = this.pps <= fitPps + 0.01;
    }
    this.layout();
    if (!this.fit) this.scroll.scrollLeft = Math.max(0, GUTTER + anchor * this.pps - this.scroll.clientWidth / 2);
  }

  drawRuler() {
    const d = this.ctx.duration;
    const pps = this.fit ? this.fitPps() : this.pps;
    const step = tickStep(pps);
    if (step === this.renderedStep && this.ruler.childElementCount) return;
    this.renderedStep = step;
    this.ruler.textContent = "";
    const half = step / 2;
    for (let t = 0; t <= d + 1e-6; t = ms(t + half)) {
      const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      const tick = document.createElement("span");
      tick.className = major ? "ed-tl__tick ed-tl__tick--major" : "ed-tl__tick";
      tick.style.left = pct(t, d);
      if (major) {
        const label = document.createElement("span");
        label.className = "ed-tl__tick-label";
        label.textContent = step < 1 ? stamp(t) : stamp(t).replace(/\.\d$/, "");
        tick.append(label);
      }
      this.ruler.append(tick);
    }
  }

  /** x (client px) to spec time. */
  timeAt(clientX) {
    const rect = this.canvas.getBoundingClientRect();
    return clamp(((clientX - rect.left) / rect.width) * this.ctx.duration, 0, this.ctx.duration);
  }

  /* ---- drawing the state --------------------------------------------------------------------- */

  update(state, labels) {
    if (!this.ctx) return;
    const d = this.ctx.duration;
    const { start_s: start, end_s: end } = state.trim;
    this.shades.start.style.width = pct(start, d);
    this.shades.end.style.left = pct(end, d);
    this.shades.end.style.width = pct(d - end, d);
    this.handles.start.style.left = pct(start, d);
    this.handles.end.style.left = pct(end, d);
    this.describeHandle("start", start);
    this.describeHandle("end", end);
    this.readout.textContent = start <= 0.001 && end >= d - 0.001
      ? `Whole clip · ${secs(d)}`
      : `Trimmed to ${stamp(start)}–${stamp(end)} · ${secs(end - start)}`;

    const edits = new Map(state.captions.edits.map((e) => [e.i, e.text]));
    this.ctx.words.forEach((w, n) => {
      const node = this.wordNodes[n];
      const text = edits.get(w.i) ?? w.text;
      if (node.textContent !== text) node.textContent = text;
      node.dataset.edited = String(edits.has(w.i));
      node.dataset.out = String(w.end_s <= start + 0.001 || w.start_s >= end - 0.001);
    });
    this.root.dataset.captions = String(state.captions.enabled);
    this.drawMemes(state.memes, labels);
  }

  describeHandle(which, value) {
    const handle = this.handles[which];
    const words = this.ctx.words;
    let near = "";
    if (which === "start") {
      const w = words.find((x) => Math.abs(x.start_s - value) < 0.002);
      near = w ? `, starts on “${w.text}”` : value <= 0.001 ? ", the clip’s start" : "";
    } else {
      const w = words.find((x) => Math.abs(x.end_s - value) < 0.002);
      near = w ? `, ends after “${w.text}”` : value >= this.ctx.duration - 0.001 ? ", the clip’s end" : "";
    }
    handle.setAttribute("aria-valuenow", value.toFixed(1));
    handle.setAttribute("aria-valuetext", `${stamp(value)}${near}`);
  }

  drawMemes(memes, labels) {
    const d = this.ctx.duration;
    while (this.memeNodes.length > memes.length) this.memeNodes.pop().remove();
    while (this.memeNodes.length < memes.length) {
      const k = this.memeNodes.length;
      const node = document.createElement("button");
      node.type = "button";
      node.className = "ed-tl__meme";
      node.dataset.k = String(k);
      node.dataset.track = "editor-timeline-meme";
      node.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight [ ] S Delete");
      const label = document.createElement("span");
      label.className = "ed-tl__meme-label";
      const grip = document.createElement("span");
      grip.className = "ed-tl__meme-grip";
      grip.dataset.grip = "";
      grip.setAttribute("aria-hidden", "true");
      node.append(label, grip);
      this.memesRow.append(node);
      this.memeNodes.push(node);
    }
    // Drawn as the engine will show them: cut short where the next meme starts.
    const spans = memeSpans(memes, this.ctx.limits.memeMin);
    memes.forEach((m, k) => {
      const node = this.memeNodes[k];
      const name = labels.get(m.id) || "Meme";
      const shows = spans[k].end - spans[k].start;
      node.style.left = pct(m.start_s, d);
      node.style.width = pct(shows, d);
      node.dataset.sound = String(m.sound);
      node.dataset.narrow = String(shows * this.pps < 48); // too short to label at this zoom
      node.dataset.selected = String(k === this.selected);
      node.dataset.clash = String(spans[k].clash);
      const label = node.firstElementChild;
      if (label.textContent !== name) label.textContent = name;
      const why = spans[k].clash ? ", starts too close to another meme" : spans[k].cut ? ", cut short by the next meme" : "";
      node.setAttribute("aria-label",
        `${name}, from ${stamp(m.start_s)} for ${secs(shows)}, sound ${m.sound ? "on" : "off"}${why}`);
      node.setAttribute("aria-pressed", String(k === this.selected));
    });
  }

  select(k) {
    this.selected = k;
    this.memeNodes.forEach((node, n) => {
      node.dataset.selected = String(n === k);
      node.setAttribute("aria-pressed", String(n === k));
    });
  }

  focusMeme(k) {
    const node = this.memeNodes[k];
    if (node) node.focus();
  }

  setTime(t, follow = false) {
    if (!this.ctx) return;
    this.time = t;
    this.playhead.style.left = pct(clamp(t, 0, this.ctx.duration), this.ctx.duration);
    if (follow && !this.fit) {
      const x = GUTTER + t * this.pps;
      const view = this.scroll;
      if (x < view.scrollLeft + 24 || x > view.scrollLeft + view.clientWidth - 24) view.scrollLeft = Math.max(0, x - 48);
    }
  }

  /* ---- scrubbing ----------------------------------------------------------------------------- */

  bindScrub() {
    let scrubbing = false;
    this.canvas.addEventListener("pointerdown", (e) => {
      if (!this.ctx || e.button > 0) return;
      if (e.target.closest(".ed-tl__handle, .ed-tl__meme")) return;
      const word = e.target.closest(".ed-tl__word");
      if (word) {
        const w = this.ctx.words.find((x) => String(x.i) === word.dataset.i);
        if (w) {
          this.onSeek(w.start_s);
          this.onSelectWord(w.i);
        }
        return;
      }
      this.onSeek(this.timeAt(e.clientX));
      // A finger drags the timeline sideways (native scroll); a mouse or pen scrubs.
      if (e.pointerType !== "touch") {
        scrubbing = true;
        this.canvas.setPointerCapture(e.pointerId);
      }
    });
    this.canvas.addEventListener("pointermove", (e) => {
      if (scrubbing) this.onSeek(this.timeAt(e.clientX));
    });
    const stop = () => {
      scrubbing = false;
    };
    this.canvas.addEventListener("pointerup", stop);
    this.canvas.addEventListener("pointercancel", stop);
  }

  /* ---- trim handles -------------------------------------------------------------------------- */

  trimOptions(extra = {}) {
    return { words: this.ctx.words, duration: this.ctx.duration, minLen: Math.min(this.ctx.limits.minClip, this.ctx.duration), ...extra };
  }

  bindHandles() {
    for (const which of ["start", "end"]) {
      const handle = this.handles[which];
      const key = which === "start" ? "start_s" : "end_s";
      let dragging = false;
      handle.addEventListener("pointerdown", (e) => {
        if (!this.ctx || e.button > 0) return;
        e.preventDefault();
        handle.focus();
        handle.setPointerCapture(e.pointerId);
        dragging = true;
        handle.dataset.dragging = "true";
        this.store.begin();
      });
      handle.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const t = this.timeAt(e.clientX);
        this.store.change((s) => {
          s.trim = placeTrim(s.trim, which, t, this.trimOptions({ free: e.altKey }));
        }, { reason: "trim" });
        this.onSeek(this.store.state.trim[key] - (which === "end" ? 1 / 30 : 0));
      });
      const end = () => {
        if (!dragging) return;
        dragging = false;
        delete handle.dataset.dragging;
        this.store.end();
      };
      handle.addEventListener("pointerup", end);
      handle.addEventListener("pointercancel", end);
      handle.addEventListener("lostpointercapture", end);

      handle.addEventListener("keydown", (e) => {
        if (!this.ctx) return;
        const opts = this.trimOptions();
        const minLen = opts.minLen;
        let next = null;
        const trim = this.store.state.trim;
        if (e.key === "ArrowRight" || e.key === "ArrowUp") next = stepTrim(trim, which, 1, { ...opts, fine: e.shiftKey ? 0.1 : 0 });
        else if (e.key === "ArrowLeft" || e.key === "ArrowDown") next = stepTrim(trim, which, -1, { ...opts, fine: e.shiftKey ? 0.1 : 0 });
        else if (e.key === "PageUp") next = stepTrim(trim, which, 1, { ...opts, fine: 1 });
        else if (e.key === "PageDown") next = stepTrim(trim, which, -1, { ...opts, fine: 1 });
        else if (e.key === "Home") next = { ...trim, [key]: which === "start" ? 0 : ms(Math.min(this.ctx.duration, trim.start_s + minLen)) };
        else if (e.key === "End") next = { ...trim, [key]: which === "start" ? ms(Math.max(0, trim.end_s - minLen)) : this.ctx.duration };
        if (next === null) return;
        e.preventDefault();
        this.store.change((s) => {
          s.trim = next;
        }, { key: `trim-${which}`, reason: "trim" });
        this.onSeek(this.store.state.trim[key] - (which === "end" ? 1 / 30 : 0));
      });
    }
  }

  /* ---- memes --------------------------------------------------------------------------------- */

  bindMemes() {
    let drag = null;
    this.memesRow.addEventListener("pointerdown", (e) => {
      const node = e.target.closest(".ed-tl__meme");
      if (!node || !this.ctx || e.button > 0) return;
      e.preventDefault();
      const k = Number(node.dataset.k);
      const meme = this.store.state.memes[k];
      if (!meme) return;
      node.focus();
      node.setPointerCapture(e.pointerId);
      this.onSelectMeme(k);
      drag = { k, node, x: e.clientX, mode: e.target.closest("[data-grip]") ? "resize" : "move", orig: { ...meme }, moved: false };
      this.store.begin();
    });
    this.memesRow.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const px = e.clientX - drag.x;
      if (!drag.moved && Math.abs(px) < 3) return;
      drag.moved = true;
      const rect = this.canvas.getBoundingClientRect();
      const dt = (px / rect.width) * this.ctx.duration;
      const { k, orig, mode } = drag;
      this.store.change((s) => {
        const moved = mode === "move" ? { ...orig, start_s: orig.start_s + dt } : { ...orig, duration_s: orig.duration_s + dt };
        s.memes[k] = fitMeme(moved, this.ctx);
      }, { reason: "meme" });
    });
    const end = () => {
      if (!drag) return;
      const { k, moved } = drag;
      drag = null;
      this.store.end();
      const meme = this.store.state.memes[k];
      if (meme) this.onSeek(moved ? meme.start_s : meme.start_s + 0.01);
    };
    this.memesRow.addEventListener("pointerup", end);
    this.memesRow.addEventListener("pointercancel", end);

    // Enter or Space on a focused meme (a click with no pointer) selects it and jumps to it.
    this.memesRow.addEventListener("click", (e) => {
      const node = e.target.closest(".ed-tl__meme");
      if (!node || e.detail !== 0) return;
      const k = Number(node.dataset.k);
      this.onSelectMeme(k);
      const meme = this.store.state.memes[k];
      if (meme) this.onSeek(meme.start_s + 0.01);
    });

    this.memesRow.addEventListener("keydown", (e) => {
      const node = e.target.closest(".ed-tl__meme");
      if (!node || !this.ctx) return;
      const k = Number(node.dataset.k);
      const meme = this.store.state.memes[k];
      if (!meme) return;
      const move = e.shiftKey ? 1 : 0.1;
      const grow = e.shiftKey ? 0.5 : 0.1;
      let change = null;
      if (e.key === "ArrowRight") change = { start_s: meme.start_s + move };
      else if (e.key === "ArrowLeft") change = { start_s: meme.start_s - move };
      else if (e.key === "]") change = { duration_s: meme.duration_s + grow };
      else if (e.key === "[") change = { duration_s: meme.duration_s - grow };
      else if (e.key === "s" || e.key === "S") change = { sound: !meme.sound };
      else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        this.store.change((s) => {
          s.memes.splice(k, 1);
        }, { reason: "meme-remove" });
        this.onSelectMeme(null);
        const next = Math.min(k, this.store.state.memes.length - 1);
        if (next >= 0) this.focusMeme(next);
        else this.handles.start.focus();
        return;
      }
      if (!change) return;
      e.preventDefault();
      this.onSelectMeme(k);
      this.store.change((s) => {
        s.memes[k] = fitMeme({ ...s.memes[k], ...change }, this.ctx);
      }, { key: `meme-${k}-${Object.keys(change)[0]}`, reason: "meme" });
      if ("start_s" in change) this.onSeek(this.store.state.memes[k].start_s + 0.01);
    });
  }
}
