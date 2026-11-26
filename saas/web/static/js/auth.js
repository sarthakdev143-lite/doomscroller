// Sign-in pages: login (+ 2FA step and magic link), signup, forgot, reset, verify-email.
import { api, busy, safeNext } from "./api.js";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

/** fp (filled by t.js), ts (form shown), website (honeypot) and the Turnstile token, as the API expects. */
function identity(form) {
  const out = {
    fp: form.elements.fp ? form.elements.fp.value : "",
    ts: Number(form.elements.ts ? form.elements.ts.value : 0) || Date.now(),
    website: form.elements.website ? form.elements.website.value : "",
  };
  const token = form.querySelector('[name="cf-turnstile-response"]');
  if (token && token.value) out.cf_turnstile_response = token.value;
  return out;
}

function resetTurnstile(form) {
  const widget = form.querySelector(".cf-turnstile");
  if (widget && window.turnstile && typeof window.turnstile.reset === "function") {
    try {
      window.turnstile.reset(widget);
    } catch (_) {
      /* not rendered */
    }
  }
}

function setError(form, message, input) {
  const out = form.querySelector("[data-form-error]");
  if (out) out.textContent = message || "";
  form.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
  if (input && message) {
    input.setAttribute("aria-invalid", "true");
    input.focus();
  }
}

function showDone(panel, hideList) {
  hideList.forEach((el) => {
    if (el) el.hidden = true;
  });
  panel.hidden = false;
  panel.focus?.();
}

document.querySelectorAll("form [name=ts]").forEach((el) => {
  el.value = String(Date.now());
});

/* ---- login ------------------------------------------------------------------------------------ */

const loginPage = document.querySelector("[data-login-page]");
if (loginPage) {
  const next = safeNext(loginPage.dataset.next || new URLSearchParams(location.search).get("next"));
  const form = loginPage.querySelector("[data-login-form]");
  const mfaForm = loginPage.querySelector("[data-mfa-form]");
  const alt = loginPage.querySelector("[data-login-alt]");
  let mfaToken = loginPage.dataset.mfaToken || "";

  const askForCode = (token) => {
    mfaToken = token;
    loginPage.querySelector("h1").textContent = "Enter your code";
    loginPage.querySelector(".auth__lede").textContent = "Your account asks for a second step. Open your authenticator app.";
    form.hidden = true;
    alt.hidden = true;
    mfaForm.hidden = false;
    mfaForm.elements.code.focus();
  };
  if (mfaToken) askForCode(mfaToken);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = form.elements.email.value.trim();
    const password = form.elements.password.value;
    if (!EMAIL.test(email)) return setError(form, "Enter the email you signed up with.", form.elements.email);
    if (!password) return setError(form, "Enter your password.", form.elements.password);
    setError(form, "");
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        const res = await api("/auth/login", { method: "POST", body: { email, password, ...identity(form) } });
        if (res.mfa_required) askForCode(res.mfa_token);
        else location.assign(next);
      } catch (err) {
        setError(form, err.code === "invalid_credentials" ? "That email and password don’t match." : err.message);
        resetTurnstile(form);
      }
    });
  });

  mfaForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = mfaForm.elements.code.value.replace(/\s+/g, "");
    if (!code) return setError(mfaForm, "Enter the code.", mfaForm.elements.code);
    setError(mfaForm, "");
    await busy(mfaForm.querySelector("button[type=submit]"), async () => {
      try {
        await api("/auth/login/mfa", { method: "POST", body: { mfa_token: mfaToken, code } });
        location.assign(next);
      } catch (err) {
        const expired = err.status === 401 && err.code !== "invalid_code";
        setError(mfaForm, err.code === "invalid_code" ? "That code didn’t work. Codes change every 30 seconds."
          : expired ? "This sign-in expired. Start again." : err.message, mfaForm.elements.code);
      }
    });
  });

  loginPage.querySelector("[data-magic-link]").addEventListener("click", async (e) => {
    const email = form.elements.email.value.trim();
    if (!EMAIL.test(email)) {
      setError(form, "Enter your email first, then ask for the link.", form.elements.email);
      return;
    }
    setError(form, "");
    await busy(e.currentTarget, async () => {
      try {
        await api("/auth/magic", { method: "POST", body: { email, next, ...identity(form) } });
        loginPage.querySelector("[data-magic-email]").textContent = email;
        showDone(loginPage.querySelector("[data-magic-done]"), [form, alt]);
      } catch (err) {
        setError(form, err.message);
      }
    });
  });
}

