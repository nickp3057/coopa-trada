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
  const fileInput = document.getElementById("picks-file");
  const grid = document.getElementById("picks-grid");
  const addRow = document.getElementById("picks-add-row");
  const figure = document.getElementById("picks-figure");
  const image = document.getElementById("picks-image");
  const COLUMNS = ["SYMBOL", "OPTION", "LAST PRICE", "TVT-DELTA", "SCHWAB DELTA"];
  const LABELS = ["Symbol", "Option", "Last price", "TVT delta", "Schwab delta"];
  let token = null;
  let revision = 0;
  let imageUrl = null;

  function invalidate() {
    revision += 1;
    token = null;
    confirm.disabled = true;
    table.replaceChildren();
  }

  // -- the text <-> table, kept in step -----------------------------------------
  function splitCsv(line) {
    const out = [];
    let cell = "", quoted = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (quoted) {
        if (ch === '"' && line[i + 1] === '"') { cell += '"'; i++; }
        else if (ch === '"') quoted = false;
        else cell += ch;
      } else if (ch === '"') quoted = true;
      else if (ch === ",") { out.push(cell); cell = ""; }
      else cell += ch;
    }
    out.push(cell);
    return out.map((c) => c.trim());
  }

  function parts() {
    const lines = csv.value.replace(/\r/g, "").split("\n");
    const at = lines.findIndex((l) => {
      const cells = splitCsv(l).map((c) => c.toUpperCase());
      return cells.includes("SYMBOL") && cells.includes("OPTION");
    });
    if (at < 0) return null;
    const rows = lines.slice(at + 1).filter((l) => l.trim()).map((l) => {
      const cells = splitCsv(l);
      while (cells.length < 5) cells.push("");
      return cells.slice(0, 5);
    });
    return { title: lines.slice(0, at), rows };
  }

  function cleanCell(value) {
    return value.replace(/[",\n\r]/g, " ").replace(/\s+/g, " ").trim();
  }

  function writeText(title, rows) {
    csv.value = [...title, COLUMNS.join(","), ...rows.map((r) => r.map(cleanCell).join(","))].join("\n") + "\n";
  }

  function rowsFromGrid() {
    return [...grid.querySelectorAll("tbody tr")].map((tr) => [...tr.querySelectorAll("input")].map((i) => i.value));
  }

  function renderGrid() {
    const p = parts();
    addRow.hidden = !p;
    if (!p) { grid.replaceChildren(); return; }
    const t = document.createElement("table");
    const head = t.createTHead().insertRow();
    [...LABELS, ""].forEach((label) => { const th = document.createElement("th"); th.textContent = label; head.append(th); });
    const body = t.createTBody();
    p.rows.forEach((row) => body.append(gridRow(row)));
    grid.replaceChildren(t);
  }

  function gridRow(row) {
    const tr = document.createElement("tr");
    row.forEach((value, i) => {
      const input = document.createElement("input");
      input.value = value;
      input.setAttribute("aria-label", LABELS[i]);
      input.spellcheck = false;
      input.autocapitalize = "characters";
      if (i >= 2) { input.className = "num"; input.inputMode = "decimal"; }
      input.addEventListener("input", syncFromGrid);
      tr.insertCell().append(input);
    });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "del";
    del.title = "Remove this row";
    del.textContent = "✕";
    del.addEventListener("click", () => { tr.remove(); syncFromGrid(); });
    tr.insertCell().append(del);
    return tr;
  }

  function syncFromGrid() {
    const p = parts();
    writeText(p ? p.title : [], rowsFromGrid());
    invalidate();
  }

  addRow.addEventListener("click", () => {
    let body = grid.querySelector("tbody");
    if (!body) return;
    const tr = gridRow(["", "", "", "", ""]);
    body.append(tr);
    syncFromGrid();
    tr.querySelector("input").focus();
  });
  csv.addEventListener("input", () => { invalidate(); renderGrid(); });
  [day, expiration].forEach((el) => el.addEventListener("input", invalidate));

  // -- the picture next to the table ----------------------------------------------
  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
    figure.hidden = true;
    if (file && file.type.startsWith("image/")) {
      imageUrl = URL.createObjectURL(file);
      image.src = imageUrl;
      figure.hidden = false;
    }
  });

  // Pictures need a reader (Apple's on a Mac, or Claude with an API key); without one, say so up front.
  CT.get("/api/app").then(({ok, body}) => {
    if (!ok || body.image_upload.available) return;
    fileInput.setAttribute("accept", ".csv");
    message.textContent = body.image_upload.reason;
  });

  upload.addEventListener("click", async () => {
    const file = fileInput.files[0];
    if (!file) { message.textContent = "Choose a file first."; return; }
    invalidate();
    const version = revision;
    upload.disabled = true;
    message.textContent = "Reading file… (a picture takes 10–30 seconds)";
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/signals/upload", {method: "POST", body: form});
      const body = await response.json();
      if (version !== revision) return;
      if (!response.ok) throw new Error(body.error);
      csv.value = body.csv;
      renderGrid();
      const filled = [];
      if (body.date) { day.value = body.date; filled.push("trade date"); }
      if (body.expiration) { expiration.value = body.expiration; filled.push("expiration"); }
      message.textContent = "Check every row against the picture" +
        (filled.length ? ", and the " + filled.join(" and ") + " filled in from its title" : ", enter both full dates") +
        ", then press Preview picks.";
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
      LABELS.forEach(label => {
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
      message.textContent = `${body.rows.length} picks for ${body.date}, expiring ${body.expiration}. Confirm only after checking the original. Last prices are reference prices; orders use fresh quotes.` +
        (body.notes && body.notes.length ? " " + body.notes.join(" ") : "");
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
