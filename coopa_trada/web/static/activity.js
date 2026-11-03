"use strict";
// Activity page: open positions with their trigger prices, and the bot's full daily log.

const $ = (id) => document.getElementById(id);
let today = null;
let shown = 0;     // lines already on screen (for live tailing)
let timer = null;
const WATCH = { box: "watch", updated: "watch-updated" };
const loadWatch = () => CT.loadWatch(WATCH);

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
