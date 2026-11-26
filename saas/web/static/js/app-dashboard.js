// /app: balance, the two free meters, the plan, and recent jobs (or a first-run guide).
import { ago, api, clock, el, minutes, statusBadge } from "./api.js";
import { freeGrant, me } from "./app-shell.js";

function meter(text, bar, left, total) {
  text.textContent = total ? `${left} of ${total} left` : String(left);
  bar.style.width = `${total ? Math.max(0, Math.min(100, (left / total) * 100)) : 0}%`;
}

me.then((account) => {
  if (!account) return;
  document.querySelector("[data-stat-balance]").textContent = minutes(account.balance);
  document.querySelector("[data-stat-plan]").textContent = account.plan?.name || "Free";
  const grant = freeGrant();
  const free = account.free || { minutes: 0, shorts: 0 };
  meter(document.querySelector("[data-free-shorts-text]"), document.querySelector("[data-free-shorts-bar]"),
    free.shorts, grant.shorts);
  meter(document.querySelector("[data-free-minutes-text]"), document.querySelector("[data-free-minutes-bar]"),
    free.minutes, grant.minutes);
  const note = document.querySelector("[data-free-note]");
  const teach = document.querySelector("[data-teach-free]");
  if (!account.email_verified) {
    note.textContent = "Unlocks when you confirm your email.";
  } else if (!account.free_credits_granted) {
    note.textContent = "Not available on this account. Paid minutes work as normal.";
  } else if (free.shorts <= 0 || free.minutes <= 0) {
    note.textContent = "Used up. Paid minutes from here.";
  } else if (teach) {
    teach.textContent = `Your first ${grant.shorts} Shorts or ${grant.minutes} minutes are free, whichever comes first.`;
  }
});

async function loadJobs() {
  const box = document.querySelector("[data-jobs]");
  try {
    const { jobs } = await api("/api/jobs?limit=8");
    box.textContent = "";
    box.removeAttribute("aria-busy");
    if (!jobs.length) {
      box.hidden = true;
      document.querySelector("[data-jobs-empty]").hidden = false;
      return;
    }
    const list = el("div", { class: "job-list" });
    for (const job of jobs) {
      const title = job.source.title || job.source.url || "Uploaded video";
      const live = !["done", "failed", "canceled"].includes(job.status);
      const meta = [
        ago(job.created_at),
        job.source.duration_s ? clock(job.source.duration_s) : null,
        job.options?.clips ? `${job.options.clips} clips asked` : null,
        live ? `${Math.round((job.progress || 0) * 100)}%` : null,
      ].filter(Boolean).join(" · ");
      const cost = job.funding === "free"
        ? `${job.shorts_charged ?? job.shorts_reserved ?? 0} free Shorts`
        : minutes(job.credits_charged ?? job.credits_reserved ?? 0);
      list.append(el("a", { class: "job-row", href: `/app/jobs/${encodeURIComponent(job.id)}`, "data-track": "dash-job-row" },
        el("div", {}, el("div", { class: "job-row__title", text: title }), el("div", { class: "job-row__meta", text: meta })),
        el("div", { class: "job-row__credits", text: cost }),
        statusBadge(job.status)));
    }
    box.append(list);
    if (jobs.some((j) => !["done", "failed", "canceled"].includes(j.status))) setTimeout(loadJobs, 4000);
  } catch (err) {
    box.hidden = true;
    const out = document.querySelector("[data-jobs-error]");
    out.textContent = `Couldn’t load your jobs. ${err.message}`;
    out.hidden = false;
  }
}

loadJobs();
