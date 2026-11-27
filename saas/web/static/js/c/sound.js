// Sound for direction C: off until the visitor presses the toggle, and every note synthesised here, so there is
// nothing to license or download (the product's own effects are synthesised the same way, app/clipper/sfx.py).
//   room  a low, quiet bed whose filter opens with scroll speed
//   snap  a cut: a bright click with a short burst of noise
//   lock  a pane locking in: a soft low thud
//   tick  a caption word arriving

export class Sound {
  constructor() {
    this.ctx = null;
    this.on = false;
    this.lastTick = 0;
  }

  _build() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return false;
    const ctx = new AC();
    this.ctx = ctx;
    this.master = ctx.createGain();
    this.master.gain.value = 0;
    this.master.connect(ctx.destination);

    // the room: two detuned low sines and a whisper of noise, through one low-pass
    this.roomFilter = ctx.createBiquadFilter();
    this.roomFilter.type = "lowpass";
    this.roomFilter.frequency.value = 260;
    this.roomFilter.Q.value = 0.7;
    const roomGain = ctx.createGain();
    roomGain.gain.value = 0.11;
    this.roomFilter.connect(roomGain).connect(this.master);
    [55, 82.6, 110.3].forEach((f, i) => {
      const o = ctx.createOscillator();
      o.type = i === 2 ? "triangle" : "sine";
      o.frequency.value = f;
      const g = ctx.createGain();
      g.gain.value = i === 2 ? 0.12 : 0.5;
      o.connect(g).connect(this.roomFilter);
      o.start();
    });
    this.noise = this._noiseBuffer(2);
    const hiss = ctx.createBufferSource();
    hiss.buffer = this.noise;
    hiss.loop = true;
    const hissGain = ctx.createGain();
    hissGain.gain.value = 0.05;
    hiss.connect(hissGain).connect(this.roomFilter);
    hiss.start();
    return true;
  }

  _noiseBuffer(seconds) {
    const ctx = this.ctx;
    const buf = ctx.createBuffer(1, ctx.sampleRate * seconds, ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    return buf;
  }

  async toggle() {
    if (!this.ctx && !this._build()) return false;
    this.on = !this.on;
    if (this.on && this.ctx.state === "suspended") await this.ctx.resume();
    const now = this.ctx.currentTime;
    this.master.gain.cancelScheduledValues(now);
    this.master.gain.setTargetAtTime(this.on ? 0.7 : 0, now, 0.25);
    return this.on;
  }

  /** Scroll speed (px/s) opens the room's filter: moving fast sounds like moving. */
  speed(v) {
    if (!this.on) return;
    const f = 220 + Math.min(1600, Math.abs(v) * 0.9);
    this.roomFilter.frequency.setTargetAtTime(f, this.ctx.currentTime, 0.12);
  }

  _burst({ at = 0, dur = 0.08, type = "highpass", freq = 2400, q = 0.8, gain = 0.5 }) {
    const ctx = this.ctx;
    const t = ctx.currentTime + at;
    const src = ctx.createBufferSource();
    src.buffer = this.noise;
    const f = ctx.createBiquadFilter();
    f.type = type;
    f.frequency.value = freq;
    f.Q.value = q;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(gain, t + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    src.connect(f).connect(g).connect(this.master);
    src.start(t, Math.random() * 1.5);
    src.stop(t + dur + 0.02);
  }

  _tone({ at = 0, from = 180, to = 60, dur = 0.18, gain = 0.6, type = "sine" }) {
    const ctx = this.ctx;
    const t = ctx.currentTime + at;
    const o = ctx.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(from, t);
    o.frequency.exponentialRampToValueAtTime(to, t + dur);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(gain, t + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(this.master);
    o.start(t);
    o.stop(t + dur + 0.02);
  }

  snap() {
    if (!this.on) return;
    this._burst({ dur: 0.05, freq: 3200, gain: 0.55 });
    this._tone({ from: 2400, to: 900, dur: 0.035, gain: 0.25, type: "square" });
    this._burst({ at: 0.012, dur: 0.16, type: "bandpass", freq: 900, q: 1.4, gain: 0.18 });
  }

  lock() {
    if (!this.on) return;
    this._tone({ from: 150, to: 48, dur: 0.24, gain: 0.8 });
    this._burst({ dur: 0.03, freq: 1800, gain: 0.25 });
  }

  tick() {
    if (!this.on) return;
    const now = performance.now();
    if (now - this.lastTick < 70) return;
    this.lastTick = now;
    this._tone({ from: 1900, to: 1500, dur: 0.03, gain: 0.08, type: "triangle" });
  }

  close() {
    this.ctx?.close().catch(() => {});
    this.ctx = null;
    this.on = false;
  }
}
