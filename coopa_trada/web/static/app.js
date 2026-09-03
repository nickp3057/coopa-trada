"use strict";
// Shared helpers + the mode pill that every page shows.

const CT = {
  async get(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    const body = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, body };
  },
  async post(url, data) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data || {}),
    });
    const body = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, body };
  },
  fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString();
  },
  fmtMoney(n) {
    if (n === null || n === undefined) return "—";
    const s = (n < 0 ? "-" : "") + "$" + Math.abs(n).toFixed(2);
    return s;
  },
  el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === "class") e.className = attrs[k];
      else if (k === "text") e.textContent = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    for (const kid of kids) e.append(kid);
    return e;
  },
};

async function refreshModePill() {
  const pill = document.getElementById("mode-pill");
  if (!pill) return;
  const { ok, body } = await CT.get("/api/mode");
  if (!ok) return;
  pill.textContent = "MODE = " + body.mode;
  pill.classList.remove("mode-unknown", "mode-dry", "mode-live");
  pill.classList.add(body.mode === "LIVE" ? "mode-live" : "mode-dry");
}

document.addEventListener("DOMContentLoaded", refreshModePill);
