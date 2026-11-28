// The opening's clock. One playhead (a fractional film frame) drives everything on screen: the film, and the
// words, whose entrances are keyed to film frames rather than to the scroll, so they can never drift apart.
//
//   after the loader's cut  the intro runs on its own clock (the strip lights up, the camera settles)
//   scroll                  sets the target frame; the playhead follows it, spring-damped
//   rest                    when nothing moves for a moment on a beat, the playhead breathes slowly across that
//                           beat's plateau (the film was rendered with a slow drift there), so the picture never
//                           freezes into a still
//   drag                    pulls the film forward or back, elastically; let go and it settles back
//   pointer                 a small warm light on the glass (see film.js)

const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const smooth = (t) => {
  const x = clamp(t, 0, 1);
  return x * x * (3 - 2 * x);
};

export class Story {
  constructor({ film, store, manifest, story, lines, cue, hover, onFrame }) {
    this.film = film;
    this.store = store;
    this.m = manifest;
    this.F = manifest.frames;
    const b = manifest.beats;
    this.beats = b;
    this.plateaus = [b.s1, b.s2, b.s3];
    // scroll progress -> film frame: the beats' plateaus get short stretches of scroll, the cut the longest
    this.map = [
      [0, b.s1[0]],
      [0.07, b.s1[1]],
      [0.32, b.s2[0]],
      [0.42, b.s2[1]],
      [0.9, b.s3[0]],
      [1, b.s3[1]],
    ];
    // each line's entrance and exit, in film frames
    this.lineWindows = {
      1: [b.intro[1] - 18, b.intro[1], b.s1[1] + 8, b.s1[1] + 24],
      2: [b.s2[0] - 18, b.s2[0], b.cut[0] + 2, b.cut[0] + 12],
      3: [b.cut[0] + 3, b.cut[0] + 18, Infinity, Infinity],
    };
    this.story = story;
    this.lines = lines;
    this.cue = cue;
    this.hover = hover;
    this.onFrame = onFrame;

    this.head = 1;
    this.target = 1;
    this.intro = null; // start time of the intro clock
    this.scrolled = false;
    this.lastInput = 0;
    this.breath = { amp: 0, phase: 0 };
    this.drag = { on: false, x0: 0, offset: 0, vel: 0 };
    this.light = { x: 0.5, y: 0.55, tx: 0.5, ty: 0.55, k: 0, tk: 0 };
    this.drawn = "";
    this.stats = { frames: 0, intervals: [], work: [] };
    this.last = 0;
    this.measure();
    this.bind();
  }

  measure() {
    const r = this.story.getBoundingClientRect();
    this.top = r.top + window.scrollY;
    this.range = Math.max(1, this.story.offsetHeight - window.innerHeight);
  }

  progress() {
    return clamp((window.scrollY - this.top) / this.range, 0, 1);
  }

  frameAt(p) {
    const m = this.map;
    for (let i = 0; i < m.length - 1; i++) {
      if (p <= m[i + 1][0]) {
        const t = (p - m[i][0]) / (m[i + 1][0] - m[i][0]);
        return m[i][1] + (m[i + 1][1] - m[i][1]) * t;
      }
    }
    return m[m.length - 1][1];
  }

