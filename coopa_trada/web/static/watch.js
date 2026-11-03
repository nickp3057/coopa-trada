"use strict";
// "Watching now": every open position with its latest price and each rule's trigger price.
// Used on the Dashboard (card hidden when nothing is open) and the Activity page.

(function () {
  const NAMES = {
    take_profit: ["Take profit", "Sells when the price reaches this (the take-profit %)."],
    ten_point: ["10-point rule", "Sells when profit falls this many points below its best."],
    hard_stop: ["Hard stop", "Sells when the price falls this far below the buy price."],
    trailing_stop: ["Trailing stop", "Sells when the price falls this far below its high."],
    drop_from_peak: ["10% from the high", "The older reading of Cooper's 10% rule."],
    drop_from_entry: ["Drop from entry", "The older reading of Cooper's 10% rule, measured from the buy."],
  };
  const price = (n) => (n == null ? "—" : "$" + Number(n).toFixed(2));

  function distText(d, isTp) {
    if (d == null) return "";
    const n = Math.abs(d * 100).toFixed(1);
    if (isTp) return d > 0 ? `${n}% to go` : "reached";
    return d >= 0 ? `${n}% above it` : `price is ${n}% below it`;
  }

  function card(p) {
    const c = CT.el("div", { class: "pick-card" });
    c.append(CT.el("div", { class: "pick-head" },
      CT.el("strong", { text: p.name || p.osi, "data-tip": p.osi }),
      CT.el("span", { class: "pick-state held", text: `${p.mode === "LIVE" ? "REAL" : "practice"} · ×${p.qty}` })));
    const mid = p.mid;
    const facts = CT.el("div", { class: "pick-facts" });
    const add = (k, v, tip) => {
      const s = CT.el("span", {}, CT.el("span", { class: "hint", text: k + " " }), CT.el("b", { text: v }));
      if (tip) s.setAttribute("data-tip", tip);
      facts.append(s);
    };
    add("bought", CT.fmtTime(p.bought_at));
    add("paid", price(p.paid), "What one contract cost per share (×100 for the dollar cost)");
    add("measured from", price(p.ref), "The price the rules measure gains and losses from (the mid at purchase, unless changed in Settings)");
    add("high", price(p.peak), "The highest mid seen since it was bought");
    add("latest", mid == null ? "no price yet" : `${price(mid)} at ${CT.fmtTime(p.quoted_at)}`, "The last price the bot read (the mid)");
    if (mid != null) add("gain", ((mid / p.ref - 1) * 100).toFixed(1) + "%");
    if (p.sell_by) add("next-day sell by", CT.fmtTime(p.sell_by), "Anything still open is sold at this time on the next trading day");
    c.append(facts);
    const list = CT.el("ul", { class: "trigger-list" });
    const stops = Object.entries(p.triggers).filter(([k, v]) => v != null && k !== "take_profit");
    const nearest = stops.length ? stops.reduce((a, b) => (a[1] > b[1] ? a : b))[0] : null;
    // Take profit first, then the stops from the nearest (highest) down.
    const ordered = Object.entries(p.triggers).filter(([, v]) => v != null)
      .sort((a, b) => (a[0] === "take_profit" ? -1 : b[0] === "take_profit" ? 1 : b[1] - a[1]));
    for (const [k, v] of ordered) {
      const isTp = k === "take_profit";
      const d = mid == null ? null : isTp ? v / mid - 1 : mid / v - 1;
      const [name, tip] = NAMES[k] || [k, ""];
      const li = CT.el("li", { class: k === nearest ? "nearest" : "", "data-tip": tip + (k === nearest ? " This is the stop that would fire first." : "") },
        CT.el("span", { text: name }), CT.el("b", { text: price(v) }), CT.el("span", { class: "hint", text: distText(d, isTp) }));
      list.append(li);
    }
    c.append(list);
    return c;
  }

  CT.loadWatch = async function ({ box, card: wrapper, updated, hideWhenEmpty }) {
    const { ok, body } = await CT.get("/api/watch");
    const el = document.getElementById(box);
    if (!el) return;
    el.innerHTML = "";
    const up = updated && document.getElementById(updated);
    if (up) up.textContent = "Updated " + new Date().toLocaleTimeString("en-US", { timeZone: "America/Denver", hour: "numeric", minute: "2-digit" }) + " MT";
    if (!ok) { el.append(CT.el("p", { class: "error", text: body.error || "Could not load" })); return; }
    const w = wrapper && document.getElementById(wrapper);
    if (w && hideWhenEmpty) w.hidden = !body.positions.length;
    if (!body.positions.length) { el.append(CT.el("p", { class: "empty", text: "Nothing is open right now." })); return; }
    for (const p of body.positions) el.append(card(p));
  };

  // Dashboard: start it here (the Activity page calls CT.loadWatch itself).
  if (document.getElementById("watch-card")) {
    const opts = { box: "watch", card: "watch-card", updated: "watch-updated", hideWhenEmpty: true };
    CT.loadWatch(opts);
    setInterval(() => CT.loadWatch(opts), 10000);
  }
})();
