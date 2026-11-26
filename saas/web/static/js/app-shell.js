// The signed-in shell: menu on phones, balance in the sidebar, sign out, resend the confirmation link.
// Page modules import `me` from here so the account is fetched once per page.
import { api, busy, fail, minutes } from "./api.js";
import { toast } from "./site.js";

export const me = api("/api/me").catch((err) => {
  if (err.status !== 401) fail(err);
  return null;
});

/** The free-tier grant (totals), written into the page by the server. */
export function freeGrant() {
  const side = document.querySelector("[data-app-side]");
  return {
    minutes: Number(side?.dataset.freeMinutes || 0),
    shorts: Number(side?.dataset.freeShorts || 0),
  };
}

export function freeLine(account) {
  if (!account || !account.free) return "";
  const { minutes: m, shorts: s } = account.free;
  if (!account.free_credits_granted) return account.email_verified ? "" : "Free tier unlocks when you confirm your email";
  if (m <= 0 || s <= 0) return "Free tier used";
  const grant = freeGrant();
  return `Free: ${s} of ${grant.shorts || s} Shorts · ${m} of ${grant.minutes || m} min left`;
}

const side = document.querySelector("[data-app-side]");
if (side && !window.__dsShellReady) {
  window.__dsShellReady = true;
  const toggle = side.querySelector("[data-app-menu]");
  toggle?.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    side.setAttribute("data-menu", open ? "open" : "closed");
  });

  me.then((account) => {
    if (!account) return;
    side.querySelector("[data-shell-minutes]").textContent = minutes(account.balance);
    side.querySelector("[data-shell-free]").textContent = freeLine(account);
  });

  side.querySelector("[data-sign-out]")?.addEventListener("click", async (e) => {
    await busy(e.currentTarget, async () => {
      try {
        await api("/auth/logout", { method: "POST" });
      } catch (_) {
        /* signed out either way below */
      }
      location.assign("/login");
    });
  });

  document.querySelector("[data-resend-verify]")?.addEventListener("click", async (e) => {
    const button = e.currentTarget;
    await busy(button, async () => {
      try {
        const res = await api("/auth/verify/resend", { method: "POST" });
        if (res.already_verified) {
          location.reload();
          return;
        }
        toast("Sent. Check your inbox.");
      } catch (err) {
        fail(err);
      }
    });
  });
}
