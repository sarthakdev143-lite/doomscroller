// The WebGL layer for direction C. Decorative (the canvas is aria-hidden): everything shown is also in the text.
//
// One continuous object tells the story. A long source ribbon, the episode, floats in depth. A light reads it
// (listen); at sentence boundaries three stretches snap free (cut); their 9:16 shards fly, flip and stack into
// phone-shaped panes that become finished Shorts (frame); one plays its captions word by word (caption); the three
// fan out (shorts), then gather into a deck beside the package card (package).
//
// Everything that moves is a pure function of the story position S (0 hero .. 7 pricing) plus J (the closing
// section), read through springs: scrolling back plays it backwards, and any scroll position always looks the same.
// Only the snap and lock moments are events (a flash, a kick, a sound), fired when S crosses them going forward.
import * as THREE from "../../vendor/three/three.module.min.js";
import { sourceAtlas, ShortCanvas, demoVideo, softDot } from "./textures.js";

/* ---- small maths -------------------------------------------------------------------------------------------- */
const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const seg = (s, a, b) => clamp((s - a) / (b - a));
const smooth = (t) => t * t * (3 - 2 * t);
const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const easeOut = (t) => 1 - Math.pow(1 - t, 3);
const mix3 = (a, b, t) => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];

/* ---- shaders ------------------------------------------------------------------------------------------------ */
const SHARD_VERT = /* glsl */ `
  attribute float aIndex;
  attribute float aSeg;
  attribute vec4 aState; // read, flash, dim, alpha
  uniform float uTime;
  uniform vec3 uMouse;
  uniform float uMouseAmt;
  varying vec2 vUv;
  varying vec4 vState;
  varying float vIndex;
  varying float vSeg;
  varying vec3 vWorld;
  varying float vDepth;
  void main() {
    vUv = uv;
    vState = aState;
    vIndex = aIndex;
    vSeg = aSeg;
    vec4 world = modelMatrix * instanceMatrix * vec4(position, 1.0);
    // the cursor presses a slow ripple into the strip
    float d = distance(world.xy, uMouse.xy);
    float ripple = exp(-d * d * 1.6) * sin(d * 7.0 - uTime * 3.2);
    world.z += uMouseAmt * 0.22 * ripple * (1.0 - aState.z);
    vWorld = world.xyz;
    vec4 view = viewMatrix * world;
    vDepth = -view.z;
    gl_Position = projectionMatrix * view;
  }
`;

const SHARD_FRAG = /* glsl */ `
  uniform sampler2D uAtlas;
  uniform vec2 uGrid;
  uniform float uFrames;
  uniform float uShardW;
  uniform float uFrameW;
  uniform float uScanX;
  uniform float uScanOn;
  uniform float uSlice;
  uniform float uTime;
  uniform vec3 uMouse;
  uniform float uMouseAmt;
  uniform float uFogNear;
  uniform float uFogFar;
  uniform float uMarked;
  varying vec2 vUv;
  varying vec4 vState;
  varying float vIndex;
  varying float vSeg;
  varying vec3 vWorld;
  varying float vDepth;

  float hash(float n) { return fract(sin(n * 12.9898) * 43758.5453); }

  vec3 frameAt(float gx, float v, float shift) {
    float f = floor((gx + shift) / uFrameW);
    float lu = fract((gx + shift) / uFrameW);
    float k = floor(hash(f + 3.0) * uFrames);
    vec2 cell = vec2(mod(k, uGrid.x), floor(k / uGrid.x));
    return texture2D(uAtlas, (cell + vec2(lu, v)) / uGrid).rgb;
  }

  void main() {
    // a faded shard must not write depth: the stacked ones sit just in front of their pane
    if (vState.w < 0.02) discard;
    float gx = (vIndex + vUv.x) * uShardW;
    float md = distance(vWorld.xy, uMouse.xy);
    float ca = uMouseAmt * 0.018 * exp(-md * md * 1.2);
    vec3 col;
    col.r = frameAt(gx, vUv.y, ca).r;
    col.g = frameAt(gx, vUv.y, 0.0).g;
    col.b = frameAt(gx, vUv.y, -ca).b;

    // unread footage is a grey memory of itself; the light gives it its colour back
    float lum = dot(col, vec3(0.299, 0.587, 0.114));
    vec3 unread = vec3(lum) * 0.56 + vec3(0.0, 0.012, 0.03);
    col = mix(unread, col * 1.06, vState.x);

    // the reading light
    float ds = vWorld.x - uScanX;
    float core = exp(-ds * ds * 60.0);
    float halo = exp(-ds * ds * 1.2);
    col += uScanOn * (vec3(1.0, 0.92, 0.25) * core * 0.9 + vec3(1.0, 0.85, 0.3) * halo * 0.16);

    // 16:9 frame seams, like a contact strip
    float lu = fract(gx / uFrameW);
    float seam = smoothstep(0.0, 0.01, lu) * smoothstep(0.0, 0.01, 1.0 - lu);
    col *= mix(0.45, 1.0, seam);

    // the slices: a hairline between shards that opens once the cut happens
    float ex = min(vUv.x, 1.0 - vUv.x);
    col *= 1.0 - (1.0 - smoothstep(0.0, 0.025, ex)) * uSlice * 0.9;

    // a marked stretch carries caption-yellow rails, top and bottom
    float rail = (1.0 - smoothstep(0.0, 0.018, vUv.y)) + (1.0 - smoothstep(0.0, 0.018, 1.0 - vUv.y));
    col = mix(col, vec3(1.0, 0.9, 0.0), clamp(rail, 0.0, 1.0) * step(0.0, vSeg) * uMarked);

    col += vState.y * vec3(1.0, 0.95, 0.55);
    col *= 1.0 - vState.z * 0.85;

    float fog = smoothstep(uFogNear, uFogFar, vDepth);
    col = mix(col, vec3(0.043, 0.043, 0.039), fog);
    gl_FragColor = vec4(col, vState.w);
  }
`;

