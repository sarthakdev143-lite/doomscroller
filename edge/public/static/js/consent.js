// First-party consent for the static site. Google Analytics 4 and Microsoft Clarity load only after the
// visitor presses Accept; the choice is kept in localStorage. Google Consent Mode v2 starts fully denied.
// The IDs come from <meta name="ds:ga4"> and <meta name="ds:clarity">, written at export time.

const KEY = "ds_consent";
const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content.trim() || "";
const ga4 = /^G-[A-Z0-9]{4,20}$/.test(meta("ds:ga4")) ? meta("ds:ga4") : "";
const clarityId = /^[a-z0-9]{6,20}$/i.test(meta("ds:clarity")) ? meta("ds:clarity") : "";
const banner = document.querySelector("[data-consent]");

window.dataLayer = window.dataLayer || [];
window.gtag = window.gtag || function gtag() {
  window.dataLayer.push(arguments); // eslint-disable-line prefer-rest-params
};
window.gtag("consent", "default", {
  ad_storage: "denied",
  ad_user_data: "denied",
  ad_personalization: "denied",
  analytics_storage: "denied",
});

let granted = false;
let loaded = false;

function stored() {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || "null");
    return value && (value.choice === "granted" || value.choice === "denied") ? value.choice : null;
  } catch (_) {
    return null;
  }
}

function remember(choice) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ choice, at: new Date().toISOString() }));
  } catch (_) {
    /* private mode: the banner simply asks again next visit */
  }
}

function inject(src) {
  const el = document.createElement("script");
  el.async = true;
  el.src = src;
  document.head.append(el);
}

function sectionViews() {
  if (!("IntersectionObserver" in window)) return;
  const seen = new Set();
  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const name = entry.target.getAttribute("data-track-section");
      if (!entry.isIntersecting || seen.has(name)) return;
      seen.add(name);
      if (granted && ga4) window.gtag("event", "section_view", { section: name });
    });
  }, { threshold: 0.4 });
  document.querySelectorAll("[data-track-section]").forEach((el) => io.observe(el));
}

function grant() {
  granted = true;
  window.gtag("consent", "update", { analytics_storage: "granted" });
  if (loaded) return;
  loaded = true;
  if (ga4) {
    inject(`https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(ga4)}`);
    window.gtag("js", new Date());
    window.gtag("config", ga4);
  }
  if (clarityId) {
    window.clarity = window.clarity || function clarity() {
      (window.clarity.q = window.clarity.q || []).push(arguments); // eslint-disable-line prefer-rest-params
    };
    inject(`https://www.clarity.ms/tag/${encodeURIComponent(clarityId)}`);
    window.clarity("consent");
  }
  sectionViews();
}

function deny() {
  granted = false;
  window.gtag("consent", "update", { analytics_storage: "denied" });
  if (loaded && typeof window.clarity === "function") window.clarity("consent", false);
}

function choose(choice) {
  remember(choice);
  if (choice === "granted") grant();
  else deny();
  if (banner) banner.hidden = true;
}

// site.js routes its custom events (waitlist_submit, demo_toggle, ...) through window.dsTrack.
window.dsTrack = (name, props = {}) => {
  if (granted && ga4) window.gtag("event", name, props);
};

// "Cookie settings" in the footer reopens the choice (site.js calls dsConsent.open()).
window.dsConsent = {
  open() {
    if (!banner) return;
    banner.hidden = false;
    banner.querySelector("[data-consent-choice]")?.focus();
  },
};

document.addEventListener("click", (e) => {
  const choice = e.target.closest("[data-consent-choice]");
  if (choice) {
    choose(choice.getAttribute("data-consent-choice"));
    return;
  }
  const el = e.target.closest("[data-track]");
  if (!el || !granted || !ga4) return;
  window.gtag("event", "track_click", {
    track_id: el.getAttribute("data-track"),
    section: el.closest("[data-track-section]")?.getAttribute("data-track-section") || "",
  });
}, { capture: true });

const previous = stored();
if (previous === "granted") grant();
else if (previous === null && banner) banner.hidden = false;
