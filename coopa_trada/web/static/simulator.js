"use strict";
// Simulator page: set up a run, start it, then play back what the bot did.

const $ = (id) => document.getElementById(id);
const MT_TZ = "America/Denver";
const RULE_NAMES = {
  take_profit: ["Take profit", "TakeProfit"],
  ten_point: ["10-point rule", "ProfitPointTrail"],
  hard_stop: ["Hard stop", "StopLoss"],
  trailing_stop: ["Trailing stop", "TrailingPeak"],
  drop_from_peak: ["10% from the high", "TrailingPeak"],
  drop_from_entry: ["Drop from entry", "StopLoss"],
};
const STOP_KEYS = ["hard_stop", "trailing_stop", "ten_point", "drop_from_peak", "drop_from_entry"];

let lists = [];
let shapes = {};
let R = null;          // the loaded result
let frameT = [];       // frame times (ms)
let idx = 0;           // current frame
let simNow = 0;        // current simulated time (ms)
let playing = false;
let lastTick = 0;
let seriesIdx = {};    // osi -> [[frameIndex, bid, ask, mid]]
let eventT = [];

// -- formatting ------------------------------------------------------------
function fmtClock(ms) {
  return new Date(ms).toLocaleString("en-US", {
    timeZone: MT_TZ, weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit",
  }) + " MT";
}
function fmtShort(ms) {
  return new Date(ms).toLocaleString("en-US", { timeZone: MT_TZ, weekday: "short", hour: "numeric", minute: "2-digit" });
}
const money = (n) => (n == null ? "—" : CT.fmtMoney(n));
const price = (n) => (n == null ? "—" : "$" + Number(n).toFixed(2));
const pct = (n) => (n == null ? "" : (n >= 0 ? "+" : "") + (n * 100).toFixed(1) + "%");
const mtDay = (ms) => new Date(ms).toLocaleDateString("en-CA", { timeZone: MT_TZ });

// -- setup form -------------------------------------------------------------------
async function loadLists() {
  const { ok, body } = await CT.get("/api/sim/lists");
  const sel = $("sim-list");
  sel.innerHTML = "";
  if (!ok) { sel.append(CT.el("option", { text: "could not load lists" })); return; }
  lists = body.lists;
  shapes = body.shapes;
  const groups = {};
  for (const l of lists) (groups[l.group] = groups[l.group] || []).push(l);
  sel.append(CT.el("option", { value: "", text: "— choose a list, or paste one below —" }));
  for (const [g, items] of Object.entries(groups)) {
    const og = CT.el("optgroup", { label: g });
    for (const l of items) og.append(CT.el("option", { value: l.id, text: `${l.name} (${l.picks} picks)` }));
    sel.append(og);
  }
  const shapeSel = $("sim-shape");
  shapeSel.innerHTML = "";
  for (const [k, v] of Object.entries(shapes)) shapeSel.append(CT.el("option", { value: k, text: `${k.replace("_", " ")}: ${v}` }));
  $("recorded-days").textContent = body.recorded_days.length
    ? "Days with recordings: " + body.recorded_days.slice(0, 8).join(", ") + (body.recorded_days.length > 8 ? "…" : "")
    : "None yet: the bot saves them from now on, whenever it watches a position with real Schwab prices.";
  const firstReal = lists.find((l) => l.group === "Confirmed lists") || lists[0];
  if (firstReal && !$("sim-csv").value) { sel.value = firstReal.id; await chooseList(); }
}

async function chooseList() {
  const id = $("sim-list").value;
  if (!id) return;
  const { ok, body } = await CT.get("/api/sim/list/" + encodeURIComponent(id));
  if (!ok) return;
  $("sim-csv").value = body.csv;
  $("sim-list-info").textContent = body.trade_date ? `List for ${body.trade_date}` : "";
  if (body.trade_date) $("sim-buy-day").value = body.trade_date;
  renderPerPick();
}