const BAR_VERT = /* glsl */ `
  attribute float aX;
  attribute float aH;
  uniform float uTime;
  uniform float uScanX;
  uniform float uRead;
  uniform float uWave;
  uniform vec4 uSegs[3];
  uniform float uSink;
  uniform float uShow;
  varying float vRead;
  varying float vIn;
  varying float vY;
  float waveY(float x) { return uWave * 0.32 * sin(x * 0.33 + uTime * 0.4); }
  float waveZ(float x) { return uWave * 0.9 * sin(x * 0.17 + uTime * 0.27 + 1.3); }
  void main() {
    float lit = max(uRead, smoothstep(0.6, -0.2, aX - uScanX));
    float inClip = 0.0;
    for (int i = 0; i < 3; i++) inClip += step(uSegs[i].x, aX) * step(aX, uSegs[i].y);
    vRead = lit;
    vIn = inClip;
    vY = position.y;
    float h = aH * (0.25 + 0.75 * lit) * uShow;
    vec3 p = position * vec3(1.0, h, 1.0);
    p.x += aX;
    p.y += -0.66 + waveY(aX) - uSink * (1.0 - inClip) * 1.4;
    p.z += waveZ(aX) - uSink * (1.0 - inClip) * 2.4;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(p, 1.0);
  }
`;

const BAR_FRAG = /* glsl */ `
  uniform float uMarked;
  uniform float uShow;
  uniform float uSink;
  varying float vRead;
  varying float vIn;
  varying float vY;
  void main() {
    vec3 off = vec3(0.25, 0.25, 0.23);
    vec3 on = vec3(0.92, 0.91, 0.86);
    vec3 col = mix(off, on, vRead);
    col = mix(col, vec3(1.0, 0.9, 0.0), vIn * uMarked);
    float a = uShow * mix(1.0, vIn, uSink);
    gl_FragColor = vec4(col, a * (0.55 + 0.45 * vRead));
  }
`;

const PANE_FRAG = /* glsl */ `
  uniform sampler2D uMap;
  uniform float uAlpha;
  uniform float uFlash;
  uniform float uDim;
  uniform float uTime;
  uniform vec2 uSize;
  varying vec2 vUv;
  float rbox(vec2 p, vec2 b, float r) { vec2 q = abs(p) - b + r; return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r; }
  void main() {
    vec2 p = (vUv - 0.5) * uSize;
    float d = rbox(p, uSize * 0.5, 0.075);
    float mask = 1.0 - smoothstep(-0.006, 0.0, d);
    vec3 col = texture2D(uMap, vUv).rgb;
    // a slow sheen across the glass
    float sheen = smoothstep(0.08, 0.0, abs(vUv.x + vUv.y * 0.6 - fract(uTime * 0.08) * 3.0 + 0.6));
    col += sheen * 0.05;
    float rim = 1.0 - smoothstep(0.0, 0.012, abs(d + 0.006));
    col += rim * (0.10 + uFlash * 0.9) * vec3(1.0, 0.95, 0.6);
    col += uFlash * 0.25 * vec3(1.0, 0.95, 0.7);
    col *= 1.0 - uDim * 0.6;
    gl_FragColor = vec4(col, mask * uAlpha);
  }
`;

const PANE_VERT = /* glsl */ `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
`;

const DUST_VERT = /* glsl */ `
  attribute float aSeed;
  uniform float uTime;
  uniform float uDrift;
  uniform float uSize;
  varying float vA;
  void main() {
    vec3 p = position;
    p.y += sin(uTime * 0.2 + aSeed * 6.28) * 0.25;
    p.x += mod(uTime * 0.12 * (0.5 + aSeed) + uDrift * (0.6 + aSeed), 60.0) - 30.0;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = uSize * (0.4 + aSeed) * (14.0 / -mv.z);
    vA = 0.18 + 0.5 * aSeed;
  }
`;

const DUST_FRAG = /* glsl */ `
  uniform sampler2D uDot;
  uniform float uOpacity;
  varying float vA;
  void main() {
    float a = texture2D(uDot, gl_PointCoord).a;
    gl_FragColor = vec4(vec3(1.0, 0.96, 0.82), a * vA * uOpacity);
  }
`;

const GLOW_FRAG = /* glsl */ `
  uniform vec3 uColor;
  uniform float uAmt;
  varying vec2 vUv;
  void main() {
    vec2 p = vUv - 0.5;
    float g = exp(-dot(p, p) * 22.0);
    gl_FragColor = vec4(uColor * g * uAmt, 1.0);
  }
`;

/* ---- springs ------------------------------------------------------------------------------------------------ */
// Semi-implicit Euler on a fixed step. Under-damped (zeta ~0.6), so arrivals overshoot a touch and settle.
class Springs {
  constructor(n, k = 120, zeta = 0.62) {
    this.n = n;
    this.x = new Float32Array(n);
    this.v = new Float32Array(n);
    this.t = new Float32Array(n);
    this.k = new Float32Array(n).fill(k);
    this.c = new Float32Array(n).fill(2 * zeta * Math.sqrt(k));
  }
  step(dt) {
    const { x, v, t, k, c } = this;
    for (let i = 0; i < this.n; i++) {
      v[i] += (k[i] * (t[i] - x[i]) - c[i] * v[i]) * dt;
      x[i] += v[i] * dt;
    }
  }
  snap() {
    this.x.set(this.t);
    this.v.fill(0);
  }
}

/* ---- the layouts: desktop words sit left, so the picture leans right; phones put it below the words -------- */
function layoutFor(width, height) {
  const mobile = width < 900;
  const portrait = height > width;
  const n = mobile ? 40 : 64;
  const segLen = mobile ? 5 : 6;
  // where the three clips sit in the 72:40 episode (routes.DEMO_CLIPS): 14:02, 31:18, 58:40
  const segStart = [0.193, 0.431, 0.807].map((f) => Math.round(f * n) - (f > 0.5 ? segLen : 0));
  const paneH = mobile ? 2.2 : 2.9;
  const paneGap = mobile ? 1.38 : 2.05;
  const paneX = mobile ? 0 : 3.9;
  const paneY = mobile ? -1.15 : 0.05;
  return { mobile, portrait, n, segLen, segStart, paneH, paneGap, paneX, paneY };
}

