// /app/settings: profile and time zone, password, two-factor (QR + recovery codes), sessions, export/delete.
import { api, browserTimeZone, busy, dateTime, el, fail } from "./api.js";
import { me } from "./app-shell.js";
import { copyText, toast } from "./site.js";

const $ = (sel) => document.querySelector(sel);
let account = null;

/* ---- profile ----------------------------------------------------------------------------------- */

function fillZones(current) {
  const select = $("[data-tz-select]");
  const zones = new Set(["UTC", browserTimeZone(), current].filter(Boolean));
  try {
    (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []).forEach((z) => zones.add(z));
  } catch (_) {
    /* the short list will do */
  }
  select.textContent = "";
  [...zones].sort().forEach((z) => select.append(el("option", { value: z, selected: z === current, text: z })));
}

$("[data-profile-form]").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const state = $("[data-profile-state]");
  await busy(form.querySelector("button[type=submit]"), async () => {
    try {
      await api("/api/me", { method: "PATCH", body: { name: form.elements.name.value.trim(), timezone: form.elements.timezone.value } });
      state.textContent = "Saved";
    } catch (err) {
      state.textContent = err.message;
    }
  });
});

/* ---- password ---------------------------------------------------------------------------------- */

// Absent in Google-only mode (SAAS_AUTH_GOOGLE_ONLY): there are no passwords to change.
$("[data-password-form]")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const out = form.querySelector("[data-form-error]");
  out.textContent = "";
  const min = Number(form.elements.new_password.getAttribute("minlength")) || 8;
  if (form.elements.new_password.value.length < min) {
    out.textContent = `Use at least ${min} characters.`;
    return;
  }
  const body = { new_password: form.elements.new_password.value };
  if (account?.has_password) body.current_password = form.elements.current_password.value;
  await busy(form.querySelector("button[type=submit]"), async () => {
    try {
      await api("/api/me/password", { method: "POST", body });
      form.reset();
      toast(account?.has_password ? "Password changed. Other devices are signed out." : "Password set.");
      if (account) account.has_password = true;
      passwordMode();
    } catch (err) {
      out.textContent = err.code === "invalid_credentials" ? "Your current password isn’t right." : err.message;
    }
  });
});

function passwordMode() {
  if (!$("[data-password-form]")) return;
  const has = Boolean(account?.has_password);
  $("[data-current-pw]").hidden = !has;
  $("[data-pw-title]").textContent = has ? "Password" : "Set a password";
}

/* ---- two-factor -------------------------------------------------------------------------------- */

function mfaView(state) {
  ["off", "setup", "codes", "on"].forEach((s) => {
    $(`[data-mfa-${s}]`).hidden = s !== state;
  });
}

function showCodes(codes) {
  const box = $("[data-codes]");
  box.textContent = "";
  codes.forEach((c) => box.append(el("span", { text: c })));
  $("[data-codes-copy]").onclick = async () => {
    const ok = await copyText(codes.join("\n"));
    toast(ok ? "Recovery codes copied." : "Could not copy. Write them down.", ok ? "info" : "error");
  };
  mfaView("codes");
}

$("[data-mfa-start]").addEventListener("click", async (e) => {
  await busy(e.currentTarget, async () => {
    try {
      const res = await api("/api/me/mfa/totp/setup", { method: "POST" });
      const qr = $("[data-mfa-qr]");
      // An inline SVG has no namespace; an image needs it. As an <img> it can never run anything.
      const svg = /xmlns=/.test(res.qr_svg) ? res.qr_svg : res.qr_svg.replace(/^<svg\b/, '<svg xmlns="http://www.w3.org/2000/svg"');
      qr.src = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
      $("[data-mfa-secret]").value = res.secret;
      mfaView("setup");
      $("#mfa-code").focus();
    } catch (err) {
      fail(err);
    }
  });
});