function listLabels() {
  const out = [];
  let header = false;
  for (const line of $("sim-csv").value.split(/\r?\n/)) {
    const cells = line.split(",").map((c) => c.trim());
    if (cells.map((c) => c.toUpperCase()).includes("SYMBOL")) { header = true; continue; }
    if (header && /^[A-Za-z]+$/.test(cells[0] || "") && cells[1]) out.push(`${cells[0].toUpperCase()} ${cells[1].toUpperCase()}`);
  }
  return out;
}

function renderPerPick() {
  const box = $("per-pick");
  const keep = {};
  box.querySelectorAll("select").forEach((s) => { if (s.value) keep[s.dataset.label] = s.value; });
  box.innerHTML = "";
  for (const label of listLabels()) {
    const s = CT.el("select", { "data-label": label });
    s.append(CT.el("option", { value: "", text: "(same as every pick)" }));
    for (const k of Object.keys(shapes)) if (k !== "mixed") s.append(CT.el("option", { value: k, text: k.replace("_", " ") }));
    s.value = keep[label] || "";
    box.append(CT.el("label", { class: "inline" }, label + " ", s));
  }
}

function pricesChoice() { return document.querySelector('input[name="sim-prices"]:checked').value; }

function requestBody() {
  const perPick = {};
  $("per-pick").querySelectorAll("select").forEach((s) => { if (s.value) perPick[s.dataset.label] = s.value; });
  return {
    list_csv: $("sim-csv").value,
    buy_day: $("sim-buy-day").value,
    confirm_at: $("sim-confirm").value,
    start_at: $("sim-start").value,
    end_day: $("sim-end-day").value || null,
    end_at: $("sim-end").value,
    prices: pricesChoice(),
    shape: $("sim-shape").value || "mixed",
    shapes: perPick,
    seed: Number($("sim-seed").value || 1),
    spread_pct: Number($("sim-spread").value || 0) / 100,
    first_sell_unfilled: $("sim-stuck").checked,
  };
}

