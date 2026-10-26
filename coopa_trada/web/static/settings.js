"use strict";
// Settings: load the rules into the form, save them, and keep the Schwab app
// details (a separate form). Every value is validated server-side by the same
// code that loads rules.yaml. Percentages are shown as %, stored as fractions.

const form = document.getElementById("rules-form");
const msg = document.getElementById("rules-msg");
const dirty = document.getElementById("dirty");
const savebar = document.getElementById("savebar");
let hideTimer = null;

// The save bar only shows while there is something to save (or a save just happened).
function showSavebar(on) {
  clearTimeout(hideTimer);
  savebar.hidden = !on;
}
const pathEl = document.getElementById("rules-path");
const fileWarn = document.getElementById("rules-file-warning");
const noteEl = document.getElementById("ambiguous-note");
const ewRows = document.getElementById("exit-window-rows");

const NUMBER_FIELDS = [
  "earliest_buy_minutes_before_close", "no_new_buys_minutes_before_close", "poll_interval_seconds",
  "trailing_stop_pct", "hard_stop_loss_pct", "take_profit_pct",
  "total_budget", "max_dollars_per_position",
  "exit_reprice_after_seconds", "exit_reprice_max_attempts", "contracts_per_pick",
  "cooper_drop_from_peak_pct", "cooper_drop_from_entry_pct",
  "profit_trail_points",
];
const TEXT_FIELDS = ["profit_trail_arm", "held_exit_start", "rank_delta_source", "exit_reference_price",
  "auto_start_time"];
const BOOL_FIELDS = ["stops_on_entry_day", "auto_start_daily", "next_day_time_exit"];
const DELTA_NUMBERS = ["min_abs_delta", "max_abs_delta"];
const EW_LABELS = { SINGLE_NAME: "Stocks", ETF: "ETFs" };

let saved = null; // last response from the server

const isPct = (input) => input && input.hasAttribute("data-pct");
const round = (n) => Number(Number(n).toFixed(6));

// "15:35" New York -> "1:35 PM MT"; 25 (minutes before a 4:00 PM ET close) -> "3:35 PM ET" -> MT.
function mtFromEt(value) {
  const s = String(value).trim();
  let minutes;
  if (/^\d+$/.test(s)) minutes = 16 * 60 - Number(s);
  else {
    const m = s.match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return "";
    minutes = Number(m[1]) * 60 + Number(m[2]);
  }
  minutes -= 120; // New York is two hours ahead of Mountain
  if (minutes < 0 || minutes >= 24 * 60) return "";
  const h = Math.floor(minutes / 60), mm = String(minutes % 60).padStart(2, "0");
  return `${((h + 11) % 12) + 1}:${mm} ${h < 12 ? "AM" : "PM"} MT`;
}

function updateHints() {
  document.querySelectorAll("[data-mt-for]").forEach((el) => {
    const input = form.elements[el.dataset.mtFor];
    const t = input && input.value !== "" ? mtFromEt(input.value) : "";
    el.textContent = t ? `= ${t} on a normal day` : "";
  });
  const deltaOn = form.elements.delta_enabled.checked;
  document.getElementById("delta-fields").classList.toggle("muted-block", !deltaOn);
  ewRows.querySelectorAll(".ew-row").forEach((row) => {
    const [s, e] = row.querySelectorAll("input");
    row.querySelector(".ew-mt").textContent = [mtFromEt(s.value), mtFromEt(e.value)].every(Boolean)
      ? `${mtFromEt(s.value)} – ${mtFromEt(e.value)}` : "";
  });
}

function renderExitWindow(ew) {
  ewRows.replaceChildren();
  for (const cls of Object.keys(ew)) {
    const [start, end] = ew[cls];
    const row = CT.el("div", { class: "ew-row" });
    row.append(CT.el("span", { class: "ew-label", text: EW_LABELS[cls] || cls }));
    const s = CT.el("input", { type: "text", value: String(start), "data-ew": cls, "data-end": "0", "aria-label": cls + " start" });
    const e = CT.el("input", { type: "text", value: String(end), "data-ew": cls, "data-end": "1", "aria-label": cls + " end" });
    row.append(s, CT.el("span", { class: "hint", text: "to" }), e, CT.el("span", { class: "help ew-mt" }));
    ewRows.append(row);
  }
}

