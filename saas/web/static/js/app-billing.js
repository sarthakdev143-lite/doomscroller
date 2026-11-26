// /app/billing: balance, plan and subscription, buy buttons from the catalog, promo code, the ledger.
import { api, busy, dateTime, el, fail, minutes, plural } from "./api.js";
import { freeLine, me } from "./app-shell.js";
import { toast } from "./site.js";

const $ = (sel) => document.querySelector(sel);
let account = null;
let catalog = null;
let subscription = null;
let cursor = null;

const KIND = {
  grant: "Added", purchase: "Bought", pack: "Bought", plan: "Monthly minutes", subscription_grant: "Monthly minutes",
  reserve: "Set aside for a job", settle: "Job finished", release: "Returned from a job", refund: "Refunded",
  chargeback: "Reversed", promo: "Promo code", admin_adjust: "Adjusted by support", free_grant: "Free tier",
  referral: "Referral", expire: "Expired",
};

async function checkout(button, kind, slug, interval) {
  await busy(button, async () => {
    try {
      const body = { kind, slug };
      if (interval) body.interval = interval;
      const res = await api("/api/billing/checkout", { method: "POST", body });
      location.assign(res.redirect_url);
    } catch (err) {
      fail(err);
    }
  });
}

function offer({ name, price, note, button, current, onBuy, track }) {
  const btn = el("button", { class: `btn ${current ? "btn--ghost" : ""}`, type: "button", "data-track": track, disabled: current },
    el("span", { class: "spinner", "aria-hidden": "true" }), current ? "Your plan" : button);
  btn.addEventListener("click", () => onBuy(btn));
  return el("div", { class: "offer", "data-current": String(Boolean(current)) },
    el("p", { class: "offer__name", text: name }), price, el("p", { class: "offer__note", text: note }), btn);
}

function renderCatalog() {
  const packs = $("[data-packs]");
  const plans = $("[data-plans]");
  packs.textContent = "";
  plans.textContent = "";
  (catalog.packs || []).forEach((p) => {
    packs.append(offer({
      name: p.credits ? plural(p.credits, "minute", "minutes") : p.name,
      price: el("p", { class: "offer__price" }, p.price?.display || ""),
      note: p.description || plural(p.credits, "minute of video", "minutes of video"),
      button: `Buy ${plural(p.credits, "minute", "minutes")}`,
      track: `billing-buy-${p.slug}`,
      onBuy: (btn) => checkout(btn, "pack", p.slug),
    }));
  });
  if (!(catalog.packs || []).length) packs.append(el("p", { class: "muted", text: "Nothing to buy right now." }));

  const interval = () => document.querySelector('[name="interval"]:checked')?.value || "month";
  const hasYear = (catalog.plans || []).some((p) => p.prices?.year);
  $("[data-interval]").hidden = !hasYear;
  const draw = () => {
    plans.textContent = "";
    const per = interval();
    (catalog.plans || []).forEach((p) => {
      const price = p.prices?.[per] || p.prices?.month;
      if (!price) return;
      const current = account?.plan?.slug === p.slug && subscription && subscription.status !== "canceled";
      plans.append(offer({
        name: p.name,
        price: el("p", { class: "offer__price" }, price.display, el("small", { text: per === "year" ? " a year" : " a month" })),
        note: `${plural(p.monthly_credits, "minute", "minutes")} every month. ${p.description || ""}`.trim(),
        button: "Subscribe",
        current,
        track: `billing-subscribe-${p.slug}`,
        onBuy: (btn) => checkout(btn, "plan", p.slug, per),
      }));
    });
    if (!plans.children.length) plans.append(el("p", { class: "muted", text: "No monthly plans right now." }));
  };
  document.querySelectorAll('[name="interval"]').forEach((r) => r.addEventListener("change", draw));
  draw();

  const note = $("[data-pay-note]");
  if (catalog.provider === "bmc" && account) {
    note.textContent = `You pay on our payment page. Use ${account.email} there, so the payment reaches this account.`;
    note.hidden = false;
  }
}

