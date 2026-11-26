// /waitlist/thanks: the result of joining (sessionStorage) or a lookup by referral code (?code=).
import { RESULT_KEY, track } from "./site.js";

const root = document.querySelector("[data-thanks]");

function show(state) {
  root.querySelectorAll("[data-thanks-state]").forEach((el) => {
    el.hidden = el.getAttribute("data-thanks-state") !== state;
  });
  const busy = root.querySelector('[data-thanks-state="loading"]');
  if (busy) busy.setAttribute("aria-busy", String(state === "loading"));
}

function stored() {
  try {
    return JSON.parse(sessionStorage.getItem(RESULT_KEY) || "null");
  } catch (_) {
    return null;
  }
}

function shareLinks(url) {
  const brand = root.dataset.brand || "";
  const text = `I’m on the waitlist for ${brand}. It turns long videos into finished Shorts. Join with my link:`;
  const enc = encodeURIComponent;
  const targets = {
    x: `https://twitter.com/intent/tweet?text=${enc(text)}&url=${enc(url)}`,
    whatsapp: `https://wa.me/?text=${enc(`${text} ${url}`)}`,
    linkedin: `https://www.linkedin.com/sharing/share-offsite/?url=${enc(url)}`,
    email: `mailto:?subject=${enc(`Join me on the ${brand} waitlist`)}&body=${enc(`${text}\n\n${url}`)}`,
  };
  root.querySelectorAll("[data-share]").forEach((a) => {
    a.href = targets[a.getAttribute("data-share")] || "#";
  });
  const native = root.querySelector("[data-native-share]");
  if (native && navigator.share) {
    native.hidden = false;
    native.addEventListener("click", async () => {
      try {
        await navigator.share({ title: brand, text, url });
        track("referral_shared", { via: "native" });
      } catch (_) {
        /* the visitor closed the share sheet */
      }
    });
  }
}

function render(result) {
  const position = Number(result.position);
  const posEl = root.querySelector("[data-thanks-position]");
  if (Number.isFinite(position) && position > 0) {
    root.querySelector("[data-position]").textContent = position.toLocaleString();
    posEl.hidden = false;
  }
  root.querySelector("[data-thanks-already]").hidden = !result.already;

  const url = result.referral_url || "";
  if (url) {
    root.querySelector("[data-referral-url]").value = url;
    shareLinks(url);
    root.querySelector("[data-thanks-share]").hidden = false;
  }
  const referrals = Number(result.referrals);
  if (Number.isFinite(referrals) && referrals > 0) {
    const el = root.querySelector("[data-referrals]");
    el.textContent = referrals === 1 ? "1 person has joined with your link." : `${referrals} people have joined with your link.`;
    el.hidden = false;
  }
  show("joined");
}

async function lookup(code) {
  show("loading");
  try {
    const res = await fetch(`/api/waitlist/me?code=${encodeURIComponent(code)}`, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    if (res.status === 404) {
      show("empty");
      return;
    }
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    render({ ...(stored() || {}), ...data });
  } catch (_) {
    const local = stored();
    if (local) render(local);
    else show("error");
  }
}

function start() {
  const code = root.dataset.code || new URLSearchParams(location.search).get("code") || "";
  if (code) {
    lookup(code);
    return;
  }
  const local = stored();
  if (local) render(local);
  else show("empty");
}

if (root) {
  root.querySelector("[data-thanks-retry]")?.addEventListener("click", start);
  start();
}
