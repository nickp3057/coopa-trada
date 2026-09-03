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
const liveError = document.getElementById("live-error");
const tokenStatus = document.getElementById("token-status");
const tokenReconnect = document.getElementById("token-reconnect");

let confirmPhrase = "ENABLE LIVE TRADING";

function renderStatus(s) {
  const running = s.status === "running";
  btnStart.disabled = running;
  btnStop.disabled = !running;
  if (btnStartLive) {
    btnStartLive.disabled =
      running || !s.live_env_enabled || liveConfirm.value !== confirmPhrase;
  }

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

async function loadToken() {
  const { ok, body } = await CT.get("/api/token");
  if (!ok) return;
  let cls = "ok";
  let msg;
  if (!body.present) {
    cls = "is-error";
    msg = "No Schwab token on disk (" + body.path + ").";
  } else if (body.error) {
    cls = "warn";
    msg = "Token present at " + body.path + " — " + body.error;
  } else if (body.refresh_expires_in_days === null) {
    cls = "warn";
    msg = "Token present; freshness unknown.";
  } else if (body.refresh_expires_in_days <= 0) {
    cls = "is-error";
    msg = "Token refresh expired ~" + (-body.refresh_expires_in_days).toFixed(1) + " day(s) ago. Reconnect.";
  } else if (body.stale) {
    cls = "warn";
    msg = "Token refresh expires in ~" + body.refresh_expires_in_days.toFixed(1) + " day(s). Reconnect soon.";
  } else {
    msg = "Token OK — refresh expires in ~" + body.refresh_expires_in_days.toFixed(1) + " day(s) (created ~" + body.creation_age_days.toFixed(1) + " day(s) ago).";
  }
  tokenStatus.textContent = msg;
  tokenStatus.className = "status-line " + cls;
  if (body.reconnect_hint && (!body.present || body.stale || body.error)) {
    tokenReconnect.textContent = body.reconnect_hint;
    tokenReconnect.hidden = false;
  } else {
    tokenReconnect.hidden = true;
  }
}

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
    if (btnStartLive) btnStartLive.disabled = liveConfirm.value !== confirmPhrase;
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
pollStatus();
setInterval(pollStatus, 3000);
setInterval(loadToken, 60000);