function fillForm(req) {
  $("sim-csv").value = req.list_csv || "";
  $("sim-buy-day").value = req.buy_day || "";
  $("sim-confirm").value = req.confirm_at || "13:40";
  $("sim-start").value = req.start_at || "13:00";
  $("sim-end-day").value = req.end_day || "";
  $("sim-end").value = req.end_at || "10:00";
  const radio = document.querySelector(`input[name="sim-prices"][value="${req.prices || "scenario"}"]`);
  if (radio) radio.checked = true;
  $("sim-shape").value = req.shape || "mixed";
  $("sim-seed").value = req.seed ?? 1;
  $("sim-spread").value = ((req.spread_pct ?? 0.06) * 100).toFixed(1).replace(/\.0$/, "");
  $("sim-stuck").checked = !!req.first_sell_unfilled;
  renderPerPick();
  $("per-pick").querySelectorAll("select").forEach((s) => { s.value = (req.shapes || {})[s.dataset.label] || ""; });
  togglePriceOpts();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function togglePriceOpts() { $("scenario-opts").hidden = pricesChoice() !== "scenario"; }

function setStatus(text, cls) {
  const el = $("sim-status");
  el.textContent = text;
  el.className = "status-line " + (cls || "");
}

async function startRun() {
  const btn = $("btn-play");
  btn.disabled = true;
  setStatus("Starting…", "is-running");
  const res = await CT.post("/api/sim/run", requestBody());
  if (!res.ok) {
    btn.disabled = false;
    setStatus(res.body.error || "Could not start", "is-error");
    if (res.status === 409 && res.body.id) poll(res.body.id);
    return;
  }
  poll(res.body.id);
}

async function poll(id) {
  const btn = $("btn-play");
  btn.disabled = true;
  for (;;) {
    const { ok, body } = await CT.get(`/api/sim/${id}/status`);
    if (!ok) { setStatus(body.error || "Lost track of the simulation", "is-error"); break; }
    if (body.state === "done") {
      setStatus(`Done: ${body.outcome}.`, "ok");
      await openResult(id);
      break;
    }
    if (body.state === "error") {
      setStatus(body.error + (body.detail ? "\n" + body.detail : ""), "is-error");
      break;
    }
    setStatus(body.at ? `Simulating… now at ${fmtClock(Date.parse(body.at))}` : "Simulating…", "is-running");
    await new Promise((r) => setTimeout(r, 600));
  }
  btn.disabled = false;
  loadHistory();
}

// -- playback ---------------------------------------------------------------------
async function openResult(id) {
  const r = await fetch(`/api/sim/${id}/result`);
  if (!r.ok) { setStatus("No result for that run", "is-error"); return; }
  R = await r.json();
  R.id = id;
  frameT = R.frames.map((f) => Date.parse(f.t));
  const indexOf = new Map(R.frames.map((f, i) => [f.t, i]));
  seriesIdx = {};
  for (const [osi, pts] of Object.entries(R.series)) {
    seriesIdx[osi] = pts.filter((p) => indexOf.has(p[0])).map((p) => [indexOf.get(p[0]), p[1], p[2], p[3]]);
  }
  eventT = R.events.map((e) => Date.parse(e.t));
  for (const t of R.trades) {
    t.entryMs = t.entry_at ? Date.parse(t.entry_at) : null;
    t.exitMs = t.exit_at ? Date.parse(t.exit_at) : null;
  }
  $("playback").hidden = false;
  $("dl-log").href = `/api/sim/${id}/log`;
  $("dl-json").href = `/api/sim/${id}/result`;
  const req = R.request;
  $("pb-title").textContent = `Playback: list for ${req.buy_day}, ${R.picks.length} picks, ` +
    { scenario: "made-up prices", stock: "real stock prices", recorded: "recorded quotes" }[req.prices];
  const notes = R.events.filter((e) => e.kind === "note");
  $("pb-notes").innerHTML = "";
  for (const n of notes) $("pb-notes").append(CT.el("p", { class: "hint", text: n.text }));
  const r0 = R.rules;
  $("pb-notes").append(CT.el("p", { class: "hint", text:
    `Settings used: take profit +${(r0.take_profit_pct * 100).toFixed(0)}%, hard stop -${(r0.hard_stop_loss_pct * 100).toFixed(0)}%, ` +
    `trailing ${(r0.trailing_stop_pct * 100).toFixed(0)}%, 10-point rule ${r0.profit_trail_points ?? "off"} (${r0.profit_trail_arm}), ` +
    `stops on the buy day ${r0.stops_on_entry_day ? "yes" : "no"}, next day sell by ${r0.next_day_time_exit ? r0.next_day_sell_by + " MT" : "off"}, ` +
    `budget $${r0.total_budget}.` }));
  const scrub = $("pb-scrub");
  scrub.max = Math.max(0, R.frames.length - 1);
  renderTrades();
  renderUnbought();
  pause();
  seek(0, true);
  $("playback").scrollIntoView({ behavior: "smooth" });
}

function frameAt(ms) {
  let lo = 0, hi = frameT.length - 1;
  if (ms <= frameT[0]) return 0;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (frameT[mid] <= ms) lo = mid; else hi = mid - 1;
  }
  return lo;
}

function seek(i, setTime) {
  idx = Math.max(0, Math.min(i, frameT.length - 1));
  if (setTime) simNow = frameT[idx];
  $("pb-scrub").value = idx;
  render();
}

function play() {
  if (!R) return;
  if (idx >= frameT.length - 1) seek(0, true);
  playing = true;
  $("pb-toggle").innerHTML = "&#10074;&#10074;";
  lastTick = performance.now();
  requestAnimationFrame(step);
}
function pause() {
  playing = false;
  $("pb-toggle").innerHTML = "&#9654;";
}

function nextEventAfter(ms) {
  for (let i = 0; i < eventT.length; i++) if (eventT[i] > ms && R.events[i].kind !== "log") return eventT[i];
  return null;
}

