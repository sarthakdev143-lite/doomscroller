// Everything the WebGL layer shows is drawn here, on 2D canvases, from the product's own geometry: no footage.
//
//   sourceAtlas()  the "episode": frames of the two-person set from partials/marks.html scene() (same shapes, same
//                  colours, via Path2D), as a wide shot, a close on the host and a close on the guest, each graded a
//                  little differently, so the ribbon reads as video.
//   ShortCanvas    a finished Short, laid out like the renderer (app/clipper/captions.py framed_layout, mirrored in
//                  partials/clip.html): progress bar, hook box, a 16:9 picture over a dimmed copy of itself, credit,
//                  word captions with the current word in caption yellow. Drawn per frame while it is on screen.

const YELLOW = "#ffe500";

/* ---- the set, from marks.html scene(): a 160x90 drawing ------------------------------------------------- */
const P = (d) => new Path2D(d);
const SET = {
  speakerA: P("M25 90 V63 a13 13 0 0 1 13 -13 h12 a13 13 0 0 1 13 13 V90 z"),
  hairA: P("M35 38 a9 9 0 0 1 18 0 c0 -3 -3 -9 -9 -9 s-9 6 -9 9z"),
  speakerB: P("M97 90 V65 a13 13 0 0 1 13 -13 h12 a13 13 0 0 1 13 13 V90 z"),
  hairB: P("M107 40 a9 9 0 0 1 18 0 c0 -6 -4 -10 -9 -10 s-9 4 -9 10z"),
  boomA: P("M64 20 L57 44"),
  boomB: P("M96 20 L103 46"),
};

function rect(ctx, x, y, w, h, fill, r = 0) {
  ctx.fillStyle = fill;
  if (r) {
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, r);
    ctx.fill();
  } else {
    ctx.fillRect(x, y, w, h);
  }
}