  bind() {
    const input = () => {
      this.lastInput = performance.now();
    };
    window.addEventListener(
      "scroll",
      () => {
        input();
        if (window.scrollY > 2) this.scrolled = true;
      },
      { passive: true },
    );
    window.addEventListener("wheel", input, { passive: true });
    window.addEventListener("keydown", input);
    window.addEventListener("resize", () => {
      this.measure();
      this.film.resize();
      this.drawn = "";
    });
    // drag to scrub: horizontal drags anywhere on the film (links and text selection keep their own behaviour)
    const down = (e) => {
      if (e.button !== 0 || e.target.closest("a, button, input, .d-foot")) return;
      this.drag.on = true;
      this.drag.x0 = e.clientX;
      this.drag.y0 = e.clientY;
      this.drag.base = this.drag.offset;
      this.drag.axis = e.pointerType === "mouse" ? "x" : null;
      this.drag.lastX = e.clientX;
      this.drag.lastT = performance.now();
      this.setLight(e.clientX, e.clientY, 1);
      input();
    };
    const move = (e) => {
      if (this.hover || this.drag.on) this.setLight(e.clientX, e.clientY, 1);
      if (!this.drag.on) return;
      const dx = e.clientX - this.drag.x0;
      const dy = e.clientY - this.drag.y0;
      if (!this.drag.axis && Math.hypot(dx, dy) > 8) this.drag.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      if (this.drag.axis !== "x") return;
      document.documentElement.classList.add("d-dragging");
      // pulling the strip left brings later footage in: forward in time
      const raw = this.drag.base - (dx / window.innerWidth) * 64;
      this.drag.offset = 40 * Math.tanh(raw / 40); // elastic past the first few seconds of pull
      const now = performance.now();
      this.drag.vel = (e.clientX - this.drag.lastX) / Math.max(1, now - this.drag.lastT);
      this.drag.lastX = e.clientX;
      this.drag.lastT = now;
      input();
    };
    const up = () => {
      if (!this.drag.on) return;
      this.drag.on = false;
      document.documentElement.classList.remove("d-dragging");
      if (!this.hover) this.light.tk = 0;
      input();
    };
    window.addEventListener("pointerdown", down);
    window.addEventListener("pointermove", move, { passive: true });
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    window.addEventListener("mouseout", (e) => {
      if (!e.relatedTarget && this.hover) this.light.tk = 0;
    });
  }

  setLight(x, y, k) {
    const [u, v] = this.film.toFrame(x, y);
    this.light.tx = u;
    this.light.ty = v;
    this.light.tk = k;
    if (this.light.k < 0.01) {
      this.light.x = u;
      this.light.y = v;
    }
  }

  startIntro(now) {
    this.intro = now;
    this.lastInput = now;
  }

  inPlateau(x) {
    return this.plateaus.find(([a, b]) => x >= a - 0.5 && x <= b + 0.5) || null;
  }

  tick(now) {
    const t0 = performance.now();
    const dt = this.last ? clamp((now - this.last) / 1000, 0, 0.1) : 1 / 60;
    if (this.last) {
      this.stats.intervals.push(now - this.last);
      if (this.stats.intervals.length > 900) this.stats.intervals.shift();
    }
    this.last = now;
    const b = this.beats;
    const p = this.progress();
    let scrollFrame = this.frameAt(p);

    // the intro: frames 1 .. intro end at the film's own rate, unless the visitor scrolls first
    let introOn = false;
    if (this.intro !== null && !this.scrolled) {
      const f = 1 + ((now - this.intro) / 1000) * this.m.fps;
      if (f < b.intro[1]) {
        introOn = true;
        scrollFrame = f;
      }
    }

    // breathing across a plateau when nothing has moved for a moment
    const idle = (now - this.lastInput) / 1000;
    const plateau = !introOn && !this.drag.on ? this.inPlateau(scrollFrame) : null;
    const wantAmp = plateau && idle > 1.2 ? smooth((idle - 1.2) / 2.2) : 0;
    const rate = wantAmp > this.breath.amp ? 1.2 : 5.0;
    this.breath.amp += (wantAmp - this.breath.amp) * (1 - Math.exp(-dt * rate));
    if (this.breath.amp < 0.002) this.breath.phase = 0;
    else this.breath.phase += dt * ((Math.PI * 2) / 9);
    let target = scrollFrame;
    if (plateau && this.breath.amp > 0.002) {
      // drift to the middle of the plateau and sway either side of it
      const [a, z] = plateau;
      const half = (z - a) / 2;
      const base = clamp(scrollFrame, a, z);
      const centre = base + (a + half - base) * this.breath.amp;
      target = clamp(centre + Math.sin(this.breath.phase) * half * 0.75 * this.breath.amp, a, z);
    }

    // drag: elastic offset, springs home when let go
    if (!this.drag.on && Math.abs(this.drag.offset) > 0.001) {
      this.drag.offset *= Math.exp(-dt * 4.2);
      if (Math.abs(this.drag.offset) < 0.01) this.drag.offset = 0;
    }
    target = clamp(target + this.drag.offset, 1, this.F);
    this.target = target;

    if (introOn && Math.abs(this.drag.offset) < 0.01) this.head = target;
    else this.head += (target - this.head) * (1 - Math.exp(-dt * 7.5));
    if (Math.abs(this.head - target) < 0.002) this.head = target;

    // the light trails the pointer a little, like something with mass
    const L = this.light;
    L.x += (L.tx - L.x) * (1 - Math.exp(-dt * 10));
    L.y += (L.ty - L.y) * (1 - Math.exp(-dt * 10));
    L.k += (L.tk - L.k) * (1 - Math.exp(-dt * (L.tk > L.k ? 6 : 2.5)));

    this.updateLines();
    if (this.cue) this.cue.classList.toggle("is-on", this.intro !== null && !introOn && p < 0.012);
    this.render();
    this.stats.work.push(performance.now() - t0);
    if (this.stats.work.length > 900) this.stats.work.shift();
  }

