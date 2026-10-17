"use strict";
// Settings: load rules into the form, save with inline validation messages.
// Delta bounds and confirmation markers are validated server-side by the same
// code that loads rules.yaml.

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
const DELTA_NUMBERS = ["min_abs_delta", "max_abs_delta"];

let saved = null; // last response from the server

function renderConfirmations(data) {
  for (const item of data.default_settings || []) {
    const row = form.querySelector(`[data-confirm-key="${item.key}"]`);
    if (!row) continue;
    const ok = item.confirmed_by_cooper;
    const badge = row.querySelector("[data-badge]");
    badge.textContent = ok ? "Confirmed" : "Unconfirmed";
    badge.className = "badge " + (ok ? "badge-ok" : "badge-warn");
    badge.title = ok ? "" : "Blocks live runs until confirmed. Current: " + item.current;
    form.elements["confirm_" + item.key].checked = ok;
  }
}

function fillDelta(delta) {
  form.elements.delta_enabled.checked = !!delta.enabled;
  form.elements.delta_source.value = delta.source;
  for (const name of DELTA_NUMBERS) form.elements["delta_" + name].value = delta[name];
}

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
  saved = data;
  noteEl.textContent = data.default_stop_note || "";
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
  fillDelta(data.delta_filter || {});
  renderConfirmations(data);
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
  payload.delta_filter = { enabled: form.elements.delta_enabled.checked, source: form.elements.delta_source.value };
  for (const name of DELTA_NUMBERS) {
    const raw = form.elements["delta_" + name].value;
    payload.delta_filter[name] = raw === "" ? null : Number(raw);
  }
  payload.setting_confirmations = {};
  for (const item of (saved && saved.default_settings) || []) {
    payload.setting_confirmations[item.key] = { confirmed_by_cooper: form.elements["confirm_" + item.key].checked };
  }
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
  const payload = collect();
  const { ok, body } = await CT.post("/api/rules", payload);
  if (ok) {
    // A ticked box that comes back false was reset because its value changed.
    const reset = (body.default_settings || []).filter((item) =>
      payload.setting_confirmations[item.key] &&
      payload.setting_confirmations[item.key].confirmed_by_cooper && !item.confirmed_by_cooper);
    fill(body);
    msg.textContent = "Saved and reloaded." + (reset.length
      ? " Confirmation reset because the value changed: " + reset.map((i) => i.label).join(", ") + ". Re-confirm with Cooper."
      : "");
    msg.className = reset.length ? "msg warn" : "msg ok";
  } else {
    msg.textContent = body.error || "save rejected";
    msg.className = "msg is-error";
  }
});

document.getElementById("btn-reload").addEventListener("click", load);
load();

// -- alerts ---------------------------------------------------------------
function renderAlerts(body) {
  const channels = document.getElementById("alert-channels");
  channels.replaceChildren(...(body.channels || []).map((c) =>
    CT.el("li", { class: c.configured ? "ok" : "hint", text: c.channel + ": " + c.detail })));
  const recent = document.getElementById("alert-recent");
  if (!body.recent || !body.recent.length) return;
  recent.replaceChildren(...body.recent.map((a) =>
    CT.el("li", { text: CT.fmtTime(a.at) + " · " + a.title + " · " + a.message })));
}

async function loadAlerts() {
  const { ok, body } = await CT.get("/api/alerts");
  if (ok) renderAlerts(body);
}

document.getElementById("btn-test-alert").addEventListener("click", async () => {
  const out = document.getElementById("alert-msg");
  out.textContent = "Sending…";
  out.className = "msg";
  const { ok, body } = await CT.post("/api/alerts/test", {});
  if (!ok) {
    out.textContent = body.error || "Could not send the test alert.";
    out.className = "msg is-error";
    return;
  }
  const sent = body.results.filter((r) => r.ok).map((r) => r.channel);
  const failed = body.results.filter((r) => !r.ok).map((r) => r.channel + " (" + r.detail + ")");
  out.textContent = (sent.length ? "Sent to: " + sent.join(", ") + ". " : "Nothing was sent. ") +
    (failed.length ? "Not sent: " + failed.join("; ") : "");
  out.className = "msg " + (sent.length ? (failed.length ? "warn" : "ok") : "is-error");
  renderAlerts(body);
});

loadAlerts();
