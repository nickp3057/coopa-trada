"use strict";
// Dashboard: run status poll, start/stop, live panel, token status.

const btnStart = document.getElementById("btn-start");
const btnStop = document.getElementById("btn-stop");
const btnStartLive = document.getElementById("btn-start-live");
const liveConfirm = document.getElementById("live-confirm");
const runDate = document.getElementById("run-date");
const statusLine = document.getElementById("run-status");
const bannerEl = document.getElementById("banner");
const liveEnv = document.getElementById("live-env");
const liveSettings = document.getElementById("live-settings");
const liveError = document.getElementById("live-error");
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

function liveAllowed(s) {
  return s.status !== "running" && !!s.live_env_enabled && !s.live_blocked &&
    liveConfirm.value === confirmPhrase;
}

function renderLiveSettings(s) {
  if (!liveSettings) return;
  liveSettings.replaceChildren();
  liveSettings.className = "status-line " + (s.live_blocked ? "is-error" : "ok");
  if (s.live_block_error) {
    liveSettings.textContent = "Live runs are blocked. " + s.live_block_error;
    return;
  }
  if (!s.live_blocked) {
    liveSettings.textContent = "All settings are confirmed with Cooper.";
    return;
  }
  liveSettings.append(
    "Live runs are blocked: these settings are not confirmed with Cooper. ",
    CT.el("a", { href: "/settings", text: "Confirm them in Settings." }),
  );
  const list = CT.el("ul", { class: "live-blockers" });
  for (const item of s.unconfirmed_settings) {
    list.append(CT.el("li", { text: item.label + " (currently " + item.current + ")" }));
  }
  liveSettings.append(list);
}

function renderStatus(s) {
  const running = s.status === "running";
  btnStart.disabled = running;
  btnStop.disabled = !running;
  lastStatus = s;
  if (btnStartLive) btnStartLive.disabled = !liveAllowed(s);
  renderLiveSettings(s);

  let txt = running ? "RUNNING" : "idle";
  if (running && s.last_started) txt += " since " + CT.fmtTime(s.last_started);
  if (!running && s.last_finished) {
    txt += " · last run " + CT.fmtTime(s.last_finished);
    if (s.last_result) txt += " (" + s.last_result + ")";
  }
  if (s.last_error) txt += " · ERROR: " + s.last_error;
  statusLine.textContent = txt;
  statusLine.classList.toggle("is-error", !!s.last_error);
  statusLine.classList.toggle("is-running", running);

  if (s.live_confirm_phrase) {
    confirmPhrase = s.live_confirm_phrase;
    liveConfirm.placeholder = confirmPhrase;
  }
  if (liveEnv) {
    liveEnv.textContent = s.live_env_enabled
      ? "LIVE_TRADING=1 is set in this environment. A live run is possible once you type the phrase."
      : "LIVE_TRADING is not set. Live runs are blocked. Start the server with LIVE_TRADING=1 to allow them.";
    liveEnv.classList.toggle("ok", !!s.live_env_enabled);
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
    " (" + tokenExpiresAt.toLocaleString() + ")";
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
  const { ok, body } = await CT.post("/api/run/start", { date: runDate.value || null });
  if (!ok) statusLine.textContent = "could not start: " + (body.error || "unknown error");
  pollStatus();
});

btnStop.addEventListener("click", async () => {
  btnStop.disabled = true;
  await CT.post("/api/run/stop", {});
  pollStatus();
});

if (liveConfirm) {
  liveConfirm.addEventListener("input", () => {
    if (btnStartLive) btnStartLive.disabled = !lastStatus || !liveAllowed(lastStatus);
  });
}

if (btnStartLive) {
  btnStartLive.addEventListener("click", async () => {
    liveError.textContent = "";
    const { ok, body } = await CT.post("/api/run/start", {
      live: true,
      confirm: liveConfirm.value,
      date: runDate.value || null,
    });
    if (!ok) liveError.textContent = body.error || "could not start live run";
    else liveConfirm.value = "";
    pollStatus();
  });
}

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
