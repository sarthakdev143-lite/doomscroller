// Draws the film: the two rendered frames either side of the playhead, blended, cover-fitted to the screen, and the
// pointer as a small warm light. The light is not painted on top: it reads the normal pass Blender rendered beside
// each frame, so it catches the bevels and the glass faces where a real light would, and leaves the dark air alone.

const VERT = `#version 300 es
in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FRAG = `#version 300 es
precision highp float;
uniform sampler2D uA;
uniform sampler2D uB;
uniform sampler2D uL0;
uniform sampler2D uL1;
uniform float uMix;
uniform float uLMix;
uniform vec2 uScale;
uniform float uAspect;     // frame width / height
uniform vec3 uPointer;     // x, y in frame uv (y up), intensity
uniform float uLight;      // 1 when the light pass is bound
uniform float uLinear;     // light pass stored linear (1) or sRGB (0)
uniform vec3 uWarm;
in vec2 vUv;
out vec4 outColor;

vec3 pass(sampler2D t, vec2 uv) {
  return texture(t, vec2(uv.x, 1.0 - uv.y)).rgb;
}

void main() {
  vec2 uv = (vUv - 0.5) * uScale + 0.5;
  vec3 c = mix(pass(uA, uv), pass(uB, uv), uMix);
  if (uPointer.z > 0.001) {
    vec2 d = (uPointer.xy - uv) * vec2(uAspect, 1.0);
    float r2 = dot(d, d);
    // the air: a faint warm bloom where the light is
    c += uWarm * 0.022 * exp(-r2 * 26.0) * uPointer.z;
    if (uLight > 0.5) {
      vec3 n0 = mix(pass(uL0, uv), pass(uL1, uv), uLMix);
      if (uLinear < 0.5) n0 = pow(n0, vec3(2.2));
      float gloss = n0.b;
      if (gloss > 0.02) {
        vec2 nxy = n0.rg * 2.0 - 1.0;
        vec3 n = normalize(vec3(nxy, sqrt(max(0.0, 1.0 - dot(nxy, nxy)))));
        vec3 L = normalize(vec3(d, 0.22));
        vec3 H = normalize(L + vec3(0.0, 0.0, 1.0));
        float spec = pow(max(dot(n, H), 0.0), 110.0);
        float sheen = pow(max(dot(n, H), 0.0), 9.0) * 0.06;
        float fall = exp(-r2 * 7.0);
        c += uWarm * (spec * 0.85 + sheen) * gloss * fall * uPointer.z;
      }
    }
  }
  outColor = vec4(c, 1.0);
}`;

export class Film {
  constructor(canvas, size, { preserve = false } = {}) {
    this.canvas = canvas;
    this.size = size; // {w, h} of the frames
    const gl = canvas.getContext("webgl2", {
      antialias: false,
      alpha: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: preserve,
      powerPreference: "high-performance",
    });
    if (!gl) throw new Error("no webgl2");
    this.gl = gl;
    const prog = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, VERT], [gl.FRAGMENT_SHADER, FRAG]]) {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
      gl.attachShader(prog, sh);
    }
    gl.bindAttribLocation(prog, 0, "aPos");
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    this.u = {};
    for (const name of ["uA", "uB", "uL0", "uL1", "uMix", "uLMix", "uScale", "uAspect", "uPointer", "uLight",
      "uLinear", "uWarm"]) {
      this.u[name] = gl.getUniformLocation(prog, name);
    }
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    this.tex = [0, 1, 2, 3].map((unit) => {
      const t = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([7, 7, 10, 255]));
      return { t, unit, key: null, w: 1, h: 1 };
    });
    gl.uniform1i(this.u.uA, 0);
    gl.uniform1i(this.u.uB, 1);
    gl.uniform1i(this.u.uL0, 2);
    gl.uniform1i(this.u.uL1, 3);
    gl.uniform3f(this.u.uWarm, 1.0, 0.9, 0.78);
    this.state = { mix: 0, lmix: 0, light: 0, linear: 1, pointer: [0.5, 0.5, 0] };
    this.lost = false;
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this.lost = true;
    });
    this.resize();
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cw = this.canvas.clientWidth || window.innerWidth;
    const ch = this.canvas.clientHeight || window.innerHeight;
    if (this.view && this.view.cw === cw && this.view.ch === ch && this.view.dpr === dpr) return;
    // never wider than the frames need: past ~1.3x their size, extra pixels only cost fill rate
    const cap = Math.max(this.size.w, this.size.h) * 1.35;
    const s = Math.min(dpr, cap / Math.max(cw, ch));
    this.canvas.width = Math.max(1, Math.round(cw * s));
    this.canvas.height = Math.max(1, Math.round(ch * s));
    this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    const fa = this.size.w / this.size.h;
    const va = cw / ch;
    this.scale = va > fa ? [1, fa / va] : [va / fa, 1];
    this.view = { cw, ch, dpr };
  }

  /** Client pixel -> frame uv (y up), through the same cover fit as the shader. */
  toFrame(x, y) {
    const vx = x / this.view.cw;
    const vy = 1 - y / this.view.ch;
    return [(vx - 0.5) * this.scale[0] + 0.5, (vy - 0.5) * this.scale[1] + 0.5];
  }

  upload(slot, key, bmp) {
    const s = this.tex[slot];
    if (!bmp || s.key === key) return;
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE0 + s.unit);
    gl.bindTexture(gl.TEXTURE_2D, s.t);
    if (s.w === bmp.width && s.h === bmp.height) {
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, gl.RGBA, gl.UNSIGNED_BYTE, bmp);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, bmp);
      s.w = bmp.width;
      s.h = bmp.height;
    }
    s.key = key;
  }

  frames(aKey, aBmp, bKey, bBmp, mix) {
    this.upload(0, aKey, aBmp);
    this.upload(1, bKey, bBmp);
    this.state.mix = mix;
  }

  light(k0, b0, k1, b1, mix, linear) {
    if (!b0) {
      this.state.light = 0;
      return;
    }
    this.upload(2, k0, b0);
    this.upload(3, b1 ? k1 : k0, b1 || b0);
    this.state.lmix = b1 ? mix : 0;
    this.state.light = 1;
    this.state.linear = linear ? 1 : 0;
  }

  pointer(u, v, intensity) {
    this.state.pointer = [u, v, intensity];
  }

  draw() {
    if (this.lost) return;
    const gl = this.gl;
    const s = this.state;
    gl.uniform1f(this.u.uMix, s.mix);
    gl.uniform1f(this.u.uLMix, s.lmix);
    gl.uniform2f(this.u.uScale, this.scale[0], this.scale[1]);
    gl.uniform1f(this.u.uAspect, this.size.w / this.size.h);
    gl.uniform3f(this.u.uPointer, s.pointer[0], s.pointer[1], s.pointer[2]);
    gl.uniform1f(this.u.uLight, s.light);
    gl.uniform1f(this.u.uLinear, s.linear);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}
