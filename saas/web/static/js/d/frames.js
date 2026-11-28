// The film's frames: a few packs of WebP images (scripts/landing_film/encode.py), fetched with real byte progress,
// kept compressed, and decoded only near the playhead. Holding the whole film decoded would take about a gigabyte.

/** Fetch a URL, reporting bytes as they arrive. Resolves to a Blob. */
export async function fetchBytes(url, onBytes) {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  if (!res.body || !res.body.getReader) {
    const buf = await res.arrayBuffer();
    onBytes?.(buf.byteLength);
    return new Blob([buf]);
  }
  const reader = res.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.byteLength;
    onBytes?.(got);
  }
  return new Blob(chunks);
}

export class FrameStore {
  /**
   * @param {string} base  folder of the manifest, e.g. "/static/media/d/"
   * @param {object} cam   the manifest entry for one camera
   * @param {number} keep  how many decoded frames to hold
   */
  constructor(base, cam, keep) {
    this.base = base;
    this.cam = cam;
    this.count = cam.frames.length;
    this.keep = keep;
    this.packs = cam.packs.map(() => null); // Blob per loaded pack
    this.have = new Uint8Array(this.count + 2); // 1-based: is frame f's pack here
    this.loadedList = []; // sorted frame numbers available
    this.decoded = new Map(); // frame -> ImageBitmap, oldest first
    this.pending = new Map(); // frame -> Promise
    this.light = null; // Blob of the light pass
  }

  url(file, v) {
    return `${this.base}${file}?v=${v}`;
  }

  packBytes(i) {
    return this.cam.packs[i].bytes;
  }

  async loadPack(i, onBytes) {
    if (this.packs[i]) return;
    const p = this.cam.packs[i];
    this.packs[i] = await fetchBytes(this.url(p.file, p.v), onBytes);
    this.cam.frames.forEach(([pack], k) => {
      if (pack === i) this.have[k + 1] = 1;
    });
    this.loadedList = [];
    for (let f = 1; f <= this.count; f++) if (this.have[f]) this.loadedList.push(f);
  }

  async loadLight() {
    const l = this.cam.light;
    if (!l || this.light) return;
    this.light = await fetchBytes(this.url(l.file, l.v));
  }

  /** The nearest loaded frames at or below and at or above x (a fractional frame). */
  around(x) {
    const list = this.loadedList;
    if (!list.length) return null;
    let lo = list[0];
    let hi = list[list.length - 1];
    // binary search
    let a = 0;
    let b = list.length - 1;
    while (a <= b) {
      const m = (a + b) >> 1;
      if (list[m] <= x) {
        lo = list[m];
        a = m + 1;
      } else {
        hi = list[m];
        b = m - 1;
      }
    }
    if (lo > x) lo = list[0];
    if (hi < x) hi = list[list.length - 1];
    return [lo, hi];
  }

  blobOf(f) {
    const [pack, off, len] = this.cam.frames[f - 1];
    return this.packs[pack].slice(off, off + len, "image/webp");
  }

  get(f) {
    const bmp = this.decoded.get(f);
    if (bmp) {
      this.decoded.delete(f);
      this.decoded.set(f, bmp);
    }
    return bmp || null;
  }

  /** Decode frame f (if its pack is here). Resolves to the bitmap, or null. */
  decode(f) {
    if (f < 1 || f > this.count || !this.have[f]) return Promise.resolve(null);
    const ready = this.get(f);
    if (ready) return Promise.resolve(ready);
    if (this.pending.has(f)) return this.pending.get(f);
    const job = createImageBitmap(this.blobOf(f))
      .then((bmp) => {
        this.pending.delete(f);
        this.decoded.set(f, bmp);
        while (this.decoded.size > this.keep) {
          const [old, oldBmp] = this.decoded.entries().next().value;
          this.decoded.delete(old);
          oldBmp.close?.();
        }
        return bmp;
      })
      .catch(() => {
        this.pending.delete(f);
        return null;
      });
    this.pending.set(f, job);
    return job;
  }

  /** Light pass frames are every `every` frames; decode the pair around x. */
  lightPair(x) {
    const l = this.cam.light;
    if (!l || !this.light) return null;
    const step = l.every;
    const i0 = Math.max(0, Math.min(l.frames.length - 1, Math.floor((x - 1) / step)));
    const i1 = Math.min(l.frames.length - 1, i0 + 1);
    const t = Math.max(0, Math.min(1, (x - 1 - i0 * step) / step));
    return [i0, i1, t];
  }

  decodeLight(i) {
    const key = -1 - i; // shares the cache with negative keys
    const ready = this.get(key);
    if (ready) return Promise.resolve(ready);
    if (this.pending.has(key)) return this.pending.get(key);
    const [off, len] = this.cam.light.frames[i];
    const job = createImageBitmap(this.light.slice(off, off + len, "image/webp"), {
      colorSpaceConversion: "none",
      premultiplyAlpha: "none",
    })
      .then((bmp) => {
        this.pending.delete(key);
        this.decoded.set(key, bmp);
        while (this.decoded.size > this.keep + 4) {
          const [old, oldBmp] = this.decoded.entries().next().value;
          this.decoded.delete(old);
          oldBmp.close?.();
        }
        return bmp;
      })
      .catch(() => {
        this.pending.delete(key);
        return null;
      });
    this.pending.set(key, job);
    return job;
  }
}
