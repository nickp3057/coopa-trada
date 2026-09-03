"use strict";
// Trade log: render /api/trades into the table.

const tbody = document.querySelector("#trades-table tbody");
const summaryEl = document.getElementById("trades-summary");

function row(p) {
  const tr = CT.el("tr", { class: p.is_open ? "open" : "" });
  const cells = [
    p.underlying || "—",
    p.osi_symbol,
    CT.fmtTime(p.entry_at_utc),
    p.entry_price == null ? "—" : p.entry_price.toFixed(2),
    p.is_open ? "" : CT.fmtTime(p.exit_at_utc),
    p.exit_price == null ? "" : p.exit_price.toFixed(2),
    p.quantity,
    p.is_open ? "" : CT.fmtMoney(p.realized_pnl),
    p.is_open ? "" : ((p.exit_strategy || "") + (p.exit_reason ? " — " + p.exit_reason : "")),
    p.mode || "",
  ];
  cells.forEach((c, i) => {
    const td = CT.el("td", { text: String(c) });
    if ([3, 5, 6, 7].includes(i)) td.className = "num";
    if (i === 7 && !p.is_open && p.realized_pnl != null) {
      td.classList.add(p.realized_pnl >= 0 ? "pnl-pos" : "pnl-neg");
    }
    tr.append(td);
  });
  if (p.is_open) {
    tr.children[4].append(CT.el("span", { class: "badge", text: "OPEN" }));
  }
  return tr;
}

async function load() {
  tbody.innerHTML = "<tr><td colspan='10'>loading…</td></tr>";
  const { ok, body } = await CT.get("/api/trades");
  if (!ok) {
    tbody.innerHTML = "<tr><td colspan='10'>could not load trade log</td></tr>";
    return;
  }
  const rows = body.positions || [];
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = "<tr><td colspan='10'>No positions recorded yet.</td></tr>";
  } else {
    rows.forEach((p) => tbody.append(row(p)));
  }
  const s = body.summary || {};
  summaryEl.textContent =
    `${s.total} position(s) · ${s.open} open · ${s.closed} closed · realized P&L ` +
    CT.fmtMoney(s.realized_pnl);
}

document.getElementById("btn-refresh").addEventListener("click", load);
load();
