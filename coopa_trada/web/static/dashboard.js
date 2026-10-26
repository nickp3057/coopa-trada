"use strict";
// Dashboard: run status poll, start/stop, live panel, token status.

const btnStart = document.getElementById("btn-start");
const btnStop = document.getElementById("btn-stop");
const liveSwitch = document.getElementById("live-switch");
const liveArm = document.getElementById("live-arm");
const btnLiveOn = document.getElementById("btn-live-on");
const liveMsg = document.getElementById("live-msg");
const liveConfirm = document.getElementById("live-confirm");
const runDate = document.getElementById("run-date");
const statusLine = document.getElementById("run-status");
const bannerEl = document.getElementById("banner");
const liveSettings = document.getElementById("live-settings");
const tokenStatus = document.getElementById("token-status");
const lockPanel = document.getElementById("lock-panel");
const lockText = document.getElementById("lock-text");
let staleLocks = [];
const schwabBanner = document.getElementById("schwab-banner");
const schwabState = document.getElementById("schwab-state");
const schwabCountdown = document.getElementById("schwab-countdown");
const btnReconnect = document.getElementById("btn-reconnect");
const reconnectSteps = document.getElementById("reconnect-steps");
const reconnectLink = document.getElementById("reconnect-link");
const reconnectUrl = document.getElementById("reconnect-url");
const reconnectMsg = document.getElementById("reconnect-msg");
let tokenExpiresAt = null;

let confirmPhrase = "ENABLE LIVE TRADING";
let lastStatus = null;

// Start button and switch follow the server: real money only when allowed there AND switched on.
function renderSwitch(s) {
  const on = !!s.live_switch_on;
  if (!liveArm.hidden && !on) liveSwitch.checked = true;  // typing the phrase: keep it looking "turning on"
  else liveSwitch.checked = on;
  document.getElementById("live-switch-label").textContent = on ? (s.live_env_enabled ? "On" : "On (blocked)") : "Off";
  document.getElementById("live-panel").classList.toggle("is-armed", !!s.live_armed);
  const armed = !!s.live_armed;
  btnStart.textContent = armed ? "Start today’s run with REAL MONEY" : "Start today’s practice run";
  btnStart.classList.toggle("btn-danger", armed);
  btnStart.classList.toggle("btn-primary", !armed);
  document.getElementById("run-date-label").hidden = armed;  // a real run is always for today
  document.getElementById("start-mode-hint").textContent = armed
    ? "Real money is ON: Start and the 7:00 AM automatic run place real orders. Turn it off below."
    : on ? "Real money is switched on, but this bot isn't allowed to trade real money on the server yet, so runs are practice."
      : "Real money is off, so every run is practice: nothing is sent to Schwab.";
}

function renderLiveSettings(s) {
  if (!liveSettings) return;
  liveSettings.replaceChildren();
  if (s.settings_error) {
    liveSettings.className = "status-line is-error";
    liveSettings.textContent = "Runs are blocked: " + s.settings_error;
    return;
  }
  liveSettings.className = "status-line";
  liveSettings.append("A run trades with these settings (change them in ",
    CT.el("a", { href: "/settings", text: "Settings" }), "):");
  const list = CT.el("ul", { class: "live-blockers" });
  for (const item of s.settings_summary || []) {
    list.append(CT.el("li", { text: item.label + ": " + item.current }));
  }
  liveSettings.append(list);
}

function renderLiveChecks(s) {
  const list = document.getElementById("live-checks");
  if (!list || !s.live_checks) return;
  list.replaceChildren(...s.live_checks.map((c) => {
    const li = CT.el("li", { class: c.ok ? "check-ok" : "check-no" });
    li.append(CT.el("span", { class: "check-mark", text: c.ok ? "✓" : "✗", "aria-hidden": "true" }),
      CT.el("span", { class: "check-label", text: c.label }));
    if (!c.ok && c.fix) li.append(CT.el("span", { class: "check-fix", text: c.fix }));
    return li;
  }));
}

