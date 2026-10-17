"use strict";
(() => {
  const csv = document.getElementById("picks-csv");
  const day = document.getElementById("picks-date");
  const expiration = document.getElementById("picks-expiration");
  const message = document.getElementById("picks-message");
  const table = document.getElementById("picks-table");
  const confirm = document.getElementById("picks-confirm");
  const upload = document.getElementById("picks-upload");
  const preview = document.getElementById("picks-preview");
  let token = null;
  let revision = 0;
  function invalidate() {
    revision += 1;
    token = null;
    confirm.disabled = true;
    table.replaceChildren();
  }
  [csv, day, expiration].forEach(el => el.addEventListener("input", invalidate));
  // Pictures need Apple's text recognition; elsewhere, say so before anyone tries.
  CT.get("/api/app").then(({ok, body}) => {
    if (!ok || body.image_upload.available) return;
    document.getElementById("picks-file").setAttribute("accept", ".csv");
    message.textContent = body.image_upload.reason;
  });
  upload.addEventListener("click", async () => {
    const file = document.getElementById("picks-file").files[0];
    if (!file) { message.textContent = "Choose a file first."; return; }
    invalidate();
    const version = revision;
    upload.disabled = true;
    message.textContent = "Reading file…";
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/signals/upload", {method: "POST", body: form});
      const body = await response.json();
      if (version !== revision) return;
      if (!response.ok) throw new Error(body.error);
      csv.value = body.csv;
      message.textContent = "Check every row against the original, enter both full dates, then preview.";
    } catch (error) { message.textContent = error.message; }
    finally { upload.disabled = false; }
  });
  preview.addEventListener("click", async () => {
    invalidate();
    const version = revision;
    preview.disabled = true;
    try {
      const {ok, body} = await CT.post("/api/signals/preview", {csv: csv.value, date: day.value, expiration: expiration.value});
      if (version !== revision) return;
      if (!ok) throw new Error(body.error);
      token = body.token;
      const t = document.createElement("table");
      const head = t.createTHead().insertRow();
      ["Symbol", "Option", "Last price", "TVT delta", "Schwab delta"].forEach(label => {
        const th = document.createElement("th"); th.textContent = label; head.append(th);
      });
      const rows = t.createTBody();
      body.rows.forEach(pick => {
        const row = rows.insertRow();
        [pick.symbol, pick.option, pick.last_price, pick.tvt_delta, pick.schwab_delta].forEach(value => {
          row.insertCell().textContent = value == null ? "—" : String(value);
        });
      });
      table.replaceChildren(t);
      message.textContent = `${body.rows.length} picks for ${body.date}, expiring ${body.expiration}. Confirm only after checking the original. Last prices are reference prices; orders use fresh quotes.`;
      confirm.disabled = false;
    } catch (error) { message.textContent = error.message; }
    finally { preview.disabled = false; }
  });
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      const {ok, body} = await CT.post("/api/signals/confirm", {token});
      if (!ok) throw new Error(body.error);
      document.getElementById("run-date").value = body.date;
      message.textContent = body.message;
      token = null;
    } catch (error) { message.textContent = error.message; confirm.disabled = !token; }
  });
})();
