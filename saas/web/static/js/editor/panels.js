// The inspector: captions, hook, logo, memes and sound. Each control writes to the store; sync() puts the state
// back into the controls (after undo, redo or a timeline drag) and leaves alone a control that has focus.
import { el } from "../api.js";
import { memeVisual } from "./media.js";
import { clearStart, fitMeme, memeSpans, setEdit } from "./state.js";
import { captionLines, clamp, stamp, wordAt } from "./util.js";

const $ = (root, sel) => root.querySelector(sel);

export class Panels {
  constructor(root, { store, onSeek, onSelectMeme, getTime, player }) {
    this.root = root;
    this.store = store;
    this.onSeek = onSeek;
    this.onSelectMeme = onSelectMeme;
    this.getTime = getTime;
    this.player = player;
    this.ctx = null;
    this.labels = new Map();
    this.available = [];
    this.selected = null;
    this.focusI = null;
    this.nowI = -1;
    this.wordsKey = "";
    this.atText = "";
    this.rows = [];

    this.captions = {
      enabled: $(root, "[data-captions-enabled]"),
      box: $(root, "[data-words]"),
      empty: $(root, "[data-words-empty]"),
      hint: $(root, "[data-words-hint]"),
      summary: $(root, "[data-edits-summary]"),
      count: $(root, "[data-edits-count]"),
      reset: $(root, "[data-edits-reset]"),
    };
    this.hook = {
      enabled: $(root, "[data-hook-enabled]"),
      text: $(root, "[data-hook-text]"),
      count: $(root, "[data-hook-count]"),
    };
    this.logo = {
      enabled: $(root, "[data-logo-enabled]"),
      missing: $(root, "[data-logo-missing]"),
      corners: $(root, "[data-logo-corners]"),
      scale: $(root, "[data-logo-scale]"),
      out: $(root, "[data-logo-scale-out]"),
    };
    this.memes = {
      at: $(root, "[data-meme-at]"),
      picker: $(root, "[data-meme-picker]"),
      none: $(root, "[data-memes-none]"),
      full: $(root, "[data-memes-full]"),
      locked: $(root, "[data-memes-locked]"),
      list: $(root, "[data-meme-list]"),
      empty: $(root, "[data-meme-list-empty]"),
      count: $(root, "[data-meme-count]"),
    };
    this.sound = {
      sfx: $(root, "[data-sfx-enabled]"),
      music: $(root, "[data-music-enabled]"),
      volume: $(root, "[data-music-volume]"),
      volumeOut: $(root, "[data-music-volume-out]"),
      volumeField: $(root, "[data-music-volume-field]"),
      broll: $(root, "[data-broll-enabled]"),
    };
    this.bind();
  }

  change(mutate, opts) {
    return this.store.change(mutate, opts);
  }

  /* ---- setup --------------------------------------------------------------------------------- */

  setup(ctx, available, labels) {
    this.ctx = ctx;
    this.available = available;
    this.labels = labels;
    this.wordByI = new Map(ctx.words.map((w) => [String(w.i), w]));
    const lim = ctx.limits;
    this.hook.text.maxLength = lim.hookChars;
    this.logo.scale.min = String(Math.round(lim.logoMin * 100));
    this.logo.scale.max = String(Math.round(lim.logoMax * 100));
    this.sound.volume.min = String(lim.dbMin);
    this.sound.volume.max = String(lim.dbMax);
    this.captions.empty.hidden = ctx.words.length > 0;
    this.captions.hint.hidden = ctx.words.length === 0;
    this.logo.missing.hidden = Boolean(ctx.logoAvailable);
    // The engine only places memes on clips cut from speech; say so here, not after a failed render.
    this.memes.locked.hidden = ctx.memesEditable;
    this.root.querySelector("[data-meme-tools]").hidden = !ctx.memesEditable;
    this.buildPicker();
  }

  buildPicker() {
    const { picker, none } = this.memes;
    picker.textContent = "";
    none.hidden = this.available.length > 0 || !this.ctx.memesEditable;
    this.available.forEach((m) => {
      const label = this.labels.get(m.id) || "Meme";
      const tile = el("button", { class: "ed-pick", type: "button", "data-id": m.id, "data-track": "editor-meme-add" },
        el("span", { class: "ed-pick__media" }, memeVisual(m.url, label, "ed-pick__img", m.mediaType)),
        el("span", { class: "ed-pick__label", text: label }),
        el("span", { class: "ed-pick__tag", text: m.custom ? "Your brand kit" : "From this clip" }));
      picker.append(tile);
    });
  }