function renderStatus(s) {
  renderLiveChecks(s);
  const running = s.status === "running";
  btnStart.disabled = running;
  btnStop.disabled = !running;
  lastStatus = s;
  renderSwitch(s);
  renderLiveSettings(s);

  let txt = running ? "RUNNING" : "idle";
  if (running && s.last_started) txt += " since " + CT.fmtTime(s.last_started);
  if (running && s.activity) txt += " · " + s.activity;
  if (!running && s.last_finished) {
    txt += " · last run " + CT.fmtTime(s.last_finished);
    if (s.last_result) txt += " (" + s.last_result + ")";
  }
  if (s.last_error) txt += " · ERROR: " + s.last_error;
  statusLine.textContent = txt;
  statusLine.classList.toggle("is-error", !!s.last_error);
  statusLine.classList.toggle("is-running", running);
  renderAwake(s.keep_awake);

  if (s.live_confirm_phrase) {
    confirmPhrase = s.live_confirm_phrase;
    document.getElementById("live-phrase").textContent = confirmPhrase;
  }
}

function renderAwake(k) {
  const el = document.getElementById("awake-status");
  if (!k || (!k.active && !k.error)) { el.hidden = true; return; }
  el.hidden = false;
  if (k.error) {
    el.textContent = "Could not keep the Mac awake (" + k.error + "). Keep it plugged in and stop it sleeping in System Settings.";
    el.className = "status-line is-error";
  } else {
    el.textContent = "Keeping Mac awake: " + k.reason + ". Keep it plugged in with the lid open.";
    el.className = "status-line ok";
  }
}

async function pollStatus() {
  const { ok, body } = await CT.get("/api/run/status");
  if (ok) renderStatus(body);
}

async function loadBanner() {
  const { ok, body } = await CT.get("/api/mode");
  if (ok) bannerEl.textContent = body.banner.trim();
}

const STATE_CLASS = { connected: "ok", expiring_soon: "warn", expired: "is-error", not_set_up: "is-error" };

function fmtRemaining(ms) {
  if (ms <= 0) return "expired";
  const mins = Math.floor(ms / 60000);
  const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
  if (d > 0) return `${d} day${d === 1 ? "" : "s"} ${h} hr`;
  if (h > 0) return `${h} hr ${m} min`;
  return `${m} min`;
}

function renderCountdown() {
  if (!tokenExpiresAt) { schwabCountdown.textContent = ""; return; }
  schwabCountdown.textContent = "Login expires in " + fmtRemaining(tokenExpiresAt - Date.now()) +
    " (" + CT.fmtTime(tokenExpiresAt) + ")";
}

async function loadToken() {
  const { ok, body } = await CT.get("/api/token");
  if (!ok) return;
  const cls = STATE_CLASS[body.state] || "is-error";
  schwabState.textContent = "Schwab: " + body.label;
  schwabState.className = "state-pill " + cls;
  tokenStatus.textContent = body.message + (body.app_configured ? "" :
    " The Schwab app key and secret are not in the .env file yet, so Reconnect will not work until they are.");
  tokenExpiresAt = body.expires_at ? new Date(body.expires_at) : null;
  renderCountdown();
  if (body.needed_until_text) {
    tokenStatus.textContent += " The next run needs Schwab until " + body.needed_until_text +
      (body.covers_next_run ? " (covered)." : ", and this login does not last that long.");
  }
  schwabBanner.hidden = !body.warning;
  if (body.warning) {
    // Not lasting through the next run's hold blocks it, so it is an error, not a warning.
    const blocking = body.state !== "connected" && body.state !== "expiring_soon" || !body.covers_next_run;
    schwabBanner.className = "alert " + (blocking ? "alert-error" : "alert-warn");
    schwabBanner.textContent = "Schwab: " + body.label + ". " + (body.hold_message || body.message);
  }
}

btnReconnect.addEventListener("click", async () => {
  reconnectMsg.textContent = "";
  const { ok, body } = await CT.post("/api/schwab/reconnect/begin", {});
  if (!ok) {
    reconnectMsg.textContent = body.error || "Could not start the Schwab login.";
    reconnectMsg.className = "msg is-error";
    return;
  }
  reconnectLink.href = body.authorization_url;
  document.getElementById("reconnect-callback").textContent = body.callback_url;
  reconnectUrl.value = "";
  reconnectSteps.hidden = false;
});

