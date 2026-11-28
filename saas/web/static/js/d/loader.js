// The loader. The number counts the bytes of the film's critical frames as they arrive: it eases toward what has
// really loaded and never runs ahead of it, so it can pause, but it never fakes a 99. A 1 px frame draws itself
// round the screen as it fills (top, right, bottom, left). At 100 it holds a beat and then cuts: one frame it is
// there, the next the film is.

export class Loader {
  constructor(el) {
    this.el = el;
    this.digits = [...el.querySelectorAll("[data-d-digit]")];
    this.sides = [...el.querySelectorAll(".d-loader__frame i")];
    this.real = 0; // 0..1, bytes loaded
    this.shown = 0; // 0..100, what the number says
    this.last = 0;
    this.done = null;
  }

  set(fraction) {
    this.real = Math.max(this.real, Math.min(1, fraction));
  }

  /** Advance the display; returns true once it has reached 100. */
  tick(now) {
    const dt = this.last ? Math.min(0.1, (now - this.last) / 1000) : 0.016;
    this.last = now;
    const target = this.real * 100;
    // at most ~95 per second, at least a steady crawl while bytes are arriving
    const step = Math.max(6 * dt, (target - this.shown) * (1 - Math.exp(-dt * 7)));
    this.shown = Math.min(target, this.shown + Math.min(step, 95 * dt));
    const n = Math.floor(this.shown + 1e-6);
    const s = String(n).padStart(3, "0");
    this.digits.forEach((d, i) => {
      if (d.textContent !== s[i]) d.textContent = s[i];
    });
    this.el.setAttribute("aria-valuenow", String(n));
    const f = this.shown / 100;
    this.sides.forEach((side, i) => {
      const k = Math.max(0, Math.min(1, f * 4 - i));
      side.style.transform = `scale${i % 2 ? "Y" : "X"}(${k})`;
    });
    return n >= 100;
  }

  cut() {
    this.el.hidden = true;
  }
}