/* ---- signup ----------------------------------------------------------------------------------- */

const signupPage = document.querySelector("[data-signup-page]");
if (signupPage) {
  const form = signupPage.querySelector("[data-signup-form]");
  if (form.elements.ref && !form.elements.ref.value) {
    try {
      form.elements.ref.value = localStorage.getItem("ds_ref") || "";
    } catch (_) {
      /* storage blocked */
    }
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = form.elements.email.value.trim();
    const password = form.elements.password.value;
    const min = Number(form.elements.password.getAttribute("minlength")) || 8;
    if (form.elements.invite && !form.elements.invite.value.trim()) {
      return setError(form, "Enter the invite code from your email.", form.elements.invite);
    }
    if (!EMAIL.test(email)) return setError(form, "Enter your email address, like name@example.com.", form.elements.email);
    if (password.length < min) return setError(form, `Use at least ${min} characters.`, form.elements.password);
    setError(form, "");
    const body = { email, password, ...identity(form) };
    if (form.elements.name.value.trim()) body.name = form.elements.name.value.trim();
    if (form.elements.invite) body.invite = form.elements.invite.value.trim();
    if (form.elements.ref && form.elements.ref.value) body.ref = form.elements.ref.value;
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        await api("/auth/signup", { method: "POST", body });
        signupPage.querySelector("[data-signup-email]").textContent = email;
        showDone(signupPage.querySelector("[data-signup-done]"), [form, signupPage.querySelector(".auth__alt")]);
      } catch (err) {
        const field = { weak_password: "password", breached_password: "password", invite_required: "invite",
          invalid_invite: "invite", invalid_email: "email" }[err.code];
        setError(form, err.message, field ? form.elements[field] : null);
        resetTurnstile(form);
      }
    });
  });
}

/* ---- forgot / reset / verify ------------------------------------------------------------------ */

const forgot = document.querySelector("[data-forgot-page]");
if (forgot) {
  const form = forgot.querySelector("[data-forgot-form]");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = form.elements.email.value.trim();
    if (!EMAIL.test(email)) return setError(form, "Enter your email address.", form.elements.email);
    setError(form, "");
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        await api("/auth/forgot", { method: "POST", body: { email, ...identity(form) } });
        forgot.querySelector("[data-forgot-email]").textContent = email;
        showDone(forgot.querySelector("[data-forgot-done]"), [form]);
      } catch (err) {
        setError(form, err.message);
        resetTurnstile(form);
      }
    });
  });
}

const reset = document.querySelector("[data-reset-page]");
if (reset) {
  const form = reset.querySelector("[data-reset-form]");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = form.elements.password.value;
    const min = Number(form.elements.password.getAttribute("minlength")) || 8;
    if (password.length < min) return setError(form, `Use at least ${min} characters.`, form.elements.password);
    if (password !== form.elements.confirm.value) return setError(form, "The two passwords don’t match.", form.elements.confirm);
    setError(form, "");
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        await api("/auth/reset", { method: "POST", body: { token: reset.dataset.token, password } });
        location.assign("/login?reset=1");
      } catch (err) {
        setError(form, err.status === 400 && /token/.test(err.code) ? "This link has expired or was already used. Ask for a new one." : err.message);
      }
    });
  });
}

const resend = document.querySelector("[data-resend]");
if (resend) {
  resend.addEventListener("click", async () => {
    const out = resend.closest(".done-state").querySelector("[data-form-error]");
    out.textContent = "";
    await busy(resend, async () => {
      try {
        const res = await api("/auth/verify/resend", { method: "POST" });
        if (res.already_verified) {
          location.assign("/app");
          return;
        }
        resend.textContent = "Sent. Check your inbox.";
        resend.disabled = true;
      } catch (err) {
        out.textContent = err.message;
      }
    });
  });
}