  /* ---- events -------------------------------------------------------------------------------- */

  bind() {
    const c = this.captions;
    c.enabled.addEventListener("change", () => this.change((s) => {
      s.captions.enabled = c.enabled.checked;
    }));
    c.reset.addEventListener("click", () => this.change((s) => {
      s.captions.edits = [];
    }));
    c.box.addEventListener("click", (e) => {
      const button = e.target.closest(".ed-word");
      if (button) this.editWord(Number(button.dataset.i));
    });
    c.box.addEventListener("keydown", (e) => this.wordKeys(e));
    c.box.addEventListener("focusin", (e) => {
      const button = e.target.closest(".ed-word");
      if (button) this.rove(Number(button.dataset.i));
    });

    const h = this.hook;
    h.enabled.addEventListener("change", () => this.change((s) => {
      s.hook.enabled = h.enabled.checked;
    }));
    h.text.addEventListener("input", () => {
      const text = h.text.value.replace(/[\u0000-\u001f\u007f]/g, " ");
      this.change((s) => {
        if (!s.hook.text.trim() && text.trim()) s.hook.enabled = true; // typing a hook means showing it
        s.hook.text = text;
      }, { key: "hook-text" });
      this.counter();
    });
    h.text.addEventListener("focus", () => this.showHook(true));
    h.text.addEventListener("blur", () => this.showHook(false));

    const l = this.logo;
    l.enabled.addEventListener("change", () => this.change((s) => {
      s.logo.enabled = l.enabled.checked;
    }));
    l.corners.addEventListener("change", (e) => {
      if (e.target.name !== "ed-logo-position") return;
      this.change((s) => {
        s.logo.position = e.target.value || null;
        s.logo.enabled = true;
      });
    });
    l.scale.addEventListener("input", () => {
      this.change((s) => {
        s.logo.scale = clamp(Number(l.scale.value) / 100, this.ctx.limits.logoMin, this.ctx.limits.logoMax);
      }, { key: "logo-scale" });
    });

    const m = this.memes;
    m.picker.addEventListener("click", (e) => {
      const tile = e.target.closest(".ed-pick");
      if (tile && !tile.disabled) this.addMeme(tile.dataset.id);
    });
    m.list.addEventListener("change", (e) => {
      const row = e.target.closest("[data-k]");
      if (!row) return;
      const k = Number(row.dataset.k);
      if (e.target.matches("[data-meme-sound]")) {
        this.change((s) => {
          s.memes[k].sound = e.target.checked;
        });
      } else if (e.target.matches("[data-meme-start], [data-meme-length]")) {
        const value = Number(e.target.value);
        if (!Number.isFinite(value)) {
          this.sync(this.store.state, true);
          return;
        }
        const field = e.target.matches("[data-meme-start]") ? "start_s" : "duration_s";
        const changed = this.change((s) => {
          s.memes[k] = fitMeme({ ...s.memes[k], [field]: value }, this.ctx);
        });
        if (!changed) this.sync(this.store.state, true); // put back a clamped value
        if (field === "start_s") this.onSeek(this.store.state.memes[k].start_s + 0.01);
      }
    });
    m.list.addEventListener("click", (e) => {
      const row = e.target.closest("[data-k]");
      if (!row) return;
      const k = Number(row.dataset.k);
      if (e.target.closest("[data-meme-remove]")) {
        this.change((s) => {
          s.memes.splice(k, 1);
        });
        this.onSelectMeme(null);
        const rows = m.list.querySelectorAll("[data-meme-remove]");
        (rows[Math.min(k, rows.length - 1)] || m.picker.querySelector(".ed-pick") || this.root).focus();
      } else if (e.target.closest("[data-meme-select]")) {
        this.onSelectMeme(k);
        const meme = this.store.state.memes[k];
        if (meme) this.onSeek(meme.start_s + 0.01);
      }
    });
    m.list.addEventListener("keydown", (e) => {
      // Enter in a number field commits it, like leaving the field.
      if (e.key === "Enter" && e.target.matches("input[type=number]")) e.target.blur();
    });

    const so = this.sound;
    so.sfx.addEventListener("change", () => this.change((s) => {
      s.sfx.enabled = so.sfx.checked;
    }));
    so.music.addEventListener("change", () => this.change((s) => {
      s.music.enabled = so.music.checked;
    }));
    so.volume.addEventListener("input", () => this.change((s) => {
      s.music.volume_db = Math.round(Number(so.volume.value));
    }, { key: "music-volume" }));
    so.broll.addEventListener("change", () => this.change((s) => {
      s.broll.enabled = so.broll.checked;
    }));
  }

