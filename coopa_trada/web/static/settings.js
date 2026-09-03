"use strict";
// Settings: load rules into the form, save with inline validation messages.

const form = document.getElementById("rules-form");
const msg = document.getElementById("rules-msg");
const pathEl = document.getElementById("rules-path");
const fileWarn = document.getElementById("rules-file-warning");
const noteEl = document.getElementById("ambiguous-note");
const ewRows = document.getElementById("exit-window-rows");

const PCT_OR_INT_FIELDS = [
  "entry_minutes_before_close", "poll_interval_seconds",
  "trailing_stop_pct", "hard_stop_loss_pct", "take_profit_pct",
  "max_concurrent_positions", "max_dollars_per_position",
  "cooper_drop_from_peak_pct", "cooper_drop_from_entry_pct",
];

function renderExitWindow(ew) {
  ewRows.innerHTML = "";
  for (const cls of Object.keys(ew)) {
    const [start, end] = ew[cls];
    const row = CT.el("div", { class: "ew-row" });
    row.append(CT.el("span", { class: "ew-label", text: cls }));
    const s = CT.el("input", { type: "text", value: String(start), "data-ew": cls, "data-end": "0" });
    const e = CT.el("input", { type: "text", value: String(end), "data-ew": cls, "data-end": "1" });
    row.append("start ", s, " end ", e);
    ewRows.append(row);
  }
}

function fill(data) {
  pathEl.textContent = "File: " + data.path;
  noteEl.textContent = data.ambiguous_note || "";
  if (data.warning) {
    fileWarn.hidden = false;
    fileWarn.textContent = "Current file fails validation: " + data.warning;
  } else {
    fileWarn.hidden = true;
  }
  for (const name of PCT_OR_INT_FIELDS) {
    const input = form.elements[name];
    if (!input) continue;
    const v = data[name];
    input.value = v === null || v === undefined ? "" : v;
  }
  renderExitWindow(data.exit_window || {});
}

function collect() {
  const payload = {};
  for (const name of PCT_OR_INT_FIELDS) {
    const input = form.elements[name];
    if (!input) continue;
    payload[name] = input.value === "" ? null : Number(input.value);
  }
  const ew = {};
  ewRows.querySelectorAll("input[data-ew]").forEach((inp) => {
    const cls = inp.dataset.ew;
    ew[cls] = ew[cls] || [null, null];
    ew[cls][Number(inp.dataset.end)] = inp.value.trim();
  });
  payload.exit_window = ew;
  return payload;
}

async function load() {
  msg.textContent = "";
  const { ok, body } = await CT.get("/api/rules");
  if (ok) fill(body);
  else msg.textContent = "could not load rules";
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  msg.textContent = "saving…";
  msg.className = "msg";
  const { ok, body } = await CT.post("/api/rules", collect());
  if (ok) {
    fill(body);
    msg.textContent = "Saved and reloaded.";
    msg.className = "msg ok";
  } else {
    msg.textContent = body.error || "save rejected";
    msg.className = "msg is-error";
  }
});

document.getElementById("btn-reload").addEventListener("click", load);
load();