/** The set at 160x90 units. `talk` opens a mouth (0 none, 1 host, 2 guest); `t` nudges the heads. */
function drawSet(ctx, { talk = 0, t = 0 } = {}) {
  rect(ctx, 0, 0, 160, 90, "#3a4344");
  rect(ctx, 0, 0, 160, 58, "#414b4c");
  // a warm practical lamp behind the host, a cool window behind the guest
  const lamp = ctx.createRadialGradient(22, 18, 1, 22, 18, 34);
  lamp.addColorStop(0, "rgba(255,196,120,0.55)");
  lamp.addColorStop(1, "rgba(255,196,120,0)");
  ctx.fillStyle = lamp;
  ctx.fillRect(0, 0, 80, 60);
  const win = ctx.createLinearGradient(118, 0, 160, 50);
  win.addColorStop(0, "rgba(150,190,210,0.28)");
  win.addColorStop(1, "rgba(150,190,210,0)");
  ctx.fillStyle = win;
  ctx.fillRect(110, 0, 50, 58);
  rect(ctx, 64, 8, 13, 40, "#4b5658", 1.5);
  rect(ctx, 80, 8, 13, 40, "#4b5658", 1.5);
  rect(ctx, 8, 14, 30, 2, "#2f3637");
  rect(ctx, 12, 7, 5, 7, "#7a6a55");
  rect(ctx, 19, 9, 4, 5, "#5d6b5c");
  rect(ctx, 25, 6, 6, 8, "#8a7d6b");
  rect(ctx, 124, 14, 28, 2, "#2f3637");
  rect(ctx, 128, 8, 7, 6, "#6f6250");
  rect(ctx, 139, 10, 9, 4, "#556163");

  const nodA = Math.sin(t * 2.1) * 0.6;
  const nodB = Math.sin(t * 1.7 + 1) * 0.6;
  // speaker A
  ctx.fillStyle = "#c4b196";
  ctx.fill(SET.speakerA);
  rect(ctx, 41, 45, 6, 7, "#a97c5f");
  ctx.save();
  ctx.translate(0, nodA);
  ctx.fillStyle = "#b4876a";
  ctx.beginPath();
  ctx.arc(44, 39, 9, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#2b2420";
  ctx.fill(SET.hairA);
  if (talk === 1) rect(ctx, 46.5, 42.2, 3.2, 1.6, "#5a3328", 0.8);
  ctx.restore();
  ctx.strokeStyle = "#151515";
  ctx.lineWidth = 1.6;
  ctx.stroke(SET.boomA);
  rect(ctx, 53, 42, 7, 11, "#151515", 3.5);
  // speaker B
  ctx.fillStyle = "#5d7584";
  ctx.fill(SET.speakerB);
  rect(ctx, 113, 47, 6, 7, "#6f4a35");
  ctx.save();
  ctx.translate(0, nodB);
  ctx.fillStyle = "#7a523d";
  ctx.beginPath();
  ctx.arc(116, 41, 9, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#1d1a18";
  ctx.fill(SET.hairB);
  if (talk === 2) rect(ctx, 110.5, 44.2, 3.2, 1.6, "#3b2219", 0.8);
  ctx.restore();
  ctx.stroke(SET.boomB);
  rect(ctx, 100, 44, 7, 11, "#151515", 3.5);
  // desk
  rect(ctx, 0, 74, 160, 16, "#1c1d1d");
  rect(ctx, 0, 74, 160, 1.2, "#2a2c2c");
}

/** Draw the set into (x, y, w, h), framed: `shot` 0 wide, 1 close on the host, 2 close on the guest. */
function drawShot(ctx, x, y, w, h, { shot = 0, talk = 0, t = 0, grade = 0 } = {}) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  ctx.translate(x, y);
  // cover-fit 160x90 into w x h
  const s = Math.max(w / 160, h / 90);
  ctx.translate((w - 160 * s) / 2, (h - 90 * s) / 2);
  ctx.scale(s, s);
  if (shot) {
    // the renderer's speaker focus: 1.35x pinned to that side (clip.css .clip__cam transform-origin 0%/100% 55%)
    const ox = shot === 1 ? 0 : 160;
    ctx.translate(ox, 49.5);
    ctx.scale(1.35, 1.35);
    ctx.translate(-ox, -49.5);
  }
  drawSet(ctx, { talk, t });
  ctx.restore();
  // grade: a little warmth or coolness per frame, and a vignette, so the strip reads as footage
  ctx.save();
  if (grade) {
    ctx.fillStyle = grade > 0 ? `rgba(255,170,80,${0.07 * grade})` : `rgba(70,120,170,${-0.08 * grade})`;
    ctx.fillRect(x, y, w, h);
  }
  const v = ctx.createRadialGradient(x + w / 2, y + h / 2, h * 0.3, x + w / 2, y + h / 2, w * 0.62);
  v.addColorStop(0, "rgba(0,0,0,0)");
  v.addColorStop(1, "rgba(0,0,0,0.42)");
  ctx.fillStyle = v;
  ctx.fillRect(x, y, w, h);
  ctx.restore();
}

/** A grid of source frames for the ribbon shader: `cols` x `rows` cells of 16:9. */
export function sourceAtlas({ cellW = 384, cols = 4, rows = 2 } = {}) {
  const cellH = Math.round((cellW * 9) / 16);
  const canvas = document.createElement("canvas");
  canvas.width = cellW * cols;
  canvas.height = cellH * rows;
  const ctx = canvas.getContext("2d");
  const looks = [
    { shot: 0, talk: 1, grade: 0.4 },
    { shot: 1, talk: 1, grade: 0.8 },
    { shot: 2, talk: 2, grade: -0.6 },
    { shot: 0, talk: 2, grade: -0.2 },
    { shot: 1, talk: 0, grade: 0.5, t: 1.2 },
    { shot: 2, talk: 0, grade: -0.9, t: 2.1 },
    { shot: 0, talk: 0, grade: 0.1, t: 0.7 },
    { shot: 1, talk: 1, grade: 1, t: 2.8 },
  ];
  for (let i = 0; i < cols * rows; i++) {
    const look = looks[i % looks.length];
    drawShot(ctx, (i % cols) * cellW, Math.floor(i / cols) * cellH, cellW, cellH, look);
  }
  // film grain, baked once
  const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const d = img.data;
  let seed = 7;
  for (let p = 0; p < d.length; p += 4) {
    seed = (seed * 16807) % 2147483647;
    const n = ((seed / 2147483647) - 0.5) * 14;
    d[p] += n;
    d[p + 1] += n;
    d[p + 2] += n;
  }
  ctx.putImageData(img, 0, 0);
  return { canvas, cols, rows, count: cols * rows };
}

/* ---- a finished Short -------------------------------------------------------------------------------------- */
const WORD_S = 0.38; // redesign.js WORD_MS: the drawn Shorts step one word every 380 ms
const HOOK_S = 2.5;

function wrapLines(ctx, text, maxWidth) {
  const words = text.split(" ");
  const lines = [];
  let line = "";
  for (const w of words) {
    const test = line ? `${line} ${w}` : w;
    if (ctx.measureText(test).width > maxWidth && line) {
      lines.push(line);
      line = w;
    } else {
      line = test;
    }
  }
  if (line) lines.push(line);
  return lines;
}

export class ShortCanvas {
  /**
   * @param {{hook: string, groups: string[], focus: string}} tile  one of redesign.DEMO_TILES
   * @param {number} width  canvas width in px (height is 16:9 of it, portrait)
   */
  constructor(tile, width = 360) {
    this.tile = tile;
    this.canvas = document.createElement("canvas");
    this.canvas.width = width;
    this.canvas.height = Math.round((width * 16) / 9);
    this.ctx = this.canvas.getContext("2d");
    this.groups = tile.groups.map((g) => g.split(" "));
    this.steps = this.groups.flatMap((words, g) => words.map((_, w) => [g, w]));
    this.duration = this.steps.length * WORD_S + 1.4;
    this.focusStart = tile.focus === "b" ? 2 : 1;
    this.backdrop = this._backdrop();
    this.video = null;
  }

  /** The renderer's backdrop: the same footage, zoomed to fill, darkened, desaturated and soft. Drawn once. */
  _backdrop() {
    const { width: W, height: H } = this.canvas;
    const c = document.createElement("canvas");
    c.width = Math.round(W / 4);
    c.height = Math.round(H / 4);
    const x = c.getContext("2d");
    drawShot(x, 0, 0, c.width, c.height, { shot: this.focusStart, t: 0.4 });
    const out = document.createElement("canvas");
    out.width = W;
    out.height = H;
    const o = out.getContext("2d");
    o.imageSmoothingQuality = "high";
    if ("filter" in o) o.filter = "blur(6px) saturate(0.45) brightness(0.5)";
    o.drawImage(c, -W * 0.04, -H * 0.04, W * 1.08, H * 1.08);
    o.filter = "none";
    o.fillStyle = "rgba(8,8,8,0.28)";
    o.fillRect(0, 0, W, H);
    return out;
  }

  /** Draw the clip at time t (seconds, looping), with `pop` (0..1) for the current word's spring. */
  draw(t, { pop = 1, punch = 0 } = {}) {
    const ctx = this.ctx;
    const { width: W, height: H } = this.canvas;
    const time = ((t % this.duration) + this.duration) % this.duration;
    const i = Math.min(this.steps.length - 1, Math.floor(Math.max(0, time - 0.15) / WORD_S));
    const [g, w] = this.steps[i];

    ctx.drawImage(this.backdrop, 0, 0);
    // the picture: 16:9, full width, at the renderer's y (34.17% .. 65.83%)
    const py = H * 0.3417;
    const ph = H * 0.3167;
    const shot = g % 2 === 0 ? this.focusStart : 3 - this.focusStart; // the focus changes on the beat
    const shake = punch ? Math.sin(time * 90) * punch * W * 0.012 : 0;
    drawShot(ctx, shake, py, W, ph, { shot, talk: shot, t: time, grade: shot === 1 ? 0.5 : -0.4 });
    if (punch) {
      ctx.fillStyle = `rgba(255,255,255,${0.35 * punch})`;
      ctx.fillRect(0, py, W, ph);
    }

    // progress bar along the top
    const bh = Math.max(3, W * 0.0093);
    ctx.fillStyle = "rgba(255,255,255,0.12)";
    ctx.fillRect(0, 0, W, bh);
    ctx.fillStyle = YELLOW;
    ctx.fillRect(0, 0, W * Math.min(1, time / this.duration), bh);

    // hook, first 2.5 s: caption yellow box, dark heavy capitals
    const hookAlpha = time < HOOK_S ? 1 : Math.max(0, 1 - (time - HOOK_S) / 0.25);
    if (hookAlpha > 0) {
      ctx.save();
      ctx.globalAlpha = hookAlpha;
      const fs = W * 0.062;
      ctx.font = `800 ${fs}px Archivo, "Arial Narrow", Arial, sans-serif`;
      if ("fontStretch" in ctx) ctx.fontStretch = "semi-condensed";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const lines = wrapLines(ctx, this.tile.hook.toUpperCase(), W * 0.74);
      const lh = fs * 1.3;
      const top = H * 0.1854 + fs * 0.6;
      lines.forEach((line, k) => {
        const tw = ctx.measureText(line).width;
        const y = top + k * lh;
        ctx.fillStyle = YELLOW;
        ctx.fillRect(W / 2 - tw / 2 - fs * 0.3, y - fs * 0.62, tw + fs * 0.6, fs * 1.24);
        ctx.fillStyle = "#111";
        ctx.fillText(line, W / 2, y + fs * 0.04);
      });
      ctx.restore();
    }

    // credit line
    ctx.save();
    ctx.font = `700 ${W * 0.037}px Archivo, Arial, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.shadowColor = "#000";
    ctx.shadowBlur = W * 0.006;
    ctx.fillStyle = "rgba(255,255,255,0.82)";
    ctx.fillText("Credit: @yourchannel", W / 2, H * 0.676);
    ctx.restore();

    // word captions: the group on screen, the current word yellow and 112%
    ctx.save();
    let fs = W * 0.076;
    const setFont = () => {
      ctx.font = `800 ${fs}px Archivo, "Arial Narrow", Arial, sans-serif`;
      if ("fontStretch" in ctx) ctx.fontStretch = "semi-condensed";
    };
    setFont();
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    const words = this.groups[g].map((s) => s.toUpperCase());
    let widths = words.map((s) => ctx.measureText(s).width);
    let total = widths.reduce((a, b) => a + b, 0) + fs * 0.28 * (words.length - 1) + fs * 0.25;
    // the renderer keeps a caption inside the frame: a long group is set smaller, never cropped
    if (total > W * 0.88) {
      fs *= (W * 0.88) / total;
      setFont();
      widths = words.map((s) => ctx.measureText(s).width);
      total = widths.reduce((a, b) => a + b, 0) + fs * 0.28 * (words.length - 1) + fs * 0.25;
    }
    const gap = fs * 0.28;
    let x = (W - total) / 2;
    const y = H * 0.747;
    words.forEach((word, k) => {
      const active = k === w;
      const s = active ? 1 + 0.12 * pop : 1;
      const cx = x + widths[k] / 2 + (active ? fs * 0.12 : 0);
      ctx.save();
      ctx.translate(cx, y);
      ctx.scale(s, s);
      ctx.textAlign = "center";
      ctx.lineWidth = fs * 0.2;
      ctx.strokeStyle = "#000";
      ctx.strokeText(word, 0, 0);
      ctx.fillStyle = active ? YELLOW : "#fff";
      ctx.fillText(word, 0, 0);
      ctx.restore();
      x += widths[k] + gap + (active ? fs * 0.25 : 0);
    });
    ctx.restore();
    return { word: i, group: g };
  }

  /** Index of the word on screen at time t (for word-change sounds and pops). */
  wordAt(t) {
    const time = ((t % this.duration) + this.duration) % this.duration;
    return Math.min(this.steps.length - 1, Math.floor(Math.max(0, time - 0.15) / WORD_S));
  }
}

/** Real clips, when the owner has put them in static/demo/: muted, looping, inline, never autoplayed by markup. */
export function demoVideo(src) {
  const v = document.createElement("video");
  v.src = src;
  v.muted = true;
  v.loop = true;
  v.playsInline = true;
  v.preload = "auto";
  v.crossOrigin = "anonymous";
  return v;
}

/** A soft round sprite for dust and glows. */
export function softDot(size = 64) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const x = c.getContext("2d");
  const g = x.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.35, "rgba(255,255,255,0.35)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  x.fillStyle = g;
  x.fillRect(0, 0, size, size);
  return c;
}