function renderPlan() {
  $("[data-bill-plan]").textContent = account?.plan?.name || "Free";
  const subLine = $("[data-bill-sub]");
  const cancel = $("[data-bill-cancel]");
  if (!subscription) {
    subLine.textContent = "No monthly plan. You pay as you go.";
    cancel.hidden = true;
    return;
  }
  const end = subscription.current_period_end ? dateTime(subscription.current_period_end) : "";
  subLine.textContent = subscription.cancel_at_period_end
    ? `Ends ${end}. You keep the minutes already added.`
    : `${subscription.amount} a ${subscription.interval === "year" ? "year" : "month"} · renews ${end}`;
  cancel.hidden = subscription.cancel_at_period_end || subscription.status === "canceled";
}

$("[data-bill-cancel]").addEventListener("click", async (e) => {
  if (!window.confirm("Cancel at the end of this period? You keep the minutes already added.")) return;
  await busy(e.currentTarget, async () => {
    try {
      const res = await api("/api/billing/subscription/cancel", { method: "POST" });
      subscription = res.subscription || subscription;
      renderPlan();
      toast("Your plan ends at the end of this period.");
    } catch (err) {
      fail(err);
    }
  });
});

$("[data-promo-form]").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const out = $("[data-promo-error]");
  out.textContent = "";
  const code = form.elements.code.value.trim();
  if (!code) {
    out.textContent = "Enter a code.";
    return;
  }
  await busy(form.querySelector("button"), async () => {
    try {
      const res = await api("/api/billing/promo", { method: "POST", body: { code } });
      form.reset();
      if (res.credits_granted) {
        toast(`${plural(res.credits_granted, "minute", "minutes")} added.`);
        $("[data-bill-balance]").textContent = minutes(res.balance ?? (account.balance + res.credits_granted));
        cursor = null;
        loadLedger(true);
      } else if (res.percent_off) {
        toast(`${res.percent_off}% off your next checkout.`);
      }
    } catch (err) {
      out.textContent = err.message;
    }
  });
});

function signed(n) {
  const v = Number(n) || 0;
  return v > 0 ? `+${v}` : String(v);
}

async function loadLedger(reset = false) {
  const body = $("[data-ledger]");
  const query = new URLSearchParams({ limit: "25" });
  if (cursor && !reset) query.set("before", cursor);
  try {
    const res = await api(`/api/billing/ledger?${query}`);
    if (reset || !cursor) body.textContent = "";
    if (!res.entries.length && !body.children.length) {
      body.append(el("tr", {}, el("td", { colspan: "5", class: "muted", text: "Nothing yet. Purchases, jobs and returns show up here." })));
    }
    res.entries.forEach((e) => {
      const what = el("td", {}, KIND[e.kind] || e.kind.replace(/_/g, " "));
      if (e.note) what.append(el("span", { class: "table__note", text: e.note }));
      if (e.job_id) what.append(" ", el("a", { href: `/app/jobs/${encodeURIComponent(e.job_id)}`, "data-track": "billing-ledger-job", class: "small", text: "Job" }));
      const free = [e.free_shorts ? `${signed(e.free_shorts)} Shorts` : "", e.free_minutes ? `${signed(e.free_minutes)} min` : ""].filter(Boolean).join(" · ");
      body.append(el("tr", {},
        el("td", { text: dateTime(e.created_at) }), what,
        el("td", { class: `num ${e.amount > 0 ? "amount-pos" : e.amount < 0 ? "amount-neg" : ""}`, text: e.amount ? signed(e.amount) : "" }),
        el("td", { class: "num", text: free }),
        el("td", { class: "num", text: e.balance_after ?? "" })));
    });
    cursor = res.next;
    $("[data-ledger-more]").hidden = !res.next || res.entries.length < 25;
  } catch (err) {
    body.textContent = "";
    body.append(el("tr", {}, el("td", { colspan: "5", class: "error-text", text: err.message })));
  }
}

$("[data-ledger-more]").addEventListener("click", (e) => busy(e.currentTarget, () => loadLedger()));

Promise.all([
  me,
  api("/api/billing/catalog").catch(() => ({ plans: [], packs: [] })),
  api("/api/billing/subscription").catch(() => ({ subscription: null })),
]).then(([acct, cat, sub]) => {
  account = acct;
  catalog = cat;
  subscription = sub.subscription;
  if (account) {
    $("[data-bill-balance]").textContent = minutes(account.balance);
    $("[data-bill-free]").textContent = freeLine(account);
  }
  renderPlan();
  renderCatalog();
});
loadLedger(true);