/* ---- the scene ---------------------------------------------------------------------------------------------- */
export class Stage {
  constructor(canvas, { tiles, clipSources = null, onEvent = () => {}, refreshMs = 0 } = {}) {
    this.canvas = canvas;
    this.onEvent = onEvent;
    this.tiles = tiles;
    this.clipSources = clipSources;
    this.S = 0;
    this.J = 0;
    this.mouse = new THREE.Vector2(0, 0);
    this.mouseTarget = new THREE.Vector2(0, 0);
    this.mouseAmt = 0;
    this.velocity = 0;
    this.time = 0;
    this.introAt = 0;
    this.fired = new Set();
    this.quality = { dpr: 1, level: 2 };
    this.frameMs = 16.7;
    this.workMs = 0; // time spent in frame() itself, scripts and draw submission
    // the display refresh interval, clamped to 30..240 Hz; 16.7 ms (60 Hz) when it could not be measured
    this.vsyncMs = refreshMs > 4 && refreshMs < 34 ? refreshMs : 16.7;
    this.slowFor = 0;
    this.fastFor = 0;
    this.running = false;
    this.visible = true;
    this.frameNo = 0;
  }

  init() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.L = layoutFor(w, h);
    const mobile = this.L.mobile;
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas,
      antialias: !mobile,
      alpha: false,
      powerPreference: "high-performance",
      stencil: false,
    });
    this.renderer.setClearColor(0x0b0b0a, 1);
    this.maxDpr = Math.min(window.devicePixelRatio || 1, mobile ? 1.5 : 1.5);
    this.quality.dpr = Math.min(this.maxDpr, mobile ? 1.25 : 1.25);
    this.renderer.setPixelRatio(this.quality.dpr);
    this.renderer.setSize(w, h, false);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(36, w / h, 0.1, 140);
    this.camPos = new Springs(3, 38, 0.95);
    this.camLook = new Springs(3, 38, 0.95);
    this.shake = 0;

    this._buildGlow();
    this._buildRibbon();
    this._buildBars();
    this._buildScanner();
    this._buildCuts();
    this._buildPanes();
    this._buildDust();
    this._target(0, true);
    this.camPos.snap();
    this.camLook.snap();
    return this;
  }

  _buildGlow() {
    // a warm light far behind the action; it drifts to wherever the story is
    const mat = new THREE.ShaderMaterial({
      vertexShader: PANE_VERT,
      fragmentShader: GLOW_FRAG,
      uniforms: { uColor: { value: new THREE.Vector3(0.075, 0.068, 0.05) }, uAmt: { value: 0.6 } },
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      transparent: true,
    });
    this.glow = new THREE.Mesh(new THREE.PlaneGeometry(70, 40), mat);
    this.glow.position.set(0, 2, -22);
    this.glow.renderOrder = -2;
    this.scene.add(this.glow);
  }

  _buildRibbon() {
    const { n } = this.L;
    const atlas = sourceAtlas({ cellW: this.L.mobile ? 256 : 384 });
    const tex = new THREE.CanvasTexture(atlas.canvas);
    // shaders here work in display (sRGB) values end to end: no decode on read, no encode on write
    tex.anisotropy = 4;
    tex.generateMipmaps = true;
    tex.minFilter = THREE.LinearMipmapLinearFilter;
    this.shardW = 0.5625;
    this.ribbonLen = n * this.shardW;
    const geo = new THREE.PlaneGeometry(this.shardW, 1, 4, this.L.mobile ? 4 : 8);
    const index = new Float32Array(n);
    const segAttr = new Float32Array(n).fill(-1);
    this.segOf = new Int8Array(n).fill(-1);
    this.segK = new Int8Array(n).fill(-1);
    for (let i = 0; i < n; i++) index[i] = i;
    this.L.segStart.forEach((s0, j) => {
      for (let k = 0; k < this.L.segLen; k++) {
        segAttr[s0 + k] = j;
        this.segOf[s0 + k] = j;
        this.segK[s0 + k] = k;
      }
    });
    geo.setAttribute("aIndex", new THREE.InstancedBufferAttribute(index, 1));
    geo.setAttribute("aSeg", new THREE.InstancedBufferAttribute(segAttr, 1));
    this.state = new Float32Array(n * 4);
    this.stateAttr = new THREE.InstancedBufferAttribute(this.state, 4);
    this.stateAttr.setUsage(THREE.DynamicDrawUsage);
    geo.setAttribute("aState", this.stateAttr);

    this.shardUniforms = {
      uAtlas: { value: tex },
      uGrid: { value: new THREE.Vector2(atlas.cols, atlas.rows) },
      uFrames: { value: atlas.count },
      uShardW: { value: this.shardW },
      uFrameW: { value: 16 / 9 },
      uScanX: { value: -999 },
      uScanOn: { value: 0 },
      uSlice: { value: 0.12 },
      uTime: { value: 0 },
      uMouse: { value: new THREE.Vector3(999, 999, 0) },
      uMouseAmt: { value: 0 },
      uFogNear: { value: 14 },
      uFogFar: { value: 46 },
      uMarked: { value: 0 },
    };
    const mat = new THREE.ShaderMaterial({
      vertexShader: SHARD_VERT,
      fragmentShader: SHARD_FRAG,
      uniforms: this.shardUniforms,
      transparent: true,
      side: THREE.DoubleSide,
      depthWrite: true,
    });
    this.ribbon = new THREE.InstancedMesh(geo, mat, n);
    this.ribbon.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.ribbon.frustumCulled = false;
    this.scene.add(this.ribbon);

    // seven channels per shard: x y z, rotation x y z, scale; plus alpha and flash on their own springs
    this.sp = {
      px: new Springs(n, 90, 0.66), py: new Springs(n, 90, 0.66), pz: new Springs(n, 90, 0.66),
      rx: new Springs(n, 80, 0.6), ry: new Springs(n, 80, 0.6), rz: new Springs(n, 80, 0.6),
      s: new Springs(n, 160, 0.5), a: new Springs(n, 60, 1),
    };
    this.flash = new Float32Array(n);
    this.read = new Float32Array(n);
    this._m = new THREE.Matrix4();
    this._q = new THREE.Quaternion();
    this._e = new THREE.Euler();
    this._v = new THREE.Vector3();
    this._s = new THREE.Vector3();
  }

  _buildBars() {
    const count = Math.round(this.ribbonLen / (this.L.mobile ? 0.11 : 0.085));
    const geo = new THREE.InstancedBufferGeometry();
    const base = new THREE.PlaneGeometry(this.L.mobile ? 0.05 : 0.04, 1);
    geo.index = base.index;
    geo.setAttribute("position", base.getAttribute("position"));
    const ax = new Float32Array(count);
    const ah = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      const x = i / count;
      // the same loudness strip as routes._waveform
      let level = 0.34 + 0.22 * Math.sin(x * 37) * Math.sin(x * 5.3 + 1.1) + 0.18 * Math.sin(x * 211 + 0.7);
      level += 0.12 * Math.sin(x * 97 + 2.3);
      level = clamp(Math.abs(level), 0.08, 0.96);
      ax[i] = (i + 0.5) * (this.ribbonLen / count) - this.ribbonLen / 2;
      ah[i] = level * 0.34;
    }
    geo.setAttribute("aX", new THREE.InstancedBufferAttribute(ax, 1));
    geo.setAttribute("aH", new THREE.InstancedBufferAttribute(ah, 1));
    geo.instanceCount = count;
    const segs = this.L.segStart.map((s0) => {
      const x0 = s0 * this.shardW - this.ribbonLen / 2;
      return new THREE.Vector4(x0, x0 + this.L.segLen * this.shardW, 0, 0);
    });
    this.barUniforms = {
      uTime: { value: 0 }, uScanX: { value: -999 }, uRead: { value: 0 }, uWave: { value: 1 },
      uSegs: { value: segs }, uSink: { value: 0 }, uShow: { value: 1 }, uMarked: { value: 0 },
    };
    const mat = new THREE.ShaderMaterial({
      vertexShader: BAR_VERT, fragmentShader: BAR_FRAG, uniforms: this.barUniforms,
      transparent: true, depthWrite: false,
    });
    this.bars = new THREE.Mesh(geo, mat);
    this.bars.frustumCulled = false;
    this.scene.add(this.bars);
  }

  _buildScanner() {
    const dot = new THREE.CanvasTexture(softDot(128));
    const make = (w, h, color, opacity) => new THREE.Mesh(
      new THREE.PlaneGeometry(w, h),
      new THREE.MeshBasicMaterial({ map: dot, color, transparent: true, opacity, depthWrite: false, blending: THREE.AdditiveBlending }),
    );
    this.scanner = new THREE.Group();
    this.scanCore = make(0.09, 2.3, 0xffe500, 1);
    this.scanHalo = make(2.6, 3.6, 0xffd200, 0.5);
    // the halo is light, not an object: it shows through the frames in front of it
    this.scanHalo.material.depthTest = false;
    this.scanHalo.renderOrder = 5;
    this.scanLine = new THREE.Mesh(
      new THREE.PlaneGeometry(0.012, 1.5),
      new THREE.MeshBasicMaterial({ color: 0xfff3a0, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }),
    );
    this.scanner.add(this.scanHalo, this.scanCore, this.scanLine);
    this.scene.add(this.scanner);
  }

  _buildCuts() {
    this.cuts = [];
    const geo = new THREE.PlaneGeometry(0.03, 1.7);
    this.L.segStart.forEach((s0, j) => {
      [s0, s0 + this.L.segLen].forEach((edge, e) => {
        const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
          color: 0xffe500, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending,
        }));
        m.userData = { j, e, x: edge * this.shardW - this.ribbonLen / 2 };
        this.cuts.push(m);
        this.scene.add(m);
      });
    });
  }

  _buildPanes() {
    const { paneH } = this.L;
    const paneW = (paneH * 9) / 16;
    const width = this.L.mobile ? 252 : 360;
    this.shorts = this.tiles.map((tile) => new ShortCanvas(tile, width));
    this.videos = this.clipSources ? this.clipSources.map(demoVideo) : null;
    this.panes = this.shorts.map((short, j) => {
      const map = this.videos ? new THREE.VideoTexture(this.videos[j]) : new THREE.CanvasTexture(short.canvas);
      // display values in, display values out (see the atlas)
      map.minFilter = THREE.LinearFilter;
      map.generateMipmaps = false;
      const uniforms = {
        uMap: { value: map }, uAlpha: { value: 0 }, uFlash: { value: 0 }, uDim: { value: 0 },
        uTime: { value: 0 }, uSize: { value: new THREE.Vector2(paneW, paneH) },
      };
      const mesh = new THREE.Mesh(
        new THREE.PlaneGeometry(paneW, paneH),
        new THREE.ShaderMaterial({ vertexShader: PANE_VERT, fragmentShader: PANE_FRAG, uniforms, transparent: true }),
      );
      // a soft shadow behind each pane, so it sits in the room rather than on the glass
      const shadow = new THREE.Mesh(
        new THREE.PlaneGeometry(paneW * 1.9, paneH * 1.45),
        new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(softDot(64)), color: 0x000000, transparent: true, opacity: 0, depthWrite: false }),
      );
      shadow.position.z = -0.25;
      mesh.add(shadow);
      mesh.renderOrder = 2;
      this.scene.add(mesh);
      const entry = {
        mesh, shadow, uniforms, map, short,
        sp: {
          px: new Springs(1, 70, 0.7), py: new Springs(1, 70, 0.7), pz: new Springs(1, 70, 0.7),
          rx: new Springs(1, 60, 0.62), ry: new Springs(1, 60, 0.62), rz: new Springs(1, 60, 0.62),
          s: new Springs(1, 200, 0.42), a: new Springs(1, 90, 1),
        },
        flash: 0,
        lastWord: -1,
        wordAt: -1,
      };
      entry.springs = Object.values(entry.sp);
      return entry;
    });
    this.paneW = paneW;
  }

  _buildDust() {
    const count = this.L.mobile ? 140 : 420;
    const pos = new Float32Array(count * 3);
    const seed = new Float32Array(count);
    let s = 11;
    const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647);
    for (let i = 0; i < count; i++) {
      pos[i * 3] = (rnd() - 0.5) * 60;
      pos[i * 3 + 1] = (rnd() - 0.5) * 14;
      pos[i * 3 + 2] = (rnd() - 0.5) * 30 - 4;
      seed[i] = rnd();
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
    this.dustUniforms = {
      uTime: { value: 0 }, uDrift: { value: 0 }, uSize: { value: this.L.mobile ? 5 : 6 },
      uDot: { value: new THREE.CanvasTexture(softDot(32)) }, uOpacity: { value: 1 },
    };
    this.dust = new THREE.Points(geo, new THREE.ShaderMaterial({
      vertexShader: DUST_VERT, fragmentShader: DUST_FRAG, uniforms: this.dustUniforms,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    this.dust.frustumCulled = false;
    this.scene.add(this.dust);
  }

  /* ---- choreography --------------------------------------------------------------------------------------- */
  baseX(i) {
    return (i + 0.5) * this.shardW - this.ribbonLen / 2;
  }

  /** How much the strip undulates: a flowing ribbon in the hero, nearly flat once it is being read. */
  wave(S) {
    return lerp(1.55, 0.18, easeInOut(seg(S, 0.25, 1.0))) * (1 - seg(S, 1.6, 2.0) * 0.9);
  }

  scanX(S) {
    const half = this.ribbonLen / 2;
    if (S < 1) return -half - 0.4; // waiting at the start of the episode
    return lerp(-half - 0.4, half + 0.4, easeInOut(seg(S, 1.0, 1.55)));
  }

  paneSlot(j, S) {
    const L = this.L;
    const x = L.paneX + (j - 1) * L.paneGap;
    let p = [x, L.paneY, 0];
    let r = [0, 0, 0];
    let s = 1;
    // shorts: fan out like a hand of cards, then straighten into a row
    const fan = easeInOut(seg(S, 4.62, 5.08)) * (1 - easeInOut(seg(S, 5.22, 5.48)));
    const row = easeInOut(seg(S, 5.22, 5.48)) * (1 - easeInOut(seg(S, 5.62, 6.05)));
    const side = j - 1;
    p = [p[0] - side * 0.28 * fan, p[1] - Math.abs(side) * 0.28 * fan + 0.05 * fan, p[2] + (side === 0 ? 0.35 : 0) * fan];
    r = [0, -side * 0.12 * fan, -side * 0.16 * fan];
    p[0] += side * 0.12 * row;
    r[1] += -side * 0.2 * row;
    // package: a deck in the middle, the first Short on top
    const deck = easeInOut(seg(S, 5.62, 6.05));
    if (deck > 0) {
      const dx = L.mobile ? 0 : L.paneX - 0.15;
      const dy = L.mobile ? L.paneY - 0.05 : 0.0;
      const dp = [dx + j * 0.32, dy + j * 0.12, -j * 0.55];
      p = mix3(p, dp, deck);
      r = mix3(r, [0, -0.32, -0.05 + j * 0.06], deck);
      s = lerp(s, L.mobile ? 0.82 : 0.86, deck);
    }
    return { p, r, s };
  }

  /** Targets for every spring at story position S (and J, the closing section). */
  _target(S, initial = false) {
    const { n, segLen, segStart, paneH, mobile } = this.L;
    const t = this.time;
    const sp = this.sp;
    const intro = initial ? 0 : clamp((t - this.introAt) / 2.6);
    const waveAmt = this.wave(S);
    const scanX = this.scanX(S);
    const J = this.J;

    for (let i = 0; i < n; i++) {
      const bx = this.baseX(i);
      const j = this.segOf[i];
      const k = this.segK[i];
      let x = bx;
      let y = waveAmt * 0.32 * Math.sin(bx * 0.33 + t * 0.4);
      let z = waveAmt * 0.9 * Math.sin(bx * 0.17 + t * 0.27 + 1.3);
      const dz = waveAmt * 0.9 * 0.17 * Math.cos(bx * 0.17 + t * 0.27 + 1.3);
      const dy = waveAmt * 0.32 * 0.33 * Math.cos(bx * 0.33 + t * 0.4);
      let rx = 0;
      let ry = -Math.atan(dz);
      let rz = Math.atan(dy);
      // the intro: shards arrive one after another from the start of the episode
      const born = clamp(intro * 1.25 * n - i);
      let s = initial ? 0.001 : lerp(0.001, 1, born);
      let a = 1;
      let read = S < 1 ? 0 : clamp((scanX - bx) / 0.7 + 0.5);
      let dim = 0;

      // cut: the three stretches lift free, one snap each; the rest sinks away
      const sink = easeInOut(seg(S, 2.34, 2.56));
      if (j >= 0) {
        const lifted = S >= 2.06 + j * 0.12 ? 1 : 0;
        y += lifted * 0.62;
        z += lifted * 2.1;
        // frame: each shard flips through the air and stacks into its pane
        const f0 = 2.62 + j * 0.1 + k * 0.035;
        const fp = easeInOut(seg(S, f0, f0 + 0.3));
        if (fp > 0) {
          const slot = this.paneSlot(j, S);
          const target = [slot.p[0], slot.p[1], slot.p[2] + 0.006 * k];
          const arc = Math.sin(fp * Math.PI);
          x = lerp(x, target[0], fp);
          y = lerp(y, target[1], fp) + arc * (0.7 + 0.15 * k);
          z = lerp(z, target[2], fp) + arc * (0.8 + (k % 2) * 0.35);
          rx = lerp(rx, slot.r[0], fp) + arc * 0.35;
          ry = lerp(ry, slot.r[1], fp) + smooth(fp) * Math.PI * 2;
          rz = lerp(rz, slot.r[2], fp) + arc * (k % 2 ? 0.5 : -0.5);
          s = lerp(s, (paneH * slot.s), fp);
        }
        // once the pane has locked, its shards are inside it
        if (S >= this.lockS(j) + 0.015) a = 0;
      } else {
        y -= sink * (mobile ? 2.2 : 1.6);
        z -= sink * 3.2;
        dim = sink * 0.75;
        a = 1 - easeInOut(seg(S, 2.6, 2.95));
      }

      // closing section: the episode lies back down, read and whole, under the form
      if (J > 0) {
        const back = easeOut(J);
        x = lerp(x, bx, back);
        y = lerp(y, 0.06 * Math.sin(bx * 0.33 + t * 0.4), back);
        z = lerp(z, 0.25 * Math.sin(bx * 0.17 + t * 0.27 + 1.3), back);
        rx = lerp(rx, 0, back);
        ry = lerp(ry, 0, back);
        rz = lerp(rz, 0, back);
        s = lerp(s, 1, back);
        a = lerp(a, 1, back);
        dim = lerp(dim, 0, back);
        read = lerp(read, 1, back);
      }

      sp.px.t[i] = x; sp.py.t[i] = y; sp.pz.t[i] = z;
      sp.rx.t[i] = rx; sp.ry.t[i] = ry; sp.rz.t[i] = rz;
      sp.s.t[i] = s; sp.a.t[i] = a;
      this.read[i] = read;
      this.state[i * 4 + 2] = dim;
    }

    // panes
    this.panes.forEach((pane, j) => {
      const slot = this.paneSlot(j, S);
      const on = S >= this.lockS(j) && S < 7.2 && J < 0.5 ? 1 : 0;
      const p = pane.sp;
      p.px.t[0] = slot.p[0]; p.py.t[0] = slot.p[1]; p.pz.t[0] = slot.p[2];
      // cursor tilt, mostly while the three are on show
      const tilt = (1 - seg(S, 5.6, 6.0)) * seg(S, 4.5, 4.9);
      p.rx.t[0] = slot.r[0] - this.mouse.y * 0.18 * tilt;
      p.ry.t[0] = slot.r[1] + this.mouse.x * 0.22 * tilt;
      p.rz.t[0] = slot.r[2];
      p.s.t[0] = on ? slot.s : slot.s * 0.94;
      p.a.t[0] = on;
      // in the caption beat the other two step back
      pane.uniforms.uDim.value = j === 0 ? 0 : 0.85 * seg(S, 3.55, 3.85) * (1 - seg(S, 4.5, 4.85));
    });
  }

  lockS(j) {
    return 2.62 + j * 0.1 + (this.L.segLen - 1) * 0.035 + 0.3;
  }

  /** Camera position and target for S, before springs. */
  _camera(S) {
    const { mobile, portrait, paneX, paneY, paneGap } = this.L;
    const half = this.ribbonLen / 2;
    const aspect = this.camera.aspect;
    const far = mobile ? 1.55 : 1; // phones are narrow: stand further back
    // hero: down the length of the episode, from beside its last frames, to the light waiting at its start
    const heroPos = mobile ? [half + 3.75, 1.0, 7.5] : [half + 2.6, 0.5, 1.6];
    const heroLook = mobile ? [half - 11.25, 4.2, -1] : [half - 28, 1.6, 5.6];
    // listen: alongside, following the light
    const sx = this.scanX(S);
    const listenPos = [sx + (mobile ? 0 : 1.6), mobile ? 0.2 : 0.55, mobile ? 7.6 : 5.0];
    const listenLook = [sx + (mobile ? 0 : 0.9), mobile ? 1.7 : 1.05, 0];
    // cut: the whole episode at once
    const cutPos = [0, mobile ? 0.8 : 1.6, mobile ? 30 : 23];
    const cutLook = [0, mobile ? 3.6 : 2.3, 0];
    // frame: the three panes, right of the words
    const framePos = mobile ? [paneX, paneY + 0.8, 11.5] : [paneX - 2.6, 0.55, 12.5];
    const frameLook = mobile ? [paneX, paneY + 2.15, 0] : [paneX - 2.75, 0.3, 0];
    // caption: close on the first Short
    const p0 = paneX - paneGap;
    const capPos = mobile ? [p0, paneY + 0.3, 5.6] : [p0 - 1.0, 0.2, 6.0];
    const capLook = mobile ? [p0, paneY + 1.05, 0] : [p0 - 1.24, 0.05, 0];
    // shorts: the three, centred
    const shortsPos = mobile ? [paneX, paneY + 0.5, 12] : [paneX - 1.5, 0.3, 11.5];
    const shortsLook = mobile ? [paneX, paneY + 2.25, 0] : [paneX - 1.67, 0.35, 0];
    // package: the deck in the middle column
    const packPos = mobile ? [paneX, paneY + 0.6, 10.6] : [paneX + 0.1, 0.3, 10.5];
    const packLook = mobile ? [paneX + 0.2, paneY + 2.4, 0] : [paneX + 0.15, 0.2, 0];
    // closing: the episode as a horizon under the form
    const endPos = [0, mobile ? 1.2 : 0.4, mobile ? 30 : 15];
    const endLook = [0, mobile ? 5.6 : 3.4, 0];

    const blend = (a, b, t) => mix3(a, b, easeInOut(t));
    let pos;
    let look;
    if (S < 1) {
      const t = seg(S, 0.12, 1);
      pos = blend(heroPos, listenPos, t);
      look = blend(heroLook, listenLook, t);
    } else if (S < 2) {
      const t = seg(S, 1.58, 2);
      pos = blend(listenPos, cutPos, t);
      look = blend(listenLook, cutLook, t);
    } else if (S < 3) {
      const t = seg(S, 2.55, 3.05);
      pos = blend(cutPos, framePos, t);
      look = blend(cutLook, frameLook, t);
    } else if (S < 4) {
      const t = seg(S, 3.5, 4.0);
      pos = blend(framePos, capPos, t);
      look = blend(frameLook, capLook, t);
    } else if (S < 5) {
      const t = seg(S, 4.5, 5.0);
      pos = blend(capPos, shortsPos, t);
      look = blend(capLook, shortsLook, t);
    } else if (S < 6) {
      const t = seg(S, 5.55, 6.0);
      pos = blend(shortsPos, packPos, t);
      look = blend(shortsLook, packLook, t);
    } else {
      pos = packPos;
      look = packLook;
    }
    if (this.J > 0) {
      const t = easeInOut(this.J);
      pos = mix3(pos, endPos, t);
      look = mix3(look, endLook, t);
    }
    if (this.debugCam) ({ pos, look } = this.debugCam);
    // a little parallax from the pointer
    const par = mobile ? 0 : 0.35;
    pos = [pos[0] + this.mouse.x * par, pos[1] + this.mouse.y * par * 0.6, pos[2]];
    void aspect; void portrait; void far;
    return { pos, look };
  }

  /* ---- events ------------------------------------------------------------------------------------------------ */
  _events(S, prevS) {
    const fire = (key, at, fn) => {
      if (prevS < at && S >= at && !this.fired.has(key)) {
        this.fired.add(key);
        fn();
      } else if (S < at - 0.02 && this.fired.has(key)) {
        this.fired.delete(key); // scrolled back above it: it may fire again
      }
    };
    // a big jump (a link, a refresh mid-page) skips the theatre
    if (Math.abs(S - prevS) > 0.35) return;
    for (let j = 0; j < 3; j++) {
      fire(`cut${j}`, 2.06 + j * 0.12, () => {
        const s0 = this.L.segStart[j];
        for (let k = 0; k < this.L.segLen; k++) {
          this.flash[s0 + k] = 1;
          this.sp.pz.v[s0 + k] += 6;
          this.sp.rz.v[s0 + k] += (k - this.L.segLen / 2) * 0.6;
        }
        this.cutFlash[j] = 1;
        this.shake = Math.min(1, this.shake + 0.55);
        this.onEvent("snap", j);
      });
      fire(`lock${j}`, this.lockS(j), () => {
        const pane = this.panes[j];
        pane.flash = 1;
        pane.sp.s.x[0] = 0.86;
        pane.sp.s.v[0] = 3.5;
        this.shake = Math.min(1, this.shake + 0.3);
        this.onEvent("lock", j);
      });
    }
  }

  /* ---- per frame ------------------------------------------------------------------------------------------- */
  setStory(S, J) {
    this.prevS = this.S;
    this.S = S;
    this.J = J;
  }

  setPointer(nx, ny, active) {
    this.mouseTarget.set(nx, ny);
    this.pointerActive = active;
  }

  resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const next = layoutFor(w, h);
    if (next.mobile !== this.L.mobile) {
      this.onEvent("relayout");
      return;
    }
    this.camera.aspect = w / h;
    this.camera.fov = h > w ? 46 : 36;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  }

  start() {
    this.cutFlash = [0, 0, 0];
    this.introAt = this.time;
    this.running = true;
    this.resize();
  }

  /** Advance one frame. dt in seconds. Returns false when nothing needs drawing (fully covered). */
  frame(dt, now) {
    if (!this.running) return false;
    const t0 = performance.now();
    dt = Math.min(dt, 1 / 20);
    this.time += dt;
    this.frameNo++;
    const S = this.S;
    const prevS = this.prevS ?? S;
    this.prevS = S;
    this._events(S, prevS);

    // pointer, eased
    this.mouse.lerp(this.mouseTarget, 1 - Math.exp(-dt * 4));
    this.mouseAmt = lerp(this.mouseAmt, this.pointerActive && this.quality.level > 0 ? 1 : 0, 1 - Math.exp(-dt * 3));

    this._target(S);
    const sub = 2;
    const h = dt / sub;
    const sp = this.sp;
    for (let k = 0; k < sub; k++) {
      sp.px.step(h); sp.py.step(h); sp.pz.step(h);
      sp.rx.step(h); sp.ry.step(h); sp.rz.step(h);
      sp.s.step(h); sp.a.step(h);
      for (const pane of this.panes) for (const spring of pane.springs) spring.step(h);
    }

    // shards
    const n = this.L.n;
    const m = this._m;
    for (let i = 0; i < n; i++) {
      this._e.set(sp.rx.x[i], sp.ry.x[i], sp.rz.x[i]);
      this._q.setFromEuler(this._e);
      const s = Math.max(0.0001, sp.s.x[i]);
      this._s.set(s, s, s);
      this._v.set(sp.px.x[i], sp.py.x[i], sp.pz.x[i]);
      m.compose(this._v, this._q, this._s);
      this.ribbon.setMatrixAt(i, m);
      this.flash[i] *= Math.exp(-dt * 5);
      this.state[i * 4] = this.read[i];
      this.state[i * 4 + 1] = this.flash[i];
      this.state[i * 4 + 3] = clamp(sp.a.x[i]);
    }
    this.ribbon.instanceMatrix.needsUpdate = true;
    this.stateAttr.needsUpdate = true;

    // the light
    const scanX = this.scanX(S);
    const scanOn = clamp(seg(this.time - this.introAt, 0.4, 1.6)) * (1 - seg(S, 1.55, 1.7)) * (1 - this.J);
    const u = this.shardUniforms;
    u.uTime.value = this.time;
    u.uScanX.value = scanX;
    u.uScanOn.value = scanOn;
    u.uSlice.value = lerp(0.12, 1, seg(S, 1.9, 2.1)) * (1 - this.J * 0.85);
    u.uMarked.value = seg(S, 2.0, 2.1) * (1 - seg(S, 2.65, 2.9)) * (1 - this.J);
    u.uMouseAmt.value = this.mouseAmt * (1 - seg(S, 2.6, 3));
    this.scanner.position.set(scanX, 0.32 * this.wave(S) * Math.sin(scanX * 0.33 + this.time * 0.4), 0.9 * this.wave(S) * Math.sin(scanX * 0.17 + this.time * 0.27 + 1.3) + 0.35);
    this.scanner.visible = scanOn > 0.001;
    this.scanCore.material.opacity = scanOn;
    this.scanHalo.material.opacity = scanOn * 0.45;
    this.scanLine.material.opacity = scanOn;
    this.scanHalo.scale.setScalar((1 + Math.sin(this.time * 2.2) * 0.04) * (1 + 1.6 * (1 - seg(S, 0.2, 0.9))));

    // bars under the strip
    const b = this.barUniforms;
    b.uTime.value = this.time;
    b.uScanX.value = S < 1 ? -999 : scanX;
    b.uRead.value = Math.max(seg(S, 1.55, 1.6), this.J);
    b.uWave.value = this.wave(S);
    b.uSink.value = easeInOut(seg(S, 2.34, 2.56)) * (1 - this.J);
    b.uMarked.value = seg(S, 2.0, 2.1);
    b.uShow.value = clamp((this.time - this.introAt) / 1.8) * (1 - seg(S, 2.6, 2.85)) + this.J * 0.6;
    this.bars.visible = b.uShow.value > 0.001;

    // cut lines
    this.cuts.forEach((c) => {
      const { j, x } = c.userData;
      this.cutFlash[j] *= Math.exp(-dt * 3.2);
      const shown = S >= 2.06 + j * 0.12 ? 1 : 0;
      c.position.set(x, 0.62 * shown, 2.1 * shown + 0.02);
      c.material.opacity = (0.35 * shown + this.cutFlash[j] * 1.6) * (1 - seg(S, 2.5, 2.7));
      c.scale.y = 1 + this.cutFlash[j] * 0.8;
      c.visible = c.material.opacity > 0.002;
    });

    // panes
    this.panes.forEach((pane, j) => {
      const p = pane.sp;
      pane.mesh.position.set(p.px.x[0], p.py.x[0], p.pz.x[0]);
      pane.mesh.rotation.set(p.rx.x[0], p.ry.x[0], p.rz.x[0]);
      pane.mesh.scale.setScalar(Math.max(0.001, p.s.x[0]));
      pane.flash *= Math.exp(-dt * 4);
      const alpha = clamp(p.a.x[0]);
      pane.uniforms.uAlpha.value = alpha;
      pane.uniforms.uFlash.value = pane.flash;
      pane.uniforms.uTime.value = this.time + j;
      pane.shadow.material.opacity = alpha * 0.7;
      pane.mesh.visible = alpha > 0.002;
      if (pane.mesh.visible) this._playShort(pane, j, dt);
    });

    // camera
    const cam = this._camera(S);
    for (let k = 0; k < 3; k++) {
      this.camPos.t[k] = cam.pos[k];
      this.camLook.t[k] = cam.look[k];
    }
    this.camPos.step(dt);
    this.camLook.step(dt);
    this.shake *= Math.exp(-dt * 7);
    const sh = this.shake * 0.06;
    this.camera.position.set(
      this.camPos.x[0] + Math.sin(this.time * 61) * sh,
      this.camPos.x[1] + Math.sin(this.time * 47 + 1) * sh,
      this.camPos.x[2],
    );
    this.camera.lookAt(this.camLook.x[0], this.camLook.x[1], this.camLook.x[2]);
    this.camera.updateMatrixWorld();
    // the pointer, projected onto the strip's plane (z = 0), for the ripple
    const ray = this._v.set(this.mouse.x, this.mouse.y, 0.5).unproject(this.camera).sub(this.camera.position);
    const tz = -this.camera.position.z / (ray.z || -1e-6);
    u.uMouse.value.set(this.camera.position.x + ray.x * tz, this.camera.position.y + ray.y * tz, 0);
    this.glow.position.x = lerp(this.glow.position.x, this.camLook.x[0], 1 - Math.exp(-dt * 0.8));
    this.glow.material.uniforms.uAmt.value = 0.55 + 0.25 * seg(S, 4.5, 5.2) + scanOn * 0.15;

    // dust drifts with scrolling, so speed reads as depth
    this.dustUniforms.uTime.value = this.time;
    this.dustUniforms.uDrift.value += this.velocity * dt * 0.02;
    this.dust.visible = this.quality.level > 0;

    this.renderer.render(this.scene, this.camera);
    this.workMs = lerp(this.workMs, performance.now() - t0, 0.05);
    this._adapt(dt);
    void now;
    return true;
  }

  _playShort(pane, j, dt) {
    // The first Short is scrubbed by the scroll in the caption beat; otherwise each plays on its own clock.
    const short = pane.short;
    const S = this.S;
    const scrub = seg(S, 3.75, 4.45);
    const t = S >= 3.6 && S < 4.6 && j === 0 ? scrub * (short.duration - 0.6) : this.time * 0.9 + j * 1.3;
    if (this.videos) return; // a VideoTexture updates itself
    // budget: the focused Short every frame, the others every third
    const tick = this.frameNo;
    const focused = j === 0 && S >= 3.5 && S < 4.7;
    if (!focused && (tick + j) % 3 !== 0) return;
    const word = short.wordAt(t);
    if (word !== pane.lastWord) {
      pane.lastWord = word;
      pane.wordAt = this.time;
      if (focused && Math.abs(scrub - 0.5) < 0.5) this.onEvent("word", j);
    }
    // The current word springs up to 112%, like the renderer's. A closed-form damped spring of the time since the
    // word changed: stable at any frame rate (an integrated one diverged on a 40 Hz screen and drew a word huge).
    const since = this.time - (pane.wordAt ?? -1);
    const pop = clamp(1 - Math.exp(-since * 16) * Math.cos(since * 24), 0, 1.25);
    short.draw(t, { pop });
    pane.map.needsUpdate = true;
  }

  /**
   * Hold the display's own frame rate: step the pixel ratio down while frames are being dropped, back up when
   * there is room. "Dropped" is judged against the display's refresh interval, measured by main.js while the
   * page was quiet, never a fixed 16.7 ms: a 40, 50 or 120 Hz screen must not read as slow or fast.
   */
  _adapt(dt) {
    const ms = dt * 1000;
    this.frameMs = lerp(this.frameMs, ms, 0.05);
    // the measured refresh (main.js); without one, a cautious 60 Hz
    const budget = this.vsyncMs;
    if (this.frameMs > budget * 1.2) {
      this.slowFor += dt;
      this.fastFor = 0;
    } else if (this.frameMs < budget * 1.06 && this.workMs < budget * 0.45) {
      this.fastFor += dt;
      this.slowFor = 0;
    } else {
      this.slowFor = 0;
      this.fastFor = 0;
    }
    const q = this.quality;
    const floor = this.L.mobile ? 0.75 : 0.85;
    if (this.slowFor > 1) {
      this.slowFor = 0;
      if (q.dpr > floor + 0.01) {
        q.dpr = Math.max(floor, q.dpr - 0.2);
        this.renderer.setPixelRatio(q.dpr);
        this.resize();
      } else if (q.level > 0) {
        q.level -= 1; // first the dust and the cursor ripple go
      }
    } else if (this.fastFor > 4 && q.dpr < this.maxDpr - 0.01) {
      this.fastFor = 0;
      q.dpr = Math.min(this.maxDpr, q.dpr + 0.1);
      this.renderer.setPixelRatio(q.dpr);
      this.resize();
    }
  }

  playVideos(on) {
    if (!this.videos) return;
    this.videos.forEach((v) => (on ? v.play().catch(() => {}) : v.pause()));
  }

  dispose() {
    this.running = false;
    this.playVideos(false);
    this.scene.traverse((o) => {
      o.geometry?.dispose?.();
      const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
      mats.forEach((mat) => {
        Object.values(mat.uniforms || {}).forEach((u) => u.value?.dispose?.());
        mat.map?.dispose?.();
        mat.dispose();
      });
    });
    this.renderer.dispose();
    this.renderer.forceContextLoss?.();
  }
}
