// Waitlist forms: validate, post JSON to POST /api/waitlist, then show /waitlist/thanks.
import { RESULT_KEY, csrfHeaders, landing, track } from "./site.js";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

/** One human sentence per failure, preferring the server’s own message when it sent one. */
function messageFor(status, body, retryAfter) {
  const server = body && typeof body.message === "string" && body.message.trim() ? body.message.trim() : "";
  const code = body && typeof body.error === "string" ? body.error : "";
  if (status === 0) return "We couldn’t reach the server. Check your connection and try again.";
  if (status === 429) {
    const wait = Number(retryAfter);
    const when = wait > 0 ? (wait < 90 ? `${Math.ceil(wait)} seconds` : `${Math.ceil(wait / 60)} minutes`) : "a minute";
    return `Too many tries from your network. Wait ${when} and try again.`;
  }
  if (status === 422 || code === "invalid_email" || code === "invalid_request") {
    return server && code !== "invalid_request" ? server : "That email address doesn’t look right. Check it and try again.";
  }
  if (status === 409 || code === "already_joined") {
    return server || "That email is already on the list.";
  }
  if (status === 403) return server || "We couldn’t verify this request. Refresh the page and try again.";
  if (status >= 500) return "Something broke on our side. Try again in a minute.";
  return server || "That didn’t work. Try again.";
}

/** The contract promises referral_url, not referral_code; the code is its ?ref= parameter. */
function referralCode(url) {
  try {
    return url ? new URL(url, location.origin).searchParams.get("ref") || "" : "";
  } catch (_) {
    return "";
  }
}

function setError(form, input, text) {
  const out = form.querySelector("[data-form-error]");
  if (out) out.textContent = text || "";
  if (input) input.setAttribute("aria-invalid", text ? "true" : "false");
}

function setBusy(form, busy) {
  const btn = form.querySelector('button[type="submit"]');
  if (!btn) return;
  btn.disabled = busy;
  btn.setAttribute("aria-busy", String(busy));
}

function payload(form) {
  const value = (name) => (form.elements[name] ? String(form.elements[name].value || "").trim() : "");
  let ref = value("ref");
  if (!ref) {
    try {
      ref = localStorage.getItem("ds_ref") || "";
    } catch (_) {
      ref = "";
    }
  }
  const body = {
    email: value("email"),
    ts: Number(value("ts")) || Date.now(),
    website: form.elements.website ? form.elements.website.value : "",
  };
  if (form.elements.fp) body.fp = value("fp"); // only the Python app's tracker fills it; the static site has none
  const visit = landing(); // the first page of this visit and its external referrer (UTM tags survive /pricing)
  body.page_url = visit.page_url;
  if (visit.referrer) body.referrer = visit.referrer;
  if (value("role")) body.role = value("role");
  if (value("channel_url")) body.channel_url = value("channel_url");
  if (ref) body.ref = ref;
  // Turnstile writes its token into a hidden input with a hyphenated name; the API wants underscores.
  const turnstile = form.querySelector('[name="cf-turnstile-response"]');
  if (turnstile && turnstile.value) body.cf_turnstile_response = turnstile.value;
  return body;
}

async function submit(form) {
  const email = form.elements.email;
  const body = payload(form);
  if (!EMAIL.test(body.email)) {
    setError(form, email, "Enter your email address, like name@example.com.");
    email.focus();
    return;
  }
  if (form.elements.channel_url && body.channel_url && !/^https?:\/\/\S+\.\S+/.test(body.channel_url)) {
    setError(form, form.elements.channel_url, "Your channel link should start with https://");
    form.elements.channel_url.focus();
    return;
  }
  setError(form, email, "");
  setBusy(form, true);
  const formId = form.getAttribute("data-form-id") || "form";
  track("waitlist_submit", { form: formId });

  let res;
  let data = null;
  try {
    res = await fetch("/api/waitlist", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", ...csrfHeaders() },
      body: JSON.stringify(body),
    });
    data = await res.json().catch(() => null);
  } catch (_) {
    res = null;
  }
  setBusy(form, false);

  if (res && res.ok && data && data.ok !== false) {
    track("waitlist_joined", { form: formId, already: Boolean(data.already) });
    const code = data.referral_code || referralCode(data.referral_url);
    try {
      sessionStorage.setItem(RESULT_KEY, JSON.stringify({
        position: data.position ?? null,
        referral_url: data.referral_url || "",
        referral_code: code,
        already: Boolean(data.already),
      }));
    } catch (_) {
      /* storage blocked: the thanks page falls back to the code in the URL */
    }
    const query = code ? `?code=${encodeURIComponent(code)}` : "";
    location.assign(`/waitlist/thanks/${query}`); // trailing slash: the static site serves waitlist/thanks/index.html
    return;
  }

  const status = res ? res.status : 0;
  const message = messageFor(status, data, res ? res.headers.get("Retry-After") : null);
  setError(form, status === 422 || status === 400 ? email : null, message);
  track("waitlist_error", { form: formId, status });
  if (window.turnstile && typeof window.turnstile.reset === "function") {
    try {
      window.turnstile.reset(form.querySelector(".cf-turnstile"));
    } catch (_) {
      /* the widget may not have rendered */
    }
  }
}

document.querySelectorAll("[data-waitlist-form]").forEach((form) => {
  if (form.elements.ts) form.elements.ts.value = String(Date.now());
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit(form);
  });
  const email = form.elements.email;
  email?.addEventListener("input", () => {
    if (email.getAttribute("aria-invalid") === "true") setError(form, email, "");
  });
});
