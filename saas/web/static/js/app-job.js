// /app/jobs/{id}: live stage and progress, then each clip with its player, editable text and publish package.
import { api, browserTimeZone, busy, clock, dateTime, el, fail, minutes, plural, statusBadge } from "./api.js";
import { me } from "./app-shell.js";
import { copyText, toast } from "./site.js";

const root = document.querySelector("[data-job]");
const jobId = root.dataset.job;
const STAGES = ["queued", "fetching", "transcribing", "selecting", "rendering", "done"];
const FINAL = ["done", "failed", "canceled"];
const rendered = new Set();
let accountTz = "";

me.then((account) => {
  accountTz = account?.timezone && account.timezone !== "UTC" ? account.timezone : "";
});

/* ---- the job ----------------------------------------------------------------------------------- */

function facts(job) {
  const box = root.querySelector("[data-job-facts]");
  box.textContent = "";
  const add = (label, value) => {
    if (value === null || value === undefined || value === "") return;
    box.append(el("div", {}, el("dt", { text: label }), el("dd", { text: value })));
  };
  add("Length", job.source.duration_s ? clock(job.source.duration_s) : "");
  add("Clips asked", job.options?.clips);
  if (job.funding === "free") {
    add(job.status === "done" ? "Free Shorts used" : "Free Shorts set aside", job.status === "done" ? job.shorts_charged : job.shorts_reserved);
  } else {
    add(job.status === "done" ? "Charged" : "Set aside", minutes(job.status === "done" ? job.credits_charged : job.credits_reserved));
  }
  add("Started", job.started_at ? dateTime(job.started_at) : dateTime(job.created_at));
  if (job.finished_at) add("Finished", dateTime(job.finished_at));
}

function stages(job) {
  const live = root.querySelector("[data-job-live]");
  const running = !FINAL.includes(job.status);
  live.hidden = !running;
  if (!running) return;
  const now = Math.max(0, STAGES.indexOf(job.stage || job.status));
  root.querySelectorAll("[data-stage]").forEach((li) => {
    const i = STAGES.indexOf(li.dataset.stage);
    li.dataset.state = i < now ? "done" : i === now ? "now" : "todo";
  });
  const pct = Math.round((job.progress || 0) * 100);
  root.querySelector("[data-job-bar]").style.width = `${pct}%`;
  root.querySelector("[data-job-progress]").setAttribute("aria-valuenow", String(pct));
  root.querySelector("[data-job-cancel]").hidden = Boolean(job.cancel_requested);
  if (job.cancel_requested) root.querySelector("[data-job-hint]").textContent = "Stopping at the next step. Your minutes come back in full.";
}

function render(job) {
  document.title = `${job.source.title || "Job"} | ${document.title.split(" | ").pop()}`;
  root.querySelector("[data-job-title]").textContent = job.source.title || job.source.url || "Uploaded video";
  const status = root.querySelector("[data-job-status]");
  status.textContent = "";
  status.append(statusBadge(job.status));
  facts(job);
  stages(job);
  if (FINAL.includes(job.status)) root.querySelector("[data-job-cancel]").hidden = true;
  const error = root.querySelector("[data-job-error]");
  if (job.error && job.status !== "done") {
    error.textContent = `${job.error.message || "The job stopped."} ${job.status === "canceled" ? "" : "Any minutes set aside went back to your balance."}`.trim();
    error.hidden = false;
  }
  root.querySelector("[data-job-watermark]").hidden = !job.watermark;
  const clips = job.clips || [];
  if (clips.length) {
    root.querySelector("[data-job-clips-section]").hidden = false;
    root.querySelector("[data-clips-heading]").textContent = plural(clips.length, "clip", "clips");
    clips.forEach((clip) => {
      if (!rendered.has(clip.id)) {
        rendered.add(clip.id);
        root.querySelector("[data-clip-grid]").append(clipCard(clip));
      }
    });
    if (location.hash && !root.dataset.scrolled) {
      root.dataset.scrolled = "1";
      document.querySelector(location.hash)?.scrollIntoView();
    }
  }
}

async function poll() {
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    render(job);
    if (!FINAL.includes(job.status)) setTimeout(poll, document.hidden ? 8000 : 2000);
  } catch (err) {
    if (err.status === 404) {
      root.querySelector("[data-job-missing]").hidden = false;
      root.querySelector("[data-job-title]").textContent = "Job not found";
      return;
    }
    fail(err);
    setTimeout(poll, 6000);
  }
}

