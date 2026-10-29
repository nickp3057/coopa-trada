"use strict";
// Dashboard: "Today's picks" card. Is a list loaded, and what happened to each pick.

const todayHeadline = document.getElementById("today-headline");
const todayRun = document.getElementById("today-run");
const todayPicks = document.getElementById("today-picks");
const todayHeld = document.getElementById("today-held");
const todayHeldTable = document.getElementById("today-held-table");

function clockMT(iso, suffix = true) {
  const t = new Date(iso).toLocaleTimeString("en-US", {
    timeZone: "America/Denver", hour: "numeric", minute: "2-digit",
  });
  return suffix ? t + " MT" : t.replace(/ [AP]M$/, "");
}

function dayText(isoDay) {
  // Noon UTC keeps the date the same in every US time zone.
  return new Date(isoDay + "T12:00:00Z").toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric", timeZone: "UTC",
  });
}

function modeTag(mode) {
  if (!mode) return "";
  return CT.el("span", { class: "badge" + (mode === "LIVE" ? " badge-warn" : ""),
                         text: mode === "LIVE" ? "Real" : "Practice" });
}

function twoLines(top, bottom) {
  const box = CT.el("span", {}, top);
  if (bottom) box.append(CT.el("br"), CT.el("span", { class: "hint", text: bottom }));
  return box;
}

function table(headers, rows) {
  const t = CT.el("table");
  const head = CT.el("tr");
  headers.forEach((h) => head.append(CT.el("th", { text: h })));
  t.append(CT.el("thead", {}, head));
  const body = CT.el("tbody");
  rows.forEach((cells) => {
    const tr = CT.el("tr");
    cells.forEach((c) => tr.append(c instanceof Node ? CT.el("td", {}, c) : CT.el("td", { text: String(c) })));
    body.append(tr);
  });
  t.append(body);
  return t;
}

function headline(b) {
  const day = dayText(b.day);
  if (!b.trading_day) return [`${day}: the market is closed today.`, "warn"];
  if (b.list_error) return [b.list_error, "is-error"];
  if (!b.list_loaded) return [`No picks loaded for ${day} yet.`, "warn"];
  const n = b.picks.length;
  return [`${n} pick${n === 1 ? "" : "s"} loaded for ${day}` +
          (b.confirmed_at ? `, confirmed at ${clockMT(b.confirmed_at)}.` : "."), "ok"];
}

function runLine(b) {
  if (!b.trading_day) return ["", ""];
  const w = b.buy_window ? `${clockMT(b.buy_window.start, false)}–${clockMT(b.buy_window.end)}` : "";
  if (b.run_active) {
    if (b.phase === "before") return [`Today's run is going. It buys in the buying window, ${w}.`, "is-running"];
    if (b.phase === "open") return [`Today's run is going and the buying window is open (until ${clockMT(b.buy_window.end)}).`, "is-running"];
    return ["Today's run is going (buying is over for today; it's watching positions).", "is-running"];
  }
  if (b.phase === "after") return ["No run is going. Buying is over for today.", ""];
  return ["No run is going, so nothing will be bought. Start today's run below" +
          (w ? ` before the buying window (${w}).` : "."), "is-error"];
}

function render(b) {
  const [text, cls] = headline(b);
  todayHeadline.textContent = text;
  todayHeadline.className = "status-line " + cls;
  const [run, runCls] = runLine(b);
  todayRun.textContent = run;
  todayRun.className = "status-line " + runCls;
  todayRun.hidden = !run;

  todayPicks.replaceChildren();
  if (b.picks.length) {
    const anySold = b.picks.some((p) => p.pnl != null);
    todayPicks.append(table(anySold ? ["Pick", "Status", "P&L"] : ["Pick", "Status"], b.picks.map((p) => {
      const status = CT.el("span", {}, CT.el("span", { class: "pick-state " + p.state, text: p.text }), modeTag(p.mode));
      const cells = [twoLines(p.name, p.expiration ? "exp " + dayText(p.expiration).replace(/^\w+, /, "") : ""), status];
      if (anySold) {
        const pnl = CT.el("span", { text: p.pnl == null ? "" : CT.fmtMoney(p.pnl) });
        if (p.pnl != null) pnl.className = p.pnl >= 0 ? "pnl-pos" : "pnl-neg";
        cells.push(pnl);
      }
      return cells;
    })));
  }

  todayHeld.hidden = !b.held.length;
  todayHeldTable.replaceChildren(table(["Option", "Bought", "Paid"], b.held.map((h) =>
    [CT.el("span", {}, h.name, modeTag(h.mode)), dayText(h.bought_on),
     CT.fmtMoney(h.entry_price) + (h.quantity > 1 ? ` × ${h.quantity}` : "")])));
}

async function loadToday() {
  const { ok, body } = await CT.get("/api/today");
  if (!ok) {
    todayHeadline.textContent = "Could not load today's picks.";
    todayHeadline.className = "status-line is-error";
    return;
  }
  render(body);
}

loadToday();
setInterval(loadToday, 10000);
