// Behaviour shared by every page. Each feature binds to data attributes, so markup decides what is on.

/** Where a waitlist join leaves its result for /waitlist/thanks. */
export const RESULT_KEY = "ds_waitlist";

/** Headers for a state-changing fetch: the double-submit CSRF token, when the security layer set one. */
export function csrfHeaders() {
  const match = document.cookie.match(/(?:^|;\s*)csrf=([^;]+)/);
  return match ? { "X-CSRF-Token": decodeURIComponent(match[1]) } : {};
}

/** A custom analytics event. t.js may be absent (ad blockers), so this never throws. */
export function track(name, props = {}) {
  try {
    if (typeof window.dsTrack === "function") window.dsTrack(name, props);
  } catch (_) {
    /* analytics must never break the page */
  }
}

/** A short status message at the bottom of the screen, announced to screen readers. */
export function toast(message, kind = "info", ms = 3200) {
  const host = document.querySelector("[data-toasts]");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast${kind === "error" ? " toast--error" : ""}`;
  el.textContent = message;
  host.append(el);
  setTimeout(() => el.remove(), ms);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    // Clipboard API needs a secure context; fall back to a selection copy.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "visually-hidden";
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (_) {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

/** Keep a referral code from ?ref= for the waitlist form, even if the visitor wanders first. */
function rememberReferral() {
  const ref = new URLSearchParams(location.search).get("ref");
  if (!ref) return;
  try {
    localStorage.setItem("ds_ref", ref.slice(0, 64));
  } catch (_) {
    /* private mode: the server already rendered it into the form */
  }
}

/** Where this visit started: the first page (with its UTM tags) and the site that sent the visitor.
 *  Kept for the tab's session, so joining from /pricing still credits the original campaign. */
export const LANDING_KEY = "ds_landing";

function rememberLanding() {
  try {
    if (sessionStorage.getItem(LANDING_KEY)) return;
    sessionStorage.setItem(LANDING_KEY, JSON.stringify({
      page_url: location.href.slice(0, 2000),
      referrer: document.referrer.slice(0, 2000),
    }));
  } catch (_) {
    /* storage blocked: the join sends the current page instead */
  }
}

export function landing() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(LANDING_KEY) || "null");
    if (stored && typeof stored.page_url === "string") return stored;
  } catch (_) {
    /* fall through */
  }
  return { page_url: location.href.slice(0, 2000), referrer: document.referrer.slice(0, 2000) };
}

function header() {
  const bar = document.querySelector("[data-site-header]");
  if (!bar) return;
  const onScroll = () => bar.setAttribute("data-scrolled", String(window.scrollY > 8));
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });

  const toggle = bar.querySelector("[data-menu-toggle]");
  if (!toggle) return;
  const setOpen = (open) => {
    bar.setAttribute("data-menu", open ? "open" : "closed");
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));
  bar.querySelectorAll(".site-nav__links a").forEach((a) => a.addEventListener("click", () => setOpen(false)));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      setOpen(false);
      toggle.focus();
    }
  });
}

/** WAI-ARIA tabs: arrow keys move between tabs, Home/End jump. */
function tabs() {
  document.querySelectorAll("[data-tabs]").forEach((root) => {
    const list = Array.from(root.querySelectorAll('[role="tab"]'));
    const select = (tab, focus) => {
      list.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        const panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
      if (focus) tab.focus();
    };
    list.forEach((tab, i) => {
      tab.addEventListener("click", () => select(tab, false));
      tab.addEventListener("keydown", (e) => {
        const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: list.length - 1 }[e.key];
        if (next === undefined) return;
        e.preventDefault();
        select(list[(next + list.length) % list.length], true);
      });
    });
  });
}

/** [data-copy="#id"] copies that element's value or text. */
function copyButtons() {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const target = document.querySelector(btn.getAttribute("data-copy"));
    if (!target) return;
    const text = "value" in target && target.value !== undefined ? target.value : target.textContent;
    const ok = await copyText(text.trim());
    toast(ok ? "Copied." : "Could not copy. Select the text and copy it yourself.", ok ? "info" : "error");
    if (ok) {
      const label = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = label), 1600);
    }
  });
}

/** Native <dialog> modals: [data-modal-open="#id"] opens, [data-modal-close] closes. */
function modals() {
  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-modal-open]");
    if (opener) {
      const dialog = document.querySelector(opener.getAttribute("data-modal-open"));
      if (dialog && typeof dialog.showModal === "function") dialog.showModal();
      return;
    }
    const closer = e.target.closest("[data-modal-close]");
    if (closer) closer.closest("dialog")?.close();
  });
}

/** "Cookie settings" reopens the consent choice (consent.js or t.js), else explains on /cookies. */
function consentLinks() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-consent-open]");
    if (!btn) return;
    if (window.dsConsent && typeof window.dsConsent.open === "function") {
      window.dsConsent.open();
    } else {
      location.href = "/cookies#choices";
    }
  });
}

// Page modules import "./site.js" while the page also loads "site.js?v=<hash>": two URLs, so this module
// can be evaluated twice. Bind the page behaviour once.
if (!window.__dsSiteReady) {
  window.__dsSiteReady = true;
  rememberLanding();
  rememberReferral();
  header();
  tabs();
  copyButtons();
  modals();
  consentLinks();
}