function step(now) {
  if (!playing) return;
  const dt = Math.min(now - lastTick, 250);
  lastTick = now;
  simNow += dt * Number($("pb-speed").value);
  const next = frameT[idx + 1];
  // Overnight (no frames): jump straight to the next morning.
  if (next != null && next - frameT[idx] > 10 * 60000 && simNow > frameT[idx] + 60000) simNow = Math.max(simNow, next);
  if ($("pb-skip").checked && !R.frames[idx].positions.length) {
    const ev = nextEventAfter(simNow);
    if (ev && ev - simNow > 60000) simNow = ev - 30000;
  }
  const i = frameAt(simNow);
  if (i !== idx) seek(i, false);
  if (idx >= frameT.length - 1) { pause(); return; }
  requestAnimationFrame(step);
}

function tradeFor(osi) { return R.trades.find((t) => t.osi === osi); }

function render() {
  const t = frameT[idx];
  const frame = R.frames[idx];
  $("pb-time").textContent = fmtClock(t);
  $("pb-note").textContent = "· " + (frame.status || "");
  // Summary so far
  const bought = R.trades.filter((x) => x.entryMs != null && x.entryMs <= t);
  const sold = bought.filter((x) => x.exitMs != null && x.exitMs <= t);
  const pnl = sold.reduce((a, x) => a + (x.pnl || 0), 0);
  const held = frame.positions;
  let unreal = 0;
  for (const p of held) if (p.bid != null) unreal += (p.bid - (tradeFor(p.osi)?.entry_price ?? p.ref)) * 100 * p.qty;
  const s = $("pb-summary");
  s.innerHTML = "";
  for (const [label, value, cls] of [
    ["Bought", bought.length], ["Sold", sold.length], ["Holding", held.length],
    ["Realized P&L", money(pnl), pnl >= 0 ? "pnl-pos" : "pnl-neg"],
    ["Open P&L (at the bid)", held.length ? money(unreal) : "—", unreal >= 0 ? "pnl-pos" : "pnl-neg"],
  ]) s.append(CT.el("div", { class: "stat" }, CT.el("div", { class: "stat-label", text: label }),
    CT.el("div", { class: "stat-value " + (cls || ""), text: String(value) })));
  renderPicks(t, frame);
  renderEvents(t);
}

function lastPoint(osi) {
  const pts = seriesIdx[osi] || [];
  let best = null;
  for (const p of pts) { if (p[0] > idx) break; best = p; }
  return best;
}