  showHook(on) {
    if (!this.player) return;
    this.player.forceHook = on;
    this.player.paint();
  }

  counter() {
    const n = this.hook.text.value.length;
    const max = this.ctx ? this.ctx.limits.hookChars : 80;
    this.hook.count.textContent = `${n}/${max}`;
    this.hook.count.dataset.over = String(n > max);
  }

  addMeme(id) {
    const s = this.store.state;
    const lim = this.ctx.limits;
    if (s.memes.length >= lim.maxMemes) return;
    const length = lim.memeDefault;
    const t = clamp(this.getTime(), s.trim.start_s, Math.max(s.trim.start_s, s.trim.end_s - length));
    // Memes share one corner: a new one starts clear of the others (the engine refuses two within 0.5 s).
    const meme = fitMeme({ id, start_s: clearStart(s.memes, t, lim.memeMin), duration_s: length, sound: true }, this.ctx);
    this.change((x) => {
      x.memes.push(meme);
    });
    const k = this.store.state.memes.length - 1;
    this.onSelectMeme(k);
    this.onSeek(meme.start_s + 0.01);
  }

  /* ---- caption words ------------------------------------------------------------------------- */

  buildWords(state) {
    const box = this.captions.box;
    const words = this.ctx.words;
    const edits = new Map(state.captions.edits.map((e) => [e.i, e.text]));
    const { start_s: start, end_s: end } = state.trim;
    if (this.focusI === null && words.length) this.focusI = words[0].i;
    const hadFocus = box.contains(document.activeElement) && document.activeElement.matches(".ed-word");
    box.textContent = "";
    captionLines(words).forEach((line) => {
      const row = el("div", { class: "ed-line", "data-line": "" },
        el("span", { class: "ed-line__time mono", text: stamp(line.start_s) }));
      const list = el("span", { class: "ed-line__words" });
      line.words.forEach((w) => {
        const fixed = edits.get(w.i);
        const button = el("button", {
          class: "ed-word", type: "button", "data-i": w.i, tabindex: w.i === this.focusI ? "0" : "-1",
          "data-track": "editor-caption-word", "data-edited": fixed !== undefined ? "true" : null,
          "data-out": w.end_s <= start + 0.001 || w.start_s >= end - 0.001 ? "true" : null,
          "aria-label": fixed !== undefined ? `${fixed} (fixed, was ${w.text})` : null,
          title: fixed !== undefined ? `Was “${w.text}”` : null,
          text: fixed !== undefined ? fixed : w.text,
        });
        list.append(button, " ");
      });
      row.append(list);
      box.append(row);
    });
    this.markNow(this.nowI, true);
    if (hadFocus) this.wordButton(this.focusI)?.focus();
  }

  wordButton(i) {
    return this.captions.box.querySelector(`.ed-word[data-i="${i}"]`);
  }

  rove(i) {
    if (this.focusI === i) return;
    this.wordButton(this.focusI)?.setAttribute("tabindex", "-1");
    this.focusI = i;
    this.wordButton(i)?.setAttribute("tabindex", "0");
  }

