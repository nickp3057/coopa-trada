# Running Coopa Trada on a server

A small always-on Linux server replaces the Mac: no lid to keep open, no sleep,
no "plugged in" rule. The bot starts the day's run by itself at 7:00 AM MT, reads
Cooper's Telegram screenshots, and alerts your phone through ntfy.

**What it costs.** One small server, about **$5–12 a month** (AWS Lightsail,
DigitalOcean or Hetzner all work; check their current prices). 1 GB of memory is
enough. The Claude picture reader costs cents a month (one screenshot a day).
Telegram, ntfy and Tailscale are free. Leave the server on all the time; that
costs the same as turning it off at night.

**What it doesn't change.** Schwab still asks for a new login every 7 days (one
minute, through the dashboard, from any browser on your phone or laptop).

This guide uses **AWS Lightsail** with **Ubuntu 24.04**. Other providers are the
same from step 2 on.

---

## 1. Create the server (Lightsail)

1. Sign in to the AWS console and open **Lightsail** → **Create instance**.
2. Region: one near you (for example Oregon, `us-west-2`).
3. Platform **Linux/Unix**, blueprint **OS Only → Ubuntu 24.04 LTS**.
4. Plan: **1 GB memory** (dual-stack, with a public IPv4 address).
5. Name it `coopa-trada` and click **Create instance**.
6. When it says **Running**, click the terminal icon (**Connect using SSH**). A
   terminal opens in the browser. Every command below goes there.

Leave the Lightsail firewall as it is (SSH only). The dashboard is never opened to
the internet; step 6 reaches it privately.

## 2. Install Python and git, and get the code

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo timedatectl set-timezone America/Denver   # only affects log timestamps
git clone https://github.com/nickp3057/coopa-trada.git
cd coopa-trada
```

(The repository is private: GitHub asks for your user name and a personal access
token with read access. Or copy the folder over with `scp`.)

## 3. Install the app as a service

```bash
bash packaging/server/install.sh
```

This creates `.venv`, installs the pinned packages, creates `config/rules.yaml`
and an empty `.env` (only your user can read it), and installs the
`coopa-trada` service. The service starts on boot and restarts after a crash.

## 4. Fill in `.env`

```bash
nano .env
```

| Line | Value |
|---|---|
| `SCHWAB_API_KEY`, `SCHWAB_APP_SECRET` | From the Schwab Developer Portal (docs/SCHWAB_SETUP.md) |
| `SCHWAB_CALLBACK_URL` | Exactly as registered at Schwab, normally `https://127.0.0.1:8182` |
| `SCHWAB_ACCOUNT_ID` | The account to trade (recommended) |
| `NTFY_TOPIC` | Your long random ntfy channel (COOPER_README section 2c). **On a server this is the only alert channel**: there are no Mac notifications. |
| `ANTHROPIC_API_KEY` | For reading screenshots (console.anthropic.com) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` | docs/TELEGRAM_SETUP.md |
| `DASHBOARD_PASSWORD` | A long password (12+ characters) for the dashboard |
| `LIVE_TRADING` | Leave `0` until the practice days look right |
| `AUTO_START_LIVE` | Leave `0`; set to `1` (with `LIVE_TRADING=1`) when the automatic daily run should trade real money |

Save with Ctrl-O, Enter, Ctrl-X. Then:

```bash
sudo systemctl restart coopa-trada
journalctl -u coopa-trada -n 30      # the last 30 log lines
```

## 5. Copy your settings and Schwab login (optional)

If the Mac already has a working setup, copy `config/rules.yaml` and
`schwab_token.json` from the Mac's app folder into `~/coopa-trada/` on the server
(the Lightsail SSH window has an upload button), then restart the service. Copy
`trada.db` too if positions are open, and **stop the Mac app first**: only one
computer may run the bot for an account. Otherwise do the Schwab login in step 7.

## 6. Reach the dashboard privately with Tailscale

Tailscale makes a private network between your devices. The dashboard stays on
the server's own `127.0.0.1` and is never exposed to the internet.

1. Make a free account at tailscale.com and install Tailscale on your phone and
   laptop (sign in on each).
2. On the server:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up              # open the link it prints and approve the server
   sudo tailscale serve --bg 8770
   ```

3. `tailscale serve` prints an address like `https://coopa-trada.tail1234.ts.net`.
   Open it on your phone or laptop (with Tailscale on). The browser asks for the
   dashboard password; any user name works.

## 7. First Schwab login and weekly reconnect

On the dashboard: **Reconnect Schwab** → **Open the Schwab login page** → log in →
**Allow**. The page then fails to load (it points at `127.0.0.1:8182`). That is
expected: copy the whole address from the address bar, paste it into the
dashboard, click **Finish**. Repeat every week (the dashboard and your phone warn
you 2 days ahead).

## 8. Check it

```bash
.venv/bin/python -m coopa_trada readonly-check
```

It should print OK lines only (no account numbers or amounts). Then send a test
alert from **Settings → Alerts**, and a test screenshot to the Telegram bot.

## Everyday

Nothing to do. At 7:00 AM MT on trading days the run starts by itself, watches
anything held from yesterday, buys when Cooper confirms the picks, and sells by the
rules. The dashboard shows what it is doing. To stop a day: press **Stop** (it
won't restart that day).

## Updating

```bash
cd ~/coopa-trada && git pull && bash packaging/server/install.sh
```

Not while a run is active during market hours; evenings or weekends are best.

## If something goes wrong

| Symptom | Check |
|---|---|
| Dashboard doesn't load | `sudo systemctl status coopa-trada`; `journalctl -u coopa-trada -n 50`; is Tailscale on on your device? |
| "password required" every time | The password is `DASHBOARD_PASSWORD` in `.env`; restart the service after changing it |
| No phone alerts | `NTFY_TOPIC` in `.env`; Settings → Alerts → Send test alert |
| Telegram silent | docs/TELEGRAM_SETUP.md, Troubleshooting |
| A real-money run did not shut down cleanly | After a crash or reboot mid-run: check the Reconcile page and Schwab, then **Clear stale lock** on the dashboard |

**Backups.** `trada.db` is the trade log. Lightsail can take automatic daily
snapshots of the whole server (instance → Snapshots → Enable automatic snapshots,
a small extra monthly cost).
