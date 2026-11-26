// /app/developers: list, create (shown once) and revoke API keys.
import { api, busy, dateTime, el, fail } from "./api.js";
import "./app-shell.js";

const $ = (sel) => document.querySelector(sel);

async function loadKeys() {
  const body = $("[data-keys]");
  try {
    const { keys } = await api("/api/keys");
    body.textContent = "";
    const live = keys.filter((k) => !k.revoked);
    if (!live.length) {
      body.append(el("tr", {}, el("td", { colspan: "5", class: "muted", text: "No keys yet." })));
      return;
    }
    live.forEach((k) => {
      const revoke = el("button", { class: "btn btn--ghost btn--sm", type: "button", "data-track": "dev-key-revoke", text: "Revoke" });
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`Revoke “${k.name}”? Anything using it stops working at once.`)) return;
        await busy(revoke, async () => {
          try {
            await api(`/api/keys/${encodeURIComponent(k.id)}`, { method: "DELETE" });
            loadKeys();
          } catch (err) {
            fail(err);
          }
        });
      });
      body.append(el("tr", {},
        el("td", { text: k.name }),
        el("td", { class: "mono", text: `${k.prefix}…` }),
        el("td", { class: "mono small", text: (k.scopes || []).join(" ") }),
        el("td", { text: k.last_used_at ? dateTime(k.last_used_at) : "Never" }),
        el("td", { class: "actions" }, revoke)));
    });
  } catch (err) {
    body.textContent = "";
    body.append(el("tr", {}, el("td", { colspan: "5", class: "error-text", text: err.message })));
  }
}

$("[data-key-form]").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const out = $("[data-key-error]");
  out.hidden = true;
  const name = form.elements.name.value.trim() || "API key";
  const scopes = [...form.querySelectorAll('[name="scopes"]:checked')].map((c) => c.value);
  if (!scopes.length) {
    out.textContent = "Give the key at least one scope.";
    out.hidden = false;
    return;
  }
  await busy(form.querySelector("button[type=submit]"), async () => {
    try {
      const res = await api("/api/keys", { method: "POST", body: { name, scopes } });
      $("[data-key-value]").value = res.key;
      $("[data-key-new]").hidden = false;
      $("[data-key-value]").select();
      form.reset();
      form.querySelectorAll('[name="scopes"]').forEach((c) => {
        c.checked = true;
      });
      loadKeys();
    } catch (err) {
      out.textContent = "";
      if (err.code === "plan_required") {
        out.append(`${err.message} `, el("a", { href: "/app/billing", "data-track": "dev-upgrade", text: "See plans" }));
      } else {
        out.textContent = err.message;
      }
      out.hidden = false;
    }
  });
});

loadKeys();
