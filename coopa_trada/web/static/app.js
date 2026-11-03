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

// -- status tiles (dashboard) --------------------------------------------------
// CT.tile("tile-run", { value, sub, cls: "ok" | "warn" | "bad" | "info" })
CT.tile = function (id, { value, sub = "", cls = "" }) {
  const tile = document.getElementById(id);
  if (!tile) return;
  tile.className = "tile " + cls;
  tile.querySelector(".tile-value").textContent = value;
  tile.querySelector(".tile-sub").textContent = sub;
};

// -- hover tips ---------------------------------------------------------------------
// Any element with data-tip="..." shows it on hover, on keyboard focus, and on tap
// (phones). <span class="tip" data-tip="..."></span> draws the small "i".
(function tooltips() {
  let box = null;
  let owner = null;
  function ensure() {
    if (!box) {
      box = document.createElement("div");
      box.id = "tooltip";
      box.setAttribute("role", "tooltip");
      document.body.append(box);
    }
    return box;
  }
  function show(el) {
    const text = el.getAttribute("data-tip");
    if (!text) return;
    owner = el;
    const b = ensure();
    b.textContent = text;
    b.classList.add("show");
    const r = el.getBoundingClientRect();
    const w = b.offsetWidth, h = b.offsetHeight;
    let left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    let top = r.top - h - 8;
    if (top < 8) top = r.bottom + 8;
    b.style.left = left + "px";
    b.style.top = top + "px";
  }
  function hide() { owner = null; if (box) box.classList.remove("show"); }
  const find = (t) => (t instanceof Element ? t.closest("[data-tip]") : null);
  document.addEventListener("mouseover", (e) => { const el = find(e.target); if (el) show(el); });
  document.addEventListener("mouseout", (e) => { if (find(e.target) && !find(e.relatedTarget)) hide(); });
  document.addEventListener("focusin", (e) => { const el = find(e.target); if (el) show(el); });
  document.addEventListener("focusout", hide);
  document.addEventListener("click", (e) => {
    const el = find(e.target);
    if (el && el.classList.contains("tip")) { e.preventDefault(); owner === el ? hide() : show(el); }
    else if (!el) hide();
  });
  window.addEventListener("scroll", hide, { passive: true });
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".tip").forEach((t) => {
      t.textContent = "i";
      t.tabIndex = 0;
      t.setAttribute("aria-label", t.getAttribute("data-tip"));
    });
  });
})();

// -- light / dark ---------------------------------------------------------------------
function setupTheme() {
  const btn = document.getElementById("theme-btn");
  if (!btn) return;
  const label = () => {
    const t = document.documentElement.dataset.theme;
    btn.textContent = t === "dark" ? "Dark" : t === "light" ? "Light" : "Auto";
  };
  label();
  btn.addEventListener("click", () => {
    const order = ["", "light", "dark"];
    const now = document.documentElement.dataset.theme || "";
    const next = order[(order.indexOf(now) + 1) % order.length];
    if (next) document.documentElement.dataset.theme = next;
    else delete document.documentElement.dataset.theme;
    try { next ? localStorage.setItem("theme", next) : localStorage.removeItem("theme"); } catch (e) { /* private mode */ }
    label();
  });
}

document.addEventListener("DOMContentLoaded", setupTheme);
document.addEventListener("DOMContentLoaded", refreshModePill);
document.addEventListener("DOMContentLoaded", setupQuit);