function renderPicks(t, frame) {
  const box = $("pb-picks");
  box.innerHTML = "";
  const boughtOsi = new Set(R.trades.map((x) => x.osi));
  const picks = R.picks.filter((p) => boughtOsi.has(p.osi));
  if (!picks.length) { box.append(CT.el("p", { class: "hint", text: "Nothing was bought in this run (see Not bought below)." })); return; }
  for (const pick of picks) {
    const tr = tradeFor(pick.osi);
    const pos = frame.positions.find((p) => p.osi === pick.osi);
    const pt = lastPoint(pick.osi);
    let state, stateCls;
    if (tr.entryMs == null || tr.entryMs > t) { state = "not bought yet"; stateCls = "muted"; }
    else if (tr.exitMs != null && tr.exitMs <= t) {
      state = `sold ${price(tr.exit_price)} · ${tr.why || ""} · ${money(tr.pnl)}`;
      stateCls = (tr.pnl || 0) >= 0 ? "pnl-pos" : "pnl-neg";
    } else { state = pos ? `holding (${pos.plan === "entry_day" ? "buy day" : pos.plan === "held" ? "next day" : pos.plan})` : "holding"; stateCls = "held"; }
    const card = CT.el("div", { class: "pick-card" });
    card.append(CT.el("div", { class: "pick-head" },
      CT.el("strong", { text: pick.name }),
      CT.el("span", { class: "pick-state " + stateCls, text: state })));
    card.append(CT.el("div", { class: "hint", text: pick.shape || "" }));
    card.append(chart(pick, tr));
    const facts = CT.el("div", { class: "pick-facts" });
    const mid = pt ? pt[3] : null;
    const add = (k, v) => facts.append(CT.el("span", {}, CT.el("span", { class: "hint", text: k + " " }), CT.el("b", { text: v })));
    add("now", pt ? `${price(pt[1])} bid / ${price(pt[2])} ask` : "—");
    add("paid", price(tr.entry_price));
    if (pos) {
      add("rules measure from", price(pos.ref));
      add("high", price(pos.peak));
      add("gain", pct(mid != null ? mid / pos.ref - 1 : null));
    }
    card.append(facts);
    if (pos) {
      const list = CT.el("ul", { class: "trigger-list" });
      const active = new Set(pos.rules);
      const rows = Object.entries(pos.triggers).filter(([, v]) => v != null).map(([k, v]) => {
        const [name, cls] = RULE_NAMES[k] || [k, ""];
        const on = active.has(cls);
        const isTp = k === "take_profit";
        const dist = mid != null ? (isTp ? v / mid - 1 : mid / v - 1) : null;
        return { k, name, v, on, dist, isTp };
      });
      // The stop nearest the price is the one that would fire first.
      const stops = rows.filter((r) => !r.isTp && r.on);
      const nearest = stops.length ? stops.reduce((a, b) => (a.v > b.v ? a : b)) : null;
      for (const r of rows) {
        const li = CT.el("li", { class: (r.on ? "" : "off ") + (r === nearest ? "nearest" : "") });
        li.append(CT.el("span", { text: r.name }), CT.el("b", { text: price(r.v) }),
          CT.el("span", { class: "hint", text: r.on
            ? distText(r.dist, r.isTp)
            : "off today" }));
        list.append(li);
      }
      if (pos.rules.includes("HardTimeExit")) {
        list.append(CT.el("li", {}, CT.el("span", { text: "Time exit" }), CT.el("b", { text: fmtShort(Date.parse(pos.sell_by)) }),
          CT.el("span", { class: "hint", text: "sells whatever is left" })));
      }
      if (Date.parse(pos.watch_from) > t) {
        list.append(CT.el("li", { class: "off" }, CT.el("span", { text: "Rules start" }), CT.el("b", { text: fmtShort(Date.parse(pos.watch_from)) }), CT.el("span")));
      }
      card.append(list);
    }
    box.append(card);
  }
}

function distText(d, isTp) {
  if (d == null) return "";
  const n = Math.abs(d * 100).toFixed(1);
  if (isTp) return d > 0 ? `${n}% to go` : "reached";
  return d >= 0 ? `${n}% above it` : `price is ${n}% below it`;
}

