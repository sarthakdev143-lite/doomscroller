// The signed-in app's one way to talk to the server. Every state-changing call carries the CSRF token,
// errors come back as ApiError {status, code, message}, a 401 sends the visitor to sign in, and a 403
// reauth_required asks for the password (or a 2FA code) once and retries.
import { csrfHeaders, toast } from "./site.js";

export class ApiError extends Error {
  constructor(status, code, message, data) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

const FALLBACK = {
  0: "We couldn’t reach the server. Check your connection and try again.",
  400: "That request wasn’t accepted. Check the form and try again.",
  401: "Your session ended. Sign in again.",
  402: "You don’t have enough minutes for this.",
  403: "You can’t do that from this account.",
  404: "That isn’t here any more.",
  409: "That changed while you were looking. Refresh and try again.",
  413: "That file is too large.",
  415: "That file type isn’t supported.",
  429: "Too many tries. Wait a minute and try again.",
};

export function messageFor(status) {
  return FALLBACK[status] || (status >= 500 ? "Something broke on our side. Try again in a minute." : FALLBACK[400]);
}

async function errorFrom(res) {
  const data = await res.json().catch(() => null);
  const code = (data && data.error) || `http_${res.status}`;
  let message = data && typeof data.message === "string" && data.message ? data.message : messageFor(res.status);
  if (data && Array.isArray(data.fields) && data.fields.length && code === "invalid_request") {
    message = `${message} (${data.fields.map((f) => f.field || f).join(", ")})`;
  }
  const retry = Number(res.headers.get("Retry-After"));
  if (res.status === 429 && retry > 0) message = `Too many tries. Wait ${Math.ceil(retry)} seconds and try again.`;
  return new ApiError(res.status, code, message, data);
}

function toLogin() {
  const next = encodeURIComponent(location.pathname + location.search);
  location.assign(`/login?next=${next}`);
}

export async function api(path, { method = "GET", body, form, signal, reauth = true } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET" && method !== "HEAD") Object.assign(headers, csrfHeaders());
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: "same-origin", signal });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(0, "network", messageFor(0));
  }
  if (res.ok) {
    if (res.status === 204) return {};
    return (await res.json().catch(() => ({}))) || {};
  }
  const error = await errorFrom(res);
  if (res.status === 401 && !path.startsWith("/auth/")) {
    toLogin();
  } else if (res.status === 403 && error.code === "reauth_required" && reauth) {
    if (await confirmIdentity()) return api(path, { method, body, form, signal, reauth: false });
  }
  throw error;
}

/** Multipart upload with progress (fetch cannot report upload progress). */
export function upload(path, file, { field = "file", onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("Accept", "application/json");
    Object.entries(csrfHeaders()).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total, e.loaded, e.total);
    });
    xhr.addEventListener("load", () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText || "null");
      } catch (_) {
        data = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data || {});
        return;
      }
      if (xhr.status === 401) toLogin();
      const code = (data && data.error) || `http_${xhr.status}`;
      const message = (data && data.message) || messageFor(xhr.status);
      reject(new ApiError(xhr.status, code, message, data));
    });
    xhr.addEventListener("error", () => reject(new ApiError(0, "network", messageFor(0))));
    xhr.addEventListener("abort", () => reject(new ApiError(0, "aborted", "Upload cancelled.")));
    if (signal) signal.addEventListener("abort", () => xhr.abort());
    const fd = new FormData();
    fd.append(field, file);
    xhr.send(fd);
  });
}

/* ---- re-authentication ------------------------------------------------------------------------ */

let identityPromise = null;

/** Ask for the password (or a 2FA code) in the page's reauth dialog, post /auth/reauth, resolve true on success. */
export function confirmIdentity() {
  if (identityPromise) return identityPromise;
  const dialog = document.querySelector("[data-reauth]");
  if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(false);
  identityPromise = new Promise((resolve) => {
    const form = dialog.querySelector("form");
    const error = dialog.querySelector("[data-form-error]");
    const finish = (ok) => {
      dialog.close();
      form.removeEventListener("submit", onSubmit);
      dialog.removeEventListener("close", onClose);
      identityPromise = null;
      resolve(ok);
    };
    const onClose = () => finish(false);
    const onSubmit = async (e) => {
      e.preventDefault();
      error.textContent = "";
      const password = form.elements.password.value;
      const code = form.elements.code.value.trim();
      if (!password && !code) {
        error.textContent = "Enter your password, or a code from your authenticator app.";
        return;
      }
      try {
        await api("/auth/reauth", { method: "POST", body: password ? { password } : { code }, reauth: false });
        form.reset();
        finish(true);
      } catch (err) {
        error.textContent = err.message;
      }
    };
    form.addEventListener("submit", onSubmit);
    dialog.addEventListener("close", onClose, { once: true });
    error.textContent = "";
    dialog.showModal();
    form.elements.password.focus();
  });
  return identityPromise;
}

/* ---- formatting ------------------------------------------------------------------------------- */

export function clock(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

export function minutes(n) {
  const v = Number(n) || 0;
  return `${v.toLocaleString()} min`;
}

export function plural(n, one, many) {
  return `${Number(n).toLocaleString()} ${Number(n) === 1 ? one : many}`;
}

export function ago(iso) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const s = Math.round((Date.now() - then) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return new Date(then).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function dateTime(iso) {
  const t = Date.parse(iso);
  return Number.isFinite(t)
    ? new Date(t).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : "";
}

export const STATUS_LABEL = {
  queued: "Queued",
  fetching: "Fetching",
  transcribing: "Transcribing",
  selecting: "Choosing moments",
  rendering: "Rendering",
  done: "Done",
  failed: "Failed",
  canceled: "Canceled",
};

export function statusBadge(status) {
  const el = document.createElement("span");
  const live = !["done", "failed", "canceled"].includes(status);
  el.className = `badge ${status === "done" ? "badge--ok" : status === "failed" ? "badge--danger" : live ? "badge--live" : ""}`;
  el.textContent = STATUS_LABEL[status] || status;
  return el;
}

/** Tiny element builder: el("a", {href, class: "x"}, "text", child). Text is always set as text. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function fail(err, target) {
  const message = err && err.message ? err.message : messageFor(500);
  if (target) {
    target.textContent = message;
  } else {
    toast(message, "error", 5000);
  }
}

/** Set a button busy while an async action runs. */
export async function busy(button, action) {
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  try {
    return await action();
  } finally {
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

export function browserTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch (_) {
    return "UTC";
  }
}

export function safeNext(raw, fallback = "/app") {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return fallback;
  return raw;
}