function fill(data) {
  saved = data;
  pathEl.textContent = "Settings file: " + data.path;
  noteEl.textContent = data.default_stop_note || "";
  const trailNote = document.getElementById("profit-trail-note");
  if (trailNote) trailNote.textContent = data.profit_trail_note || "";
  fileWarn.hidden = !data.warning;
  fileWarn.textContent = data.warning ? "The saved settings file has a problem: " + data.warning : "";
  for (const name of NUMBER_FIELDS) {
    const input = form.elements[name];
    if (!input) continue;
    const v = data[name];
    input.value = v === null || v === undefined ? "" : (isPct(input) ? round(v * 100) : v);
  }
  for (const name of TEXT_FIELDS) {
    if (form.elements[name] && data[name] != null) form.elements[name].value = data[name];
  }
  for (const name of BOOL_FIELDS) {
    if (form.elements[name]) form.elements[name].checked = !!data[name];
  }
  const delta = data.delta_filter || {};
  form.elements.delta_enabled.checked = !!delta.enabled;
  form.elements.delta_source.value = delta.source || "schwab";
  for (const name of DELTA_NUMBERS) form.elements["delta_" + name].value = delta[name] ?? "";
  renderExitWindow(data.exit_window || {});
  updateHints();
  dirty.hidden = true;
  showSavebar(false);
}

function collect() {
  const payload = {};
  for (const name of NUMBER_FIELDS) {
    const input = form.elements[name];
    if (!input) continue;
    payload[name] = input.value === "" ? null : (isPct(input) ? round(Number(input.value) / 100) : Number(input.value));
  }
  for (const name of TEXT_FIELDS) {
    if (form.elements[name]) payload[name] = form.elements[name].value;
  }
  for (const name of BOOL_FIELDS) {
    if (form.elements[name]) payload[name] = form.elements[name].checked;
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
  return payload;
}

async function load() {
  msg.textContent = "";
  const { ok, body } = await CT.get("/api/rules");
  if (ok) fill(body);
  else { msg.textContent = "Could not load the settings."; msg.className = "msg is-error"; }
}

form.addEventListener("input", () => { dirty.hidden = false; msg.textContent = ""; showSavebar(true); updateHints(); });
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  msg.textContent = "Saving…";
  msg.className = "msg";
  const { ok, body } = await CT.post("/api/rules", collect());
  if (ok) {
    fill(body);
    msg.textContent = "Saved.";
    msg.className = "msg ok";
    showSavebar(true);
    hideTimer = setTimeout(() => { savebar.hidden = true; }, 2000);
  } else {
    msg.textContent = body.error || "Not saved.";
    msg.className = "msg is-error";
  }
});
document.getElementById("btn-reload").addEventListener("click", load);
window.addEventListener("beforeunload", (ev) => { if (!dirty.hidden) ev.preventDefault(); });
load();

// -- Schwab app details ---------------------------------------------------------
const keysForm = document.getElementById("keys-form");
const keysMsg = document.getElementById("keys-msg");

// Once the App Key and Secret are saved the card folds down to one line; "Change" opens it again.
function showKeysForm(open) {
  document.getElementById("keys-body").hidden = !open;
  document.getElementById("keys-summary").hidden = open;
  document.getElementById("btn-keys-edit").textContent = open ? "Close" : "Change";
}

function renderKeys(k, keepOpen) {
  const state = document.getElementById("keys-state");
  const ready = k.api_key_set && k.secret_set;
  document.getElementById("btn-keys-edit").hidden = !ready;
  document.getElementById("keys-summary").textContent = ready
    ? "App Key " + k.api_key_hint + (k.account_id_set ? " · account " + k.account_hint : "") + " · callback " + k.callback_url
    : "";
  showKeysForm(!ready || !!keepOpen);
  state.textContent = ready ? "Saved" : "Not set up";
  state.className = "pill " + (ready ? "pill-ok" : "pill-warn");
  keysForm.elements.api_key.value = "";
  keysForm.elements.app_secret.value = "";
  keysForm.elements.api_key.placeholder = k.api_key_set ? "saved " + k.api_key_hint + " (leave empty to keep)" : "paste your App Key";
  keysForm.elements.app_secret.placeholder = k.secret_set ? "saved (leave empty to keep)" : "paste your Secret";
  keysForm.elements.callback_url.value = k.callback_url || "";
  keysForm.elements.account_id.value = "";
  keysForm.elements.account_id.placeholder = k.account_id_set ? "saved " + k.account_hint : "e.g. 12345678";
}

async function loadKeys() {
  const { ok, body } = await CT.get("/api/schwab/keys");
  if (ok) renderKeys(body);
}

keysForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  keysMsg.textContent = "Saving…";
  keysMsg.className = "msg";
  const data = Object.fromEntries(new FormData(keysForm).entries());
  const { ok, body } = await CT.post("/api/schwab/keys", data);
  if (ok) {
    renderKeys(body, true);
    keysMsg.textContent = body.message;
    keysMsg.className = "msg ok";
  } else {
    keysMsg.textContent = body.error || "Not saved.";
    keysMsg.className = "msg is-error";
  }
});
document.getElementById("btn-keys-edit").addEventListener("click", () => {
  const open = document.getElementById("keys-body").hidden;
  showKeysForm(open);
  keysMsg.textContent = "";
});
loadKeys();

// -- alerts -----------------------------------------------------------------------
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