  updateLines() {
    const x = this.head;
    for (const [n, el] of Object.entries(this.lines)) {
      const [i0, i1, o0, o1] = this.lineWindows[n];
      const inn = this.intro === null ? 0 : smooth((x - i0) / (i1 - i0));
      const out = o0 === Infinity ? 0 : smooth((x - o0) / (o1 - o0));
      const y = (1 - inn) * 108 - out * 108;
      const key = `${y.toFixed(2)}`;
      if (el.dataset.y !== key) {
        el.dataset.y = key;
        el.style.transform = `translate3d(0, ${y.toFixed(2)}%, 0)`;
        el.parentElement.classList.toggle("is-on", inn > 0.001 && out < 0.999);
      }
    }
  }

  render(force = false) {
    const store = this.store;
    const x = this.head;
    const pair = store.around(x);
    if (!pair) return;
    const [lo, hi] = pair;
    const mix = hi > lo ? clamp((x - lo) / (hi - lo), 0, 1) : 0;
    const a = store.get(lo);
    const bb = hi === lo ? a : store.get(hi);
    let busy = 0;
    if (!a) {
      store.decode(lo);
      busy++;
    }
    if (!bb) {
      store.decode(hi);
      busy++;
    }
    // look ahead in the direction of travel
    const dir = this.target >= x ? 1 : -1;
    for (let k = 1; k <= 5 && store.pending.size < 4; k++) {
      const f = Math.round(x) + dir * k;
      if (f >= 1 && f <= this.F && store.have[f] && !store.get(f)) store.decode(f);
    }
    if (a) this.film.frames(lo, a, bb ? hi : lo, bb || a, bb ? mix : 0);

    const L = this.light;
    let lightKey = "";
    if (L.k > 0.004 && store.light) {
      const lp = store.lightPair(x);
      if (lp) {
        const [i0, i1, t] = lp;
        const b0 = store.get(-1 - i0);
        const b1 = store.get(-1 - i1);
        if (!b0) store.decodeLight(i0);
        if (!b1) store.decodeLight(i1);
        this.film.light(-1 - i0, b0, -1 - i1, b1, t, store.cam.light.encoding === "linear");
        lightKey = `${i0}:${t.toFixed(3)}:${!!b0}`;
      }
    } else {
      this.film.light(null, null);
    }
    this.film.pointer(L.x, L.y, L.k);
    const key = `${lo}:${hi}:${mix.toFixed(4)}:${!!a}:${!!bb}:${L.x.toFixed(4)}:${L.y.toFixed(4)}:${L.k.toFixed(3)}:${lightKey}`;
    if (force || key !== this.drawn) {
      this.drawn = key;
      this.film.draw();
      this.stats.frames++;
    }
    this.busy = busy;
    this.onFrame?.(this);
  }
}