root.querySelector("[data-job-cancel]").addEventListener("click", async (e) => {
  if (!window.confirm("Cancel this job? Minutes set aside for it come back in full.")) return;
  await busy(e.currentTarget, async () => {
    try {
      render(await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }));
    } catch (err) {
      fail(err);
    }
  });
});

/* ---- a clip ------------------------------------------------------------------------------------ */

function counters(form) {
  form.querySelectorAll("[data-limit]").forEach((input) => {
    const out = form.querySelector(`[data-count="${input.name}"]`);
    const update = () => {
      const n = input.value.length;
      const limit = Number(input.dataset.limit);
      out.textContent = `${n}/${limit}`;
      out.dataset.over = String(n > limit);
    };
    input.addEventListener("input", update);
    update();
  });
}

function clipCard(clip) {
  const node = document.querySelector("[data-clip-template]").content.firstElementChild.cloneNode(true);
  node.id = `clip-${clip.id}`;
  const video = node.querySelector("[data-video]");
  video.src = clip.video_url;
  video.poster = clip.thumbnail_url;
  video.setAttribute("aria-label", `Clip ${clip.index + 1}: ${clip.title || ""}`);
  video.addEventListener("error", () => {
    node.querySelector("[data-player-fallback]").hidden = false;
  });

  const head = node.querySelector("[data-clip-head]");
  head.append(el("span", { class: "badge badge--accent", text: `Clip ${clip.index + 1}` }),
    el("span", { class: "mono", text: clock(clip.duration_s) }),
    el("span", { text: `From ${clock(clip.start_s)} to ${clock(clip.end_s)} in the source` }));

  const form = node.querySelector("[data-clip-form]");
  form.querySelectorAll("[data-for]").forEach((label) => {
    const id = `${node.id}-${label.dataset.for}`;
    label.setAttribute("for", id);
    form.elements[label.dataset.for].id = id;
  });
  form.elements.title.value = clip.title || "";
  form.elements.description.value = clip.description || "";
  form.elements.hashtags.value = (clip.hashtags || []).join(" ");
  counters(form);
  const saveState = node.querySelector("[data-save-state]");
  form.addEventListener("input", () => {
    saveState.textContent = "Unsaved changes";
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const hashtags = form.elements.hashtags.value.split(/[\s,]+/).map((t) => t.trim()).filter(Boolean)
      .map((t) => (t.startsWith("#") ? t : `#${t}`));
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        const saved = await api(`/api/clips/${encodeURIComponent(clip.id)}`, {
          method: "PATCH",
          body: { title: form.elements.title.value.trim(), description: form.elements.description.value, hashtags },
        });
        form.elements.hashtags.value = (saved.hashtags || hashtags).join(" ");
        saveState.textContent = "Saved";
        const pkg = node.querySelector("[data-package]");
        if (!pkg.hidden) loadPackage(clip, pkg);
        else pkg.dataset.loaded = "";
      } catch (err) {
        saveState.textContent = err.message;
      }
    });
  });

  const download = node.querySelector("[data-download]");
  download.href = clip.download_url;
  download.setAttribute("download", "");

  // The clip editor (editor agent, saas/web/editor_routes.py).
  const edit = node.querySelector("[data-clip-edit]");
  edit.href = `/app/clips/${encodeURIComponent(clip.id)}/edit`;
  edit.setAttribute("aria-label", `Edit clip ${(clip.index ?? 0) + 1}`);

  const toggle = node.querySelector("[data-package-toggle]");
  const pkg = node.querySelector("[data-package]");
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    pkg.hidden = !open;
    if (open && !pkg.dataset.loaded) loadPackage(clip, pkg);
  });
  return node;
}

/* ---- the publish package ------------------------------------------------------------------------ */

function zones(selected) {
  const list = new Set(["UTC", browserTimeZone(), accountTz, selected].filter(Boolean));
  try {
    (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []).forEach((z) => list.add(z));
  } catch (_) {
    /* older browsers: the short list is enough */
  }
  return [...list].sort();
}

function copyBlock(label, text, track) {
  const pre = el("pre", { class: "copy-block__text", text });
  const button = el("button", { class: "btn btn--ghost btn--sm", type: "button", "data-track": track, text: "Copy" });
  button.addEventListener("click", async () => {
    const ok = await copyText(text);
    toast(ok ? `${label} copied.` : "Could not copy. Select the text and copy it yourself.", ok ? "info" : "error");
  });
  return el("div", { class: "copy-block" }, el("div", { class: "copy-block__label" }, el("span", { text: label }), button), pre);
}

