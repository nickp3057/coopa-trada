"use strict";
// Dashboard view of previews that arrived through the Telegram bot.
(() => {
  const card = document.getElementById("telegram-card");
  const list = document.getElementById("telegram-pending");
  const msg = document.getElementById("telegram-msg");
  if (!card) return;

  function render(body) {
    card.hidden = !body.configured;
    if (!body.configured) return;
    if (body.ignored_chats && body.ignored_chats.length) {
      msg.textContent = "Ignored messages from chat ID(s) " + body.ignored_chats.join(", ") +
        " (not in TELEGRAM_ALLOWED_CHAT_IDS). If one of them is Cooper, add it to .env and restart.";
    }
    if (!body.pending.length) {
      list.replaceChildren(CT.el("p", { class: "hint", text: "No Telegram list is waiting." }));
      return;
    }
    list.replaceChildren(...body.pending.map((p) => {
      const box = CT.el("div", { class: "telegram-preview" });
      box.append(CT.el("p", { text: `Picks for ${p.trade_date}, expiring ${p.expiration} · received ${CT.fmtTime(p.created_at)}` }));
      const rows = CT.el("ul", { class: "plain-list" });
      for (const r of p.rows) {
        rows.append(CT.el("li", { text: `${r.symbol} ${r.option} · last ${r.last_price} · TVT ${r.tvt_delta ?? "—"} · Schwab ${r.schwab_delta ?? "—"}` }));
      }
      box.append(rows);
      const ok = CT.el("button", { class: "btn btn-primary", text: "Confirm these picks" });
      const no = CT.el("button", { class: "btn", text: "Reject" });
      ok.addEventListener("click", () => act(p.id, "confirm"));
      no.addEventListener("click", () => act(p.id, "reject"));
      box.append(CT.el("div", { class: "row" }, ok, no));
      return box;
    }));
  }

  async function act(id, action) {
    const { body } = await CT.post(`/api/telegram/${encodeURIComponent(id)}/${action}`, {});
    msg.textContent = body.message || body.error || "";
    load();
  }

  async function load() {
    const { ok, body } = await CT.get("/api/telegram");
    if (ok) render(body);
  }

  load();
  setInterval(load, 10000);
})();