document.getElementById("btn-reconnect-finish").addEventListener("click", async () => {
  reconnectMsg.textContent = "Checking with Schwab…";
  reconnectMsg.className = "msg";
  const { ok, body } = await CT.post("/api/schwab/reconnect/finish", { redirect_url: reconnectUrl.value });
  if (ok) {
    reconnectSteps.hidden = true;
    reconnectMsg.textContent = "Reconnected. Schwab: " + body.label + ".";
    reconnectMsg.className = "msg ok";
  } else {
    reconnectMsg.textContent = body.error || "Reconnect failed.";
    reconnectMsg.className = "msg is-error";
  }
  loadToken();
});

async function loadAttention() {
  const banner = document.getElementById("attention-banner");
  const { ok, body } = await CT.get("/api/attention");
  if (!ok) return;
  const n = body.incidents.length;
  banner.hidden = n === 0;
  if (!n) return;
  const halted = body.incidents.filter((i) => i.outcome === "UNKNOWN").map((i) => i.osi_symbol);
  banner.replaceChildren(
    `${n} order${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} your attention` +
      (halted.length ? ` (halted: ${halted.join(", ")})` : "") + ". ",
    CT.el("a", { href: "/reconcile", text: "Review on the Reconcile page." }));
}

async function loadLocks() {
  const { ok, body } = await CT.get("/api/lock");
  if (!ok) return;
  staleLocks = body.stale;
  lockPanel.hidden = staleLocks.length === 0;
  lockText.textContent = staleLocks.map((l) =>
    `A previous run did not shut down cleanly (${l.purpose} lock: ${l.description}). New runs are blocked until it is cleared.`
  ).join(" ");
}

document.getElementById("btn-clear-lock").addEventListener("click", async () => {
  const messages = [];
  for (const lock of staleLocks) {
    const { body } = await CT.post("/api/lock/clear", { path: lock.path });
    messages.push(body.message || body.error);
  }
  statusLine.textContent = messages.join(" ");
  loadLocks();
});

btnStart.addEventListener("click", async () => {
  btnStart.disabled = true;
  const armed = !!(lastStatus && lastStatus.live_armed);
  // A real-money run is always for today, whatever the practice date box says.
  const { ok, body } = await CT.post("/api/run/start", armed ? { live: true, date: null } : { date: runDate.value || null });
  if (!ok) statusLine.textContent = "could not start: " + (body.error || "unknown error");
  pollStatus();
});

btnStop.addEventListener("click", async () => {
  btnStop.disabled = true;
  await CT.post("/api/run/stop", {});
  pollStatus();
});

function closeArm() {
  liveArm.hidden = true;
  liveConfirm.value = "";
  btnLiveOn.disabled = true;
}

async function setSwitch(payload) {
  const { ok, body } = await CT.post("/api/live/switch", payload);
  liveMsg.textContent = ok ? body.message : (body.error || "Could not change the switch.");
  liveMsg.className = "msg " + (ok ? (body.live_switch_on ? "warn" : "ok") : "is-error");
  if (ok) closeArm();
  await pollStatus();
  refreshModePill();
  loadBanner();
}

liveSwitch.addEventListener("change", () => {
  liveMsg.textContent = "";
  if (liveSwitch.checked) {
    // Turning on takes the typed phrase; nothing changes until "Turn real money on".
    liveArm.hidden = false;
    liveConfirm.focus();
  } else if (!liveArm.hidden) {
    closeArm();
  } else {
    setSwitch({ on: false });
  }
});
liveConfirm.addEventListener("input", () => { btnLiveOn.disabled = liveConfirm.value.trim() !== confirmPhrase; });
btnLiveOn.addEventListener("click", () => setSwitch({ on: true, confirm: liveConfirm.value.trim() }));
document.getElementById("btn-live-cancel").addEventListener("click", () => {
  closeArm();
  liveSwitch.checked = !!(lastStatus && lastStatus.live_switch_on);
});

loadBanner();
loadToken();
loadLocks();
loadAttention();
pollStatus();
setInterval(loadAttention, 15000);
setInterval(loadLocks, 15000);
setInterval(pollStatus, 3000);
setInterval(loadToken, 60000);
setInterval(renderCountdown, 30000);
