"use strict";
// Activity page: open positions with their trigger prices, and the bot's full daily log.

const $ = (id) => document.getElementById(id);
const price = (n) => (n == null ? "—" : "$" + Number(n).toFixed(2));
const NAMES = {
  take_profit: "Take profit", ten_point: "10-point rule", hard_stop: "Hard stop",
  trailing_stop: "Trailing stop", drop_from_peak: "10% from the high", drop_from_entry: "Drop from entry",
};
let today = null;
let shown = 0;     // lines already on screen (for live tailing)
let timer = null;

async function loadWatch() {
  const { ok, body } = await CT.get("/api/watch");
  const box = $("watch");
  box.innerHTML = "";
  $("watch-updated").textContent = "Updated " + new Date().toLocaleTimeString("en-US", { timeZone: "America/Denver" }) + " MT";
  if (!ok) { box.append(CT.el("p", { class: "error", text: body.error || "Could not load" })); return; }
  if (!body.positions.length) { box.append(CT.el("p", { class: "hint", text: "Nothing is open." })); return; }
  for (const p of body.positions) {
    const card = CT.el("div", { class: "pick-card" });
    card.append(CT.el("div", { class: "pick-head" },
      CT.el("strong", { text: p.osi }),
      CT.el("span", { class: "pick-state held", text: `${p.mode === "LIVE" ? "REAL" : "practice"} · x${p.qty}` })));
    const mid = p.mid;
    const facts = CT.el("div", { class: "pick-facts" });
    const add = (k, v) => facts.append(CT.el("span", {}, CT.el("span", { class: "hint", text: k + " " }), CT.el("b", { text: v })));
    add("bought", CT.fmtTime(p.bought_at));
    add("paid", price(p.paid));
    add("rules measure from", price(p.ref));
    add("high", price(p.peak));
    add("latest mid", mid == null ? "no price yet" : `${price(mid)} (${CT.fmtTime(p.quoted_at)})`);
    if (mid != null) add("gain", ((mid / p.ref - 1) * 100).toFixed(1) + "%");
    if (p.sell_by) add("next-day sell by", CT.fmtTime(p.sell_by));
    card.append(facts);
    const list = CT.el("ul", { class: "trigger-list" });
    const stops = Object.entries(p.triggers).filter(([k, v]) => v != null && k !== "take_profit");
    const nearest = stops.length ? stops.reduce((a, b) => (a[1] > b[1] ? a : b))[0] : null;
    for (const [k, v] of Object.entries(p.triggers)) {
      if (v == null) continue;
      const isTp = k === "take_profit";
      const d = mid == null ? null : isTp ? v / mid - 1 : mid / v - 1;
      const n = d == null ? "" : Math.abs(d * 100).toFixed(1);
      const dist = d == null ? "" : isTp ? (d > 0 ? `${n}% to go` : "reached")
        : (d >= 0 ? `${n}% above it` : `price is ${n}% below it`);
      list.append(CT.el("li", { class: k === nearest ? "nearest" : "" },
        CT.el("span", { text: NAMES[k] || k }), CT.el("b", { text: price(v) }), CT.el("span", { class: "hint", text: dist })));
    }
    card.append(list);
    box.append(card);
  }
}

async function loadDays() {
  const { ok, body } = await CT.get("/api/activity/days");
  const sel = $("act-day");
  sel.innerHTML = "";
  if (!ok) return;
  today = body.today;
  const days = body.days.includes(today) ? body.days : [today, ...body.days];
  for (const d of days) sel.append(CT.el("option", { value: d, text: d === today ? `${d} (today)` : d }));
  sel.value = today;
}

function params(after) {
  const q = new URLSearchParams({ day: $("act-day").value, level: $("act-level").value, q: $("act-q").value, after: String(after) });
  return q.toString();
}

function lineEl(l) {
  const row = CT.el("div", { class: `ev lvl-${l.level}` });
  row.append(CT.el("span", { class: "ev-time", text: l.at }),
    CT.el("span", { class: "ev-kind", text: l.source.replace(/^coopa_trada\.?/, "") || "app" }),
    CT.el("span", { class: "ev-text", text: l.message }));
  return row;
}

async function loadLines(reset) {
  const box = $("act-lines");
  if (reset) { shown = 0; box.innerHTML = ""; }
  const { ok, body } = await CT.get("/api/activity?" + params(shown));
  if (!ok) { box.append(CT.el("p", { class: "error", text: body.error || "Could not load" })); return; }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  for (const l of body.lines) box.append(lineEl(l));
  shown = body.total;
  if (reset && !body.lines.length) box.append(CT.el("p", { class: "hint", text: "No lines for this day (or none match)." }));
  $("act-count").textContent = `${box.querySelectorAll(".ev").length} line(s) shown of ${body.total} on ${body.day}`;
  $("act-download").href = "/api/activity/download?day=" + encodeURIComponent(body.day);
  if (reset || atBottom) box.scrollTop = box.scrollHeight;
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => {
    loadWatch();
    if ($("act-live").checked && $("act-day").value === today) loadLines(false);
  }, 10000);
}

document.addEventListener("DOMContentLoaded", async () => {
  loadWatch();
  await loadDays();
  await loadLines(true);
  schedule();
  for (const id of ["act-day", "act-level"]) $(id).addEventListener("change", () => loadLines(true));
  let typing;
  $("act-q").addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => loadLines(true), 300); });
});