  wordKeys(e) {
    if (!e.target.matches(".ed-word")) return;
    const buttons = [...this.captions.box.querySelectorAll(".ed-word")];
    const at = buttons.indexOf(e.target);
    let next = null;
    if (e.key === "ArrowRight") next = at + 1;
    else if (e.key === "ArrowLeft") next = at - 1;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = buttons.length - 1;
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const line = e.target.closest("[data-line]");
      const target = e.key === "ArrowDown" ? line.nextElementSibling : line.previousElementSibling;
      const first = target && target.querySelector(".ed-word");
      if (first) next = buttons.indexOf(first);
    } else if (e.key === "F2") {
      e.preventDefault();
      this.editWord(Number(e.target.dataset.i));
      return;
    }
    if (next === null) return;
    e.preventDefault();
    const button = buttons[clamp(next, 0, buttons.length - 1)];
    if (!button) return;
    this.rove(Number(button.dataset.i));
    button.focus();
  }

  editWord(i) {
    const word = this.ctx.words.find((w) => w.i === i);
    const button = this.wordButton(i);
    if (!word || !button) return;
    this.rove(i);
    this.onSeek(word.start_s + 0.01);
    const current = button.textContent;
    const input = el("input", {
      class: "ed-word-input", type: "text", value: current, maxlength: this.ctx.limits.captionChars,
      size: Math.max(3, current.length + 1), spellcheck: "true", autocomplete: "off",
      "aria-label": `Fix the word “${word.text}”. Enter saves, Escape cancels.`,
    });
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const text = input.value;
      input.replaceWith(button);
      if (save && text.trim()) {
        this.change((s) => setEdit(s, word, text, this.ctx.limits.captionChars), { reason: "caption" });
      }
      this.wordButton(i)?.focus();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        finish(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        finish(false);
      }
    });
    input.addEventListener("input", () => {
      input.size = Math.max(3, input.value.length + 1);
    });
    input.addEventListener("blur", () => finish(true));
    button.replaceWith(input);
    input.focus();
    input.select();
  }

  /** Words the trim cuts off, dimmed in place (a drag must not rebuild the list on every move). */
  markOut(trim) {
    this.captions.box.querySelectorAll(".ed-word").forEach((button) => {
      const w = this.wordByI.get(button.dataset.i);
      if (!w) return;
      const out = w.end_s <= trim.start_s + 0.001 || w.start_s >= trim.end_s - 0.001;
      if (out) button.setAttribute("data-out", "true");
      else button.removeAttribute("data-out");
    });
  }

  markNow(i, force = false) {
    if (i === this.nowI && !force) return;
    if (this.nowI >= 0) this.wordButton(this.nowI)?.removeAttribute("data-now");
    this.nowI = i;
    if (i >= 0) this.wordButton(i)?.setAttribute("data-now", "true");
  }

  /* ---- memes on the clip --------------------------------------------------------------------- */

  drawMemeList(state) {
    const { list, empty, count, picker, full } = this.memes;
    const memes = state.memes;
    const lim = this.ctx.limits;
    while (this.rows.length > memes.length) this.rows.pop().row.remove();
    while (this.rows.length < memes.length) {
      const k = this.rows.length;
      const thumb = el("span", { class: "ed-mrow__thumb", "aria-hidden": "true" });
      const name = el("button", { class: "link-btn ed-mrow__name", type: "button", "data-meme-select": "", "data-track": "editor-meme-select" });
      const start = el("input", { class: "input ed-num mono", type: "number", step: "0.1", min: "0", inputmode: "decimal", "data-meme-start": "" });
      const length = el("input", { class: "input ed-num mono", type: "number", step: "0.1", min: String(lim.memeMin), max: String(lim.memeMax), inputmode: "decimal", "data-meme-length": "" });
      const sound = el("input", { type: "checkbox", "data-meme-sound": "" });
      const remove = el("button", { class: "btn btn--ghost btn--sm", type: "button", "data-meme-remove": "", "data-track": "editor-meme-remove", text: "Remove" });
      const note = el("p", { class: "ed-mrow__note small", hidden: true });
      const row = el("li", { class: "ed-mrow", "data-k": k },
        thumb,
        el("div", { class: "ed-mrow__main" }, name,
          el("div", { class: "ed-mrow__fields" },
            el("label", { class: "ed-mini" }, el("span", { text: "Starts at (s)" }), start),
            el("label", { class: "ed-mini" }, el("span", { text: "Shows for (s)" }), length),
            el("label", { class: "ed-mini ed-mini--check" }, sound, el("span", { text: "Sound" }))), note),
        remove);
      list.append(row);
      this.rows.push({ row, thumb, name, start, length, sound, remove, note });
    }
    const spans = memeSpans(memes, lim.memeMin);
    memes.forEach((m, k) => {
      const r = this.rows[k];
      const why = spans[k].clash ? "Starts too close to another meme: keep them at least half a second apart."
        : spans[k].cut ? `Shows until ${stamp(spans[k].end)}, where the next meme starts.` : "";
      r.note.hidden = !why;
      if (r.note.textContent !== why) r.note.textContent = why;
      r.row.dataset.clash = String(spans[k].clash);
      const label = this.labels.get(m.id) || "Meme";
      if (r.thumb.dataset.id !== m.id) {
        r.thumb.dataset.id = m.id;
        r.thumb.textContent = "";
        const found = this.available.find((a) => a.id === m.id) || {};
        r.thumb.append(memeVisual(found.url || "", label, "ed-mrow__img", found.mediaType));
      }
      if (r.name.textContent !== label) r.name.textContent = label;
      r.name.setAttribute("aria-label", `${label}: select it and jump to ${stamp(m.start_s)}`);
      r.remove.setAttribute("aria-label", `Remove ${label} at ${stamp(m.start_s)}`);
      r.start.max = String(Math.max(0, this.ctx.duration - m.duration_s).toFixed(1));
      r.start.setAttribute("aria-label", `${label} starts at, in seconds`);
      r.length.setAttribute("aria-label", `${label} shows for, in seconds`);
      if (document.activeElement !== r.start) r.start.value = m.start_s.toFixed(1);
      if (document.activeElement !== r.length) r.length.value = m.duration_s.toFixed(1);
      r.sound.checked = m.sound;
      r.row.dataset.selected = String(k === this.selected);
    });
    empty.hidden = memes.length > 0;
    count.textContent = memes.length ? `${memes.length} of ${lim.maxMemes}` : "";
    const isFull = memes.length >= lim.maxMemes;
    picker.querySelectorAll(".ed-pick").forEach((tile) => {
      tile.disabled = isFull;
    });
    full.hidden = !isFull;
    full.textContent = isFull ? `A clip can have up to ${lim.maxMemes} memes. Remove one to add another.` : "";
  }

  select(k) {
    this.selected = k;
    this.rows.forEach((r, n) => {
      r.row.dataset.selected = String(n === k);
    });
  }

  /* ---- state into controls ------------------------------------------------------------------- */

  sync(state, force = false) {
    const lim = this.ctx.limits;
    const c = this.captions;
    c.enabled.checked = state.captions.enabled;
    c.box.dataset.off = String(!state.captions.enabled);
    const key = JSON.stringify(state.captions.edits);
    if ((force || key !== this.wordsKey) && !c.box.querySelector(".ed-word-input")) {
      this.wordsKey = key;
      this.buildWords(state);
    } else {
      this.markOut(state.trim);
    }
    const fixes = state.captions.edits.length;
    c.summary.hidden = fixes === 0;
    c.count.textContent = fixes === 1 ? "1 word fixed." : `${fixes} words fixed.`;

    const h = this.hook;
    h.enabled.checked = state.hook.enabled;
    if (document.activeElement !== h.text && h.text.value !== state.hook.text) h.text.value = state.hook.text;
    this.counter();

    const l = this.logo;
    const avail = Boolean(this.ctx.logoAvailable);
    l.enabled.disabled = !avail;
    l.enabled.checked = avail && state.logo.enabled;
    const logoOn = avail && state.logo.enabled;
    l.corners.disabled = !logoOn;
    l.scale.disabled = !logoOn;
    l.corners.querySelectorAll("input[name=ed-logo-position]").forEach((radio) => {
      radio.checked = (radio.value || null) === state.logo.position;
    });
    const scale = state.logo.scale === null ? lim.logoDefault : state.logo.scale;
    if (document.activeElement !== l.scale) l.scale.value = String(Math.round(scale * 100));
    l.out.textContent = state.logo.scale === null ? "Default size" : `${Math.round(scale * 100)}% of the width`;

    const so = this.sound;
    so.sfx.checked = state.sfx.enabled;
    so.music.checked = state.music.enabled;
    so.volume.disabled = !state.music.enabled;
    so.volumeField.dataset.off = String(!state.music.enabled);
    const db = state.music.volume_db === null ? lim.dbDefault : state.music.volume_db;
    if (document.activeElement !== so.volume) so.volume.value = String(db);
    so.volumeOut.textContent = state.music.volume_db === null ? "The clip’s level" : `${db} dB`;
    so.broll.checked = state.broll.enabled;

    this.drawMemeList(state);
  }

  /** The playhead moved: the word playing now, and where a new meme would go. */
  setTime(t) {
    if (!this.ctx) return;
    const k = wordAt(this.ctx.words, t);
    this.markNow(k >= 0 ? this.ctx.words[k].i : -1);
    const at = `New memes go in at ${stamp(t)}`;
    if (at !== this.atText) {
      this.atText = at;
      this.memes.at.textContent = at;
      this.memes.picker.querySelectorAll(".ed-pick").forEach((tile) => {
        tile.setAttribute("aria-label", `Add ${this.labels.get(tile.dataset.id) || "meme"} at ${stamp(t)}`);
      });
    }
  }
}
