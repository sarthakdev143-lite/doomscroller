// Email normalisation, validation, alias-collapsing and the disposable-domain blocklist.
// Same rules as saas/security/disposable.py so the Python and edge backends agree.
import DISPOSABLE_TEXT from "./disposable-domains.js";

let disposable = null;
function blocklist() {
  if (!disposable) disposable = new Set(DISPOSABLE_TEXT.split("\n"));
  return disposable;
}

// Never treated as disposable, whatever a list says: a false positive locks real people out.
const NEVER = new Set([
  "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
  "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me", "aol.com", "gmx.com", "gmx.de", "web.de",
  "yandex.com", "yandex.ru", "mail.ru", "zoho.com", "fastmail.com", "hey.com", "tutanota.com", "tuta.io",
  "qq.com", "163.com", "126.com", "rediffmail.com", "naver.com",
]);
const GMAIL = new Set(["gmail.com", "googlemail.com"]);

const LOCAL_RE = /^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$/;
const LABEL_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/** Trimmed, lower-cased address if it is plausibly deliverable, else null. */
export function normalise(raw) {
  if (typeof raw !== "string") return null;
  const email = raw.trim().toLowerCase();
  if (email.length < 6 || email.length > 254) return null;
  const at = email.lastIndexOf("@");
  if (at < 1) return null;
  const local = email.slice(0, at);
  const domain = email.slice(at + 1);
  if (local.length > 64 || !LOCAL_RE.test(local)) return null;
  const labels = domain.split(".");
  if (labels.length < 2 || !labels.every((l) => LABEL_RE.test(l))) return null;
  const tld = labels[labels.length - 1];
  if (!/^[a-z]{2,24}$/.test(tld) && !/^xn--[a-z0-9-]{2,59}$/.test(tld)) return null;
  return email;
}

export function domainOf(email) {
  return email.slice(email.lastIndexOf("@") + 1);
}

/** Disposable inbox? Checks the domain and every parent (x.mailinator.com -> mailinator.com). */
export function isDisposable(email, extraCsv = "", allowCsv = "") {
  const domain = domainOf(email);
  const allow = new Set(allowCsv.split(",").map((d) => d.trim().toLowerCase()).filter(Boolean));
  const extra = new Set(extraCsv.split(",").map((d) => d.trim().toLowerCase()).filter(Boolean));
  const parts = domain.split(".");
  for (let i = 0; i < parts.length - 1; i++) {
    const candidate = parts.slice(i).join(".");
    if (NEVER.has(candidate) || allow.has(candidate)) return false;
    if (blocklist().has(candidate) || extra.has(candidate)) return true;
  }
  return false;
}

/** j.o.e+promo@googlemail.com -> joe@gmail.com; joe+x@anything -> joe@anything. */
export function canonical(email) {
  const at = email.lastIndexOf("@");
  let local = email.slice(0, at);
  let domain = email.slice(at + 1);
  if (GMAIL.has(domain)) {
    local = local.split("+")[0].replaceAll(".", "");
    domain = "gmail.com";
  } else {
    local = local.split("+")[0];
  }
  return `${local}@${domain}`;
}
