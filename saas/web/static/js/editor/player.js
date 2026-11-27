// The preview player. The video's pixels already hold its captions, hook, logo and memes (libass burns them
// in), so the page cannot change them before a render. It approximates instead: an HTML layer draws the
// hook, logo and memes that differ from what the playing video already shows, and playback stays inside the
// trim handles.
//
// Times: every time the editor deals in is on the ORIGINAL clip's timeline (spec time). A video may start
// later than that (a trimmed version), so `offset` is the spec time of the video's first frame.
import { memeVisual } from "./media.js";
import { memeSpans, same } from "./state.js";
import { clamp } from "./util.js";

const STEP = 1 / 30;

export class Player {
  constructor(root, { onTime = () => {}, onPlayState = () => {} } = {}) {
    this.root = root;
    this.video = root.querySelector("[data-video]");
    this.hookBox = root.querySelector("[data-ov-hook]");
    this.hookText = root.querySelector("[data-ov-hook-text]");
    this.logo = root.querySelector("[data-ov-logo]");
    this.memesBox = root.querySelector("[data-ov-memes]");
    this.note = root.querySelector("[data-ov-note]");
    this.modeTag = root.querySelector("[data-player-mode]");
    this.msg = root.querySelector("[data-player-msg]");
    this.skeleton = root.querySelector("[data-player-skeleton]");
    this.onTime = onTime;
    this.onPlayState = onPlayState;

    this.mode = "preview"; // "preview": your edits over the clip; "version": a rendered version as it is
    this.offset = 0;
    this.state = null;
    this.burned = null; // what the playing video already shows (null: a clean picture, draw everything)
    this.ctx = null;
    this.thumbs = new Map();
    this.logoUrl = "";
    this.selected = null;
    this.forceHook = false;
    this.frame = 0;
    this.memeKey = null;

    const v = this.video;
    v.muted = false;
    v.addEventListener("play", () => {
      this.onPlayState(true);
      this.loop();
    });
    v.addEventListener("pause", () => {
      this.onPlayState(false);
      this.paint();
    });
    v.addEventListener("seeked", () => this.paint());
    v.addEventListener("timeupdate", () => {
      if (v.paused) this.paint();
    });
    v.addEventListener("loadeddata", () => {
      this.skeleton.hidden = true;
      this.msg.hidden = true;
    });
    v.addEventListener("error", () => {
      if (!v.getAttribute("src")) return;
      this.skeleton.hidden = true;
      this.message("This video didn’t load. Reload the page to try again; the download still works.");
    });
    v.addEventListener("click", () => this.toggle());
  }

  /** Spec time now. */
  get time() {
    return (this.video.currentTime || 0) + this.offset;
  }

  get playing() {
    return !this.video.paused && !this.video.ended;
  }

  get videoEnd() {
    const d = this.video.duration;
    return Number.isFinite(d) && d > 0 ? this.offset + d : Infinity;
  }

  message(text) {
    this.msg.textContent = text;
    this.msg.hidden = !text;
  }

  /** Load a video. `at` (spec time) is where to land once it can seek; the current time by default. */
  load({ url, offset = 0, mode = "preview", label = "Preview", burned = null, poster = "", at = null }) {
    const land = at === null ? this.time : at;
    const v = this.video;
    this.mode = mode;
    this.burned = burned;
    this.modeTag.textContent = label;
    this.root.dataset.mode = mode;
    if (v.getAttribute("src") === url) {
      this.offset = offset;
      this.seek(land);
      this.paint();
      return Promise.resolve();
    }
    v.pause();
    this.offset = offset;
    this.skeleton.hidden = false;
    this.message("");
    return new Promise((resolve) => {
      const done = () => {
        v.removeEventListener("loadedmetadata", done);
        v.removeEventListener("error", done);
        this.seek(land);
        this.paint();
        resolve();
      };
      v.addEventListener("loadedmetadata", done);
      v.addEventListener("error", done);
      if (poster) v.poster = poster;
      v.src = url;
      v.load();
    });
  }

  setState(state, ctx) {
    this.state = state;
    this.ctx = ctx;
    this.paint();
  }

  /** `memes`: [{id, url, label}] from the editor's list of memes it can use. */
  setThumbs(memes) {
    this.thumbs = new Map((memes || []).map((m) => [m.id, { url: m.url || "", label: m.label || "Meme" }]));
    this.memeKey = null;
  }

  setLogoUrl(url) {
    this.logoUrl = url || "";
  }

  select(k) {
    this.selected = k;
    this.paint();
  }

  seek(t) {
    const v = this.video;
    const d = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : null;
    const local = Math.max(0, t - this.offset);
    try {
      v.currentTime = d === null ? local : clamp(local, 0, Math.max(0, d - STEP));
    } catch (_) {
      /* not seekable yet: load() seeks again once metadata arrives */
    }
    this.paint();
  }