/** `local` is ISO with the zone's own offset, so its wall-clock part is read as written, never re-zoned. */
function times(list) {
  return el("ol", { class: "times", role: "list" }, (list || []).map((t) => {
    const hhmm = String(t.local).slice(11, 16);
    const day = t.day || String(t.local).slice(0, 10);
    return el("li", {}, el("strong", { text: hhmm }),
      el("span", { text: [day, t.part_of_day].filter(Boolean).join(" · ") }));
  }));
}

async function loadPackage(clip, box, tz) {
  const zone = tz || box.dataset.tz || accountTz || browserTimeZone();
  box.dataset.tz = zone;
  box.textContent = "";
  box.append(el("span", { class: "skeleton skeleton--text" }), el("span", { class: "skeleton skeleton--text" }));
  let data;
  try {
    data = await api(`/api/clips/${encodeURIComponent(clip.id)}/package?tz=${encodeURIComponent(zone)}`);
  } catch (err) {
    box.textContent = "";
    box.append(el("p", { class: "notice notice--error", text: `Couldn’t build the package. ${err.message}` }));
    return;
  }
  box.dataset.loaded = "1";
  box.textContent = "";

  const select = el("select", { class: "select", "aria-label": "Time zone for the suggested times", "data-track": "package-tz" });
  zones(data.timezone || zone).forEach((z) => select.append(el("option", { value: z, selected: z === (data.timezone || zone), text: z })));
  select.addEventListener("change", () => loadPackage(clip, box, select.value));

  const zip = el("a", { class: "btn btn--sm", href: data.files?.zip_url || `/api/clips/${clip.id}/package.zip?tz=${encodeURIComponent(zone)}`, download: "", "data-track": "package-zip", text: "Download package (.zip)" });
  box.append(el("div", { class: "package__head" },
    el("h3", { class: "h4", text: "Ready to post" }),
    el("label", { class: "package__tz" }, el("span", { text: "Times in" }), select)));

  const platforms = data.platforms || {};
  const tabs = el("div", { class: "tabs", "data-tabs": "" });
  const list = el("div", { class: "tabs__list", role: "tablist", "aria-label": "Platform" });
  tabs.append(list);
  Object.entries(platforms).forEach(([key, p], i) => {
    const tabId = `${clip.id}-${key}-tab`;
    const panelId = `${clip.id}-${key}-panel`;
    const tab = el("button", { class: "tabs__tab", role: "tab", type: "button", id: tabId, "aria-controls": panelId,
      "aria-selected": String(i === 0), tabindex: i === 0 ? "0" : "-1", "data-track": `package-tab-${key}`, text: p.label || key });
    list.append(tab);
    const panel = el("div", { class: "tabs__panel", role: "tabpanel", id: panelId, "aria-labelledby": tabId, hidden: i !== 0 });
    if (p.title) panel.append(copyBlock("Title", p.title, `package-copy-title-${key}`));
    if (p.description) panel.append(copyBlock("Description", p.description, `package-copy-description-${key}`));
    if (p.caption) panel.append(copyBlock("Caption", p.caption, `package-copy-caption-${key}`));
    if (p.tags && p.tags.length) panel.append(copyBlock("Tags", p.tags.join(", "), `package-copy-tags-${key}`));
    panel.append(el("p", { class: "label package__times-label", text: "Suggested times to post" }), times(p.suggested_times));
    tabs.append(panel);
  });
  wireTabs(tabs);
  box.append(tabs);
  if (data.schedule_note) box.append(el("p", { class: "hint", text: data.schedule_note }));
  box.append(el("div", { class: "clip-card__actions" }, zip));
}

/** Tabs built after load: the same keyboard behaviour as site.js gives server-rendered tabs. */
function wireTabs(root) {
  const tabs = [...root.querySelectorAll('[role="tab"]')];
  const select = (tab, focus) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      root.querySelector(`#${CSS.escape(t.getAttribute("aria-controls"))}`).hidden = !on;
    });
    if (focus) tab.focus();
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab, false));
    tab.addEventListener("keydown", (e) => {
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (next === undefined) return;
      e.preventDefault();
      select(tabs[(next + tabs.length) % tabs.length], true);
    });
  });
}

poll();