function chart(pick, tr) {
  const W = 640, H = 170, P = 6;
  const pts = seriesIdx[pick.osi] || [];
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "pick-chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${pick.name} price over the simulation`);
  if (pts.length < 2) return svg;
  // Trigger paths over the frames where the position was held.
  const tp = [], stop = [];
  R.frames.forEach((f, i) => {
    const p = f.positions.find((q) => q.osi === pick.osi);
    if (!p) return;
    const active = new Set(p.rules);
    if (active.has("TakeProfit")) tp.push([i, p.triggers.take_profit]);
    const s = STOP_KEYS.filter((k) => p.triggers[k] != null && active.has(RULE_NAMES[k][1])).map((k) => p.triggers[k]);
    if (s.length) stop.push([i, Math.max(...s)]);
  });
  const vals = pts.map((p) => p[3]).concat(tp.map((p) => p[1]), stop.map((p) => p[1]));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.08 || 0.05;
  lo -= pad; hi += pad;
  const n = Math.max(1, frameT.length - 1);
  const x = (i) => P + (i / n) * (W - 2 * P);
  const y = (v) => H - P - ((v - lo) / (hi - lo)) * (H - 2 * P);
  const path = (arr, cls) => {
    if (!arr.length) return;
    const el = document.createElementNS(ns, "path");
    el.setAttribute("d", arr.map((p, j) => `${j && arr[j][0] - arr[j - 1][0] < 3 ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(""));
    el.setAttribute("class", cls);
    svg.append(el);
  };
  // Day boundaries
  for (let i = 1; i < frameT.length; i++) {
    if (mtDay(frameT[i]) !== mtDay(frameT[i - 1])) {
      const l = document.createElementNS(ns, "line");
      l.setAttribute("x1", x(i)); l.setAttribute("x2", x(i)); l.setAttribute("y1", 0); l.setAttribute("y2", H);
      l.setAttribute("class", "c-day");
      svg.append(l);
    }
  }
  path(tp, "c-tp");
  path(stop, "c-stop");
  path(pts.map((p) => [p[0], p[3]]), "c-future");
  path(pts.filter((p) => p[0] <= idx).map((p) => [p[0], p[3]]), "c-price");
  const marker = (ms, cls, label) => {
    if (ms == null) return;
    const i = frameAt(ms);
    const pt = pts.find((p) => p[0] >= i) || pts[pts.length - 1];
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", x(i)); c.setAttribute("cy", y(pt[3])); c.setAttribute("r", 5);
    c.setAttribute("class", cls);
    const title = document.createElementNS(ns, "title");
    title.textContent = label;
    c.append(title);
    svg.append(c);
  };
  marker(tr.entryMs, "c-buy", `Bought at ${price(tr.entry_price)}`);
  if (tr.exitMs != null && tr.exitMs <= frameT[idx]) marker(tr.exitMs, "c-sell", `Sold at ${price(tr.exit_price)} (${tr.why})`);
  const head = document.createElementNS(ns, "line");
  head.setAttribute("x1", x(idx)); head.setAttribute("x2", x(idx)); head.setAttribute("y1", 0); head.setAttribute("y2", H);
  head.setAttribute("class", "c-head");
  svg.append(head);
  return svg;
}

function renderEvents(t) {
  const box = $("pb-events");
  const showLogs = $("ev-logs").checked;
  const all = $("ev-all").checked;
  const items = [];
  for (let i = 0; i < R.events.length; i++) {
    const e = R.events[i];
    if (!all && eventT[i] > t) break;
    if (e.kind === "log" && !showLogs) continue;
    if (e.kind === "note") continue;
    items.push([i, e]);
  }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = "";
  for (const [i, e] of items.slice(-400)) {
    const row = CT.el("div", { class: `ev ev-${e.kind} lvl-${e.level}` + (eventT[i] > t ? " ev-future" : "") });
    row.append(CT.el("span", { class: "ev-time", text: fmtShort(eventT[i]) }),
      CT.el("span", { class: "ev-kind", text: e.kind }),
      CT.el("span", { class: "ev-text", text: e.text }));
    row.addEventListener("click", () => { pause(); seek(frameAt(eventT[i]), true); });
    box.append(row);
  }
  if (atBottom || playing) box.scrollTop = box.scrollHeight;
}

function renderTrades() {
  const tb = $("pb-trades").querySelector("tbody");
  tb.innerHTML = "";
  if (!R.trades.length) { tb.append(CT.el("tr", {}, CT.el("td", { colspan: 7, text: "No trades." }))); return; }
  for (const t of R.trades) {
    const tr = CT.el("tr", { class: t.open ? "open" : "" });
    const cells = [t.name, t.entryMs ? fmtShort(t.entryMs) : "—", price(t.entry_price),
      t.open ? "still held" : fmtShort(t.exitMs), t.open ? "" : price(t.exit_price),
      t.open ? "" : money(t.pnl), t.why || ""];
    cells.forEach((c, i) => {
      const td = CT.el("td", { text: c });
      if ([2, 4, 5].includes(i)) td.className = "num";
      if (i === 5 && !t.open) td.classList.add((t.pnl || 0) >= 0 ? "pnl-pos" : "pnl-neg");
      tr.append(td);
    });
    tr.addEventListener("click", () => { if (t.entryMs) { pause(); seek(frameAt(t.entryMs), true); } });
    tb.append(tr);
  }
  const sum = R.summary;
  const foot = CT.el("tr", {}, CT.el("td", { colspan: 5, text: `Total: ${sum.sold} sold, ${sum.still_open} still held` }),
    CT.el("td", { class: "num " + (sum.realized_pnl >= 0 ? "pnl-pos" : "pnl-neg"), text: money(sum.realized_pnl) }), CT.el("td"));
  tb.append(foot);
}

function renderUnbought() {
  const boughtOsi = new Set(R.trades.map((x) => x.osi));
  const rest = R.picks.filter((p) => !boughtOsi.has(p.osi));
  const ul = $("pb-unbought");
  ul.innerHTML = "";
  $("pb-unbought-title").textContent = `Not bought (${rest.length})`;
  $("pb-unbought-wrap").hidden = !rest.length;
  for (const p of rest) {
    const id = p.label.replace(" ", "-") + "-" + p.expiration.replaceAll("-", "");
    const skip = R.events.find((e) => e.kind === "skip" && e.text.includes(id + ":"));
    ul.append(CT.el("li", { text: `${p.name}: ${skip ? skip.text.split(": ").slice(1).join(": ") : "never bought (no skip recorded)"}` }));
  }
}

// -- history ---------------------------------------------------------------------
async function loadHistory() {
  const { ok, body } = await CT.get("/api/sim/history");
  const tb = $("sim-history").querySelector("tbody");
  tb.innerHTML = "";
  if (!ok || !body.runs.length) { tb.append(CT.el("tr", {}, CT.el("td", { colspan: 6, text: "None yet." }))); return; }
  for (const r of body.runs) {
    const res = r.state === "done" && r.summary
      ? `${r.summary.bought} bought, ${r.summary.sold} sold, ${money(r.summary.realized_pnl)}`
      : (r.outcome || r.state);
    const open = CT.el("button", { class: "btn", text: "Open" });
    open.disabled = r.state !== "done";
    open.addEventListener("click", () => openResult(r.id));
    const again = CT.el("button", { class: "btn", text: "Use these settings" });
    again.addEventListener("click", () => fillForm(r.request));
    tb.append(CT.el("tr", {},
      CT.el("td", { text: CT.fmtTime(r.started) }), CT.el("td", { text: r.list }),
      CT.el("td", { text: r.buy_day || "" }), CT.el("td", { text: r.prices + (r.prices === "scenario" ? ` (${r.shape})` : "") }),
      CT.el("td", { text: res }), CT.el("td", {}, open, " ", again)));
  }
}

async function saveSeed() {
  const res = await CT.post("/api/sim/seeds", { name: $("seed-name").value, csv: $("sim-csv").value });
  $("seed-msg").textContent = res.ok ? "Saved." : (res.body.error || "Could not save");
  $("seed-msg").className = "msg " + (res.ok ? "ok" : "is-error");
  if (res.ok) await loadLists();
}

document.addEventListener("DOMContentLoaded", () => {
  loadLists();
  loadHistory();
  $("sim-list").addEventListener("change", chooseList);
  $("sim-csv").addEventListener("change", renderPerPick);
  document.querySelectorAll('input[name="sim-prices"]').forEach((r) => r.addEventListener("change", togglePriceOpts));
  $("btn-play").addEventListener("click", startRun);
  $("btn-save-seed").addEventListener("click", saveSeed);
  $("btn-history").addEventListener("click", loadHistory);
  $("pb-toggle").addEventListener("click", () => (playing ? pause() : play()));
  $("pb-restart").addEventListener("click", () => { pause(); seek(0, true); });
  $("pb-step").addEventListener("click", () => { pause(); seek(idx + 1, true); });
  $("pb-scrub").addEventListener("input", (e) => { seek(Number(e.target.value), true); });
  $("ev-logs").addEventListener("change", () => R && render());
  $("ev-all").addEventListener("change", () => R && render());
  document.addEventListener("keydown", (e) => {
    if (!R || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    if (e.key === " ") { e.preventDefault(); playing ? pause() : play(); }
    if (e.key === "ArrowRight") { pause(); seek(idx + 1, true); }
    if (e.key === "ArrowLeft") { pause(); seek(idx - 1, true); }
  });
});