$("[data-mfa-confirm]").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const out = $("[data-mfa-error]");
  out.textContent = "";
  const code = form.elements.code.value.replace(/\s+/g, "");
  if (!/^\d{6}$/.test(code)) {
    out.textContent = "Enter the six digits from the app.";
    return;
  }
  await busy(form.querySelector("button"), async () => {
    try {
      const res = await api("/api/me/mfa/totp/confirm", { method: "POST", body: { code } });
      form.reset();
      if (account) account.mfa_enabled = true;
      showCodes(res.recovery_codes || []);
    } catch (err) {
      out.textContent = err.code === "invalid_code" ? "That code didn’t match. Check the time on your phone and try the next one." : err.message;
    }
  });
});

$("[data-codes-done]").addEventListener("click", () => mfaView(account?.mfa_enabled ? "on" : "off"));

$("[data-mfa-new-codes]").addEventListener("click", async (e) => {
  if (!window.confirm("Make new recovery codes? The old ones stop working.")) return;
  await busy(e.currentTarget, async () => {
    try {
      const res = await api("/api/me/mfa/recovery-codes", { method: "POST" });
      showCodes(res.recovery_codes || []);
    } catch (err) {
      $("[data-mfa-on-error]").textContent = err.message;
    }
  });
});

$("[data-mfa-disable]").addEventListener("click", async (e) => {
  const code = window.prompt("Enter a current code from your authenticator app to turn two-factor off.");
  if (!code) return;
  await busy(e.currentTarget, async () => {
    try {
      await api("/api/me/mfa/totp/disable", { method: "POST", body: { code: code.trim() } });
      if (account) account.mfa_enabled = false;
      mfaView("off");
      toast("Two-factor is off.");
    } catch (err) {
      $("[data-mfa-on-error]").textContent = err.message;
    }
  });
});

/* ---- sessions ---------------------------------------------------------------------------------- */

function device(ua) {
  if (!ua) return "Unknown device";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
  const os = /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return os ? `${browser} on ${os}` : browser;
}

async function loadSessions() {
  const body = $("[data-sessions]");
  try {
    const { sessions } = await api("/api/me/sessions");
    body.textContent = "";
    sessions.forEach((s) => {
      const name = el("td", {}, device(s.user_agent));
      if (s.current) name.append(" ", el("span", { class: "badge badge--ink", text: "this device" }));
      const action = el("td", { class: "actions" });
      if (!s.current) {
        const btn = el("button", { class: "btn btn--ghost btn--sm", type: "button", "data-track": "settings-revoke-session", text: "Sign out" });
        btn.addEventListener("click", () => busy(btn, async () => {
          try {
            await api(`/api/me/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
            btn.closest("tr").remove();
          } catch (err) {
            fail(err);
          }
        }));
        action.append(btn);
      }
      body.append(el("tr", {}, name, el("td", { text: dateTime(s.last_seen_at || s.created_at) }), el("td", { class: "mono", text: s.ip || "" }), action));
    });
  } catch (err) {
    body.textContent = "";
    body.append(el("tr", {}, el("td", { colspan: "4", class: "error-text", text: err.message })));
  }
}

$("[data-revoke-others]").addEventListener("click", async (e) => {
  await busy(e.currentTarget, async () => {
    try {
      const res = await api("/api/me/sessions/revoke-others", { method: "POST" });
      toast(res.revoked ? `Signed out of ${res.revoked} other ${res.revoked === 1 ? "device" : "devices"}.` : "No other devices were signed in.");
      loadSessions();
    } catch (err) {
      fail(err);
    }
  });
});

/* ---- delete ------------------------------------------------------------------------------------- */

$("[data-delete-form]").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const out = $("[data-delete-error]");
  out.textContent = "";
  if (form.elements.confirm.value.trim() !== "DELETE") {
    out.textContent = "Type DELETE in capitals to confirm.";
    return;
  }
  await busy(form.querySelector("button"), async () => {
    try {
      await api("/api/me/delete", { method: "POST", body: { confirm: "DELETE" } });
      location.assign("/?deleted=1");
    } catch (err) {
      out.textContent = err.message;
    }
  });
});

me.then((acct) => {
  account = acct;
  fillZones(acct?.timezone || browserTimeZone());
  passwordMode();
  mfaView(acct?.mfa_enabled ? "on" : "off");
});
loadSessions();