  nudge(seconds) {
    this.seek(this.time + seconds);
  }

  async play() {
    const v = this.video;
    if (this.mode === "preview" && this.state) {
      const { start_s: start, end_s: end } = this.state.trim;
      const t = this.time;
      if (t < start - 0.05 || t >= end - 0.05) this.seek(Math.max(start, this.offset));
    } else if (v.ended || v.currentTime >= (v.duration || 0) - STEP) {
      v.currentTime = 0;
    }
    try {
      await v.play();
    } catch (_) {
      /* autoplay policy or a missing video: the error handler explains the second case */
    }
  }

  pause() {
    this.video.pause();
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  loop() {
    cancelAnimationFrame(this.frame);
    const tick = () => {
      if (!this.playing) return;
      if (this.mode === "preview" && this.state) {
        const { start_s: start, end_s: end } = this.state.trim;
        const t = this.time;
        if (t >= end - STEP / 2) {
          // Stop on the trim's last frame; the next Play starts again at the first.
          this.video.pause();
          this.seek(end - STEP);
          return;
        }
        if (t < start - 0.1) this.seek(start);
      }
      this.paint();
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }

  /** Draw the overlay for the current time. Cheap enough to run every frame. */
  paint() {
    const t = this.time;
    this.onTime(t);
    const s = this.state;
    const preview = this.mode === "preview" && s;
    const burned = this.burned;
    const lim = this.ctx ? this.ctx.limits : null;

    // Hook: the yellow box over the opening seconds of the (trimmed) clip.
    const hookOn = Boolean(preview && s.hook.enabled && s.hook.text &&
      !(burned && burned.hook.enabled && burned.hook.text === s.hook.text) &&
      (this.forceHook || (t >= s.trim.start_s - 0.01 && t < s.trim.start_s + (lim ? lim.hookSeconds : 2.5))));
    this.hookBox.hidden = !hookOn;
    if (hookOn && this.hookText.textContent !== s.hook.text) this.hookText.textContent = s.hook.text;

    // Logo: only when it differs from what the video already shows.
    const logoOn = Boolean(preview && this.logoUrl && this.ctx && this.ctx.logoAvailable && s.logo.enabled &&
      !(burned && same(burned.logo, s.logo)));
    this.logo.hidden = !logoOn;
    if (logoOn) {
      if (this.logo.getAttribute("src") !== this.logoUrl) this.logo.src = this.logoUrl;
      this.logo.dataset.position = s.logo.position || "center";
      // Canvas 1080 wide: a scaled logo fits a box `scale` wide and half as tall; the default box is 220x88.
      const w = s.logo.scale === null ? 220 / 1080 : s.logo.scale;
      const h = s.logo.scale === null ? 88 / 1080 : s.logo.scale / 2;
      this.logo.style.setProperty("--logo-w", `${(w * 100).toFixed(2)}cqw`);
      this.logo.style.setProperty("--logo-h", `${(h * 100).toFixed(2)}cqw`);
    }

    // Memes the video doesn't already have, while they are on screen.
    const shown = [];
    if (preview) {
      const spans = memeSpans(s.memes, lim ? lim.memeMin : 0.5); // when each really shows (one corner, no overlap)
      s.memes.forEach((m, k) => {
        const inBurned = burned && burned.memes.some((b) => b.id === m.id && Math.abs(b.start_s - m.start_s) < 0.05 &&
          Math.abs(b.duration_s - m.duration_s) < 0.05);
        if ((!inBurned || k === this.selected) && t >= spans[k].start && t < spans[k].end) shown.push(k);
      });
    }
    // Rebuilt only when the set on screen changes (at a meme's edges), never every frame.
    const key = shown.map((k) => `${k}:${s.memes[k].id}:${k === this.selected}`).join("|");
    if (key !== this.memeKey) {
      this.memeKey = key;
      this.memesBox.textContent = "";
      shown.forEach((k) => {
        const meme = this.thumbs.get(s.memes[k].id) || { url: "", label: "Meme" };
        const node = memeVisual(meme.url, meme.label, "ed-ov__meme");
        node.dataset.selected = String(k === this.selected);
        this.memesBox.append(node);
      });
    }

    // Where the picture can't show the edit.
    let note = "";
    if (preview) {
      if (t < this.offset - 0.05 || t > this.videoEnd + 0.05) note = "This part isn’t in the video you’re previewing. Render to see it.";
      else if (!this.playing && (t < s.trim.start_s - 0.01 || t > s.trim.end_s + 0.01)) note = "Cut by the trim";
    }
    this.note.hidden = !note;
    if (note && this.note.textContent !== note) this.note.textContent = note;
  }
}
