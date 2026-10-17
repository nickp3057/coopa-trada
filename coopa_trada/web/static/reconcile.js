"use strict";
// Reconcile: run it, render the plain-language lines.

const out = document.getElementById("reconcile-out");
const btn = document.getElementById("btn-reconcile");

btn.addEventListener("click", async () => {
  btn.disabled = true;
  out.textContent = "running…";
  const { ok, body } = await CT.post("/api/reconcile", {});
  btn.disabled = false;
  out.innerHTML = "";
  if (!ok) {
    out.append(CT.el("p", { class: "error", text: body.error || "reconcile failed" }));
    return;
  }
  const head = CT.el("p", { class: "hint", text: "mode: " + body.mode + (body.simulated ? " (simulated)" : "") });
  out.append(head);
  const ul = CT.el("ul", { class: "recon-lines" });
  (body.lines || []).forEach((line) => ul.append(CT.el("li", { text: line })));
  out.append(ul);
  if (body.divergences && body.divergences.length) {
    out.classList.add("has-divergence");
  } else {
    out.classList.remove("has-divergence");
  }
});

// Orders whose submission could not be confirmed: show and resolve.
const attentionCard = document.getElementById("attention-card");
const attentionList = document.getElementById("attention-list");

const OUTCOME_TEXT = {
  UNKNOWN: "Outcome unknown. This contract is halted.",
  NOT_PLACED: "Confirmed NOT placed (the connection to Schwab never opened). Nothing was resent.",
};

function incidentRow(item) {
  const box = CT.el("div", { class: "incident" });
  box.append(CT.el("p", { class: "incident-title",
    text: `${item.side} ${item.quantity} × ${item.osi_symbol} at $${Number(item.limit_price).toFixed(2)} (${item.intent.toLowerCase()}, ${item.mode})` }));
  box.append(CT.el("p", { text: OUTCOME_TEXT[item.outcome] || item.outcome }));
  box.append(CT.el("p", { class: "hint", text: "When: " + CT.fmtTime(item.created_at_utc) + " · Error: " + item.error }));
  const note = CT.el("input", { type: "text", placeholder: "What you checked, e.g. 'Schwab Orders tab, 3:42pm'" });
  const orderId = CT.el("input", { type: "text", placeholder: "Schwab order number", inputmode: "numeric" });
  const msg = CT.el("p", { class: "msg" });
  const send = async (placed) => {
    const { ok, body } = await CT.post(`/api/attention/${item.id}/resolve`,
      { placed, note: note.value, broker_order_id: orderId.value });
    if (ok) loadAttention();
    else { msg.textContent = body.error || "could not save"; msg.className = "msg is-error"; }
  };
  const row = CT.el("div", { class: "row" });
  row.append(note);
  if (item.outcome === "UNKNOWN") {
    const notThere = CT.el("button", { class: "btn", text: "It is NOT in Schwab" });
    notThere.addEventListener("click", () => send(false));
    const there = CT.el("button", { class: "btn", text: "It IS in Schwab (enter order number)" });
    there.addEventListener("click", () => send(true));
    row.append(orderId, there, notThere);
  } else {
    const ack = CT.el("button", { class: "btn", text: "Acknowledge" });
    ack.addEventListener("click", () => send(false));
    row.append(ack);
  }
  box.append(row, msg);
  return box;
}

async function loadAttention() {
  const { ok, body } = await CT.get("/api/attention");
  if (!ok) return;
  attentionCard.hidden = body.incidents.length === 0;
  attentionList.replaceChildren(...body.incidents.map(incidentRow));
}

loadAttention();
