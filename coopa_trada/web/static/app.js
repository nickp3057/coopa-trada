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
  // Every time on these pages is shown in Mountain time (Cooper is in Utah).
  fmtTime(iso) {
    if (!iso) return "—";
    const d = iso instanceof Date ? iso : new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString("en-US", {
      timeZone: "America/Denver", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
    }) + " MT";
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
  // LIVE while a live run is going, or (no run) when the next run will trade real money.
  pill.textContent = body.mode !== "LIVE" ? "PRACTICE" : (body.running ? "LIVE RUN" : "REAL MONEY ON");
  pill.title = body.mode !== "LIVE" ? "Runs are practice: nothing is sent to Schwab"
    : "Runs place real orders (turn off on the Dashboard)";
  pill.classList.remove("mode-unknown", "mode-dry", "mode-live");
  pill.classList.add(body.mode === "LIVE" ? "mode-live" : "mode-dry");
}

// The packaged app has no window of its own: quitting happens here.
async function setupQuit() {
  const footer = document.getElementById("app-footer");
  if (!footer) return;
  const { ok, body } = await CT.get("/api/app");
  if (!ok || !body.can_quit) return;
  footer.hidden = false;
  document.getElementById("btn-quit").addEventListener("click", async () => {
    const res = await CT.post("/api/app/quit", {});
    const msg = document.getElementById("quit-msg");
    msg.textContent = res.body.message || res.body.error || "";
    msg.className = "msg " + (res.ok ? "ok" : "is-error");
  });
}

document.addEventListener("DOMContentLoaded", refreshModePill);
document.addEventListener("DOMContentLoaded", setupQuit);
