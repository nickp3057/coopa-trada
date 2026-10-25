# Running Coopa Trada on a server (AWS Lightsail)

The server runs **one separate bot per person** plus a **login website**:

```
https://yourname.duckdns.org  ->  Caddy (HTTPS)  ->  login website  ->  Cooper's bot  (his Schwab, settings, trades)
                                                                    ->  Nick's bot    (his Schwab, settings, trades)
```

When Cooper logs in he sees only his bot, with his Schwab login, settings and trade
log. When you log in you see only yours. The bots share no files and no Schwab login,
and each has its own Telegram bot, so nothing one person does can touch the other's
account. (Running the same Schwab account in both is refused automatically.)

**Cost:** about $7/month for a 1 GB Lightsail server, billed monthly with no upfront
fee and no contract. The DuckDNS address, the HTTPS certificate, Telegram and ntfy are
free; the Claude picture reader costs a few dollars a year.

---

## 1. Create the server

1. Go to **lightsail.aws.amazon.com** and sign in (or create an AWS account; it
   asks for a card and a phone check).
2. **Create instance** → region near you (e.g. **Oregon**) → **Linux/Unix** →
   **OS Only** → **Ubuntu 24.04 LTS**.
3. Plan: **$7 (1 GB memory, 2 vCPU, 40 GB)** on the **dual-stack** tab.
4. Name: `coopa-trada` → **Create instance**. Wait until it says **Running**.

## 2. Give it a fixed address and open the web ports

1. Open the instance → **Networking** tab.
2. **Attach static IP** → create one (free while attached) → note the address,
   e.g. `44.230.12.34`.
3. Under **IPv4 Firewall**, **Add rule**: **HTTPS** (443). Add another: **HTTP** (80;
   only used to get the certificate). Keep **SSH** (22).

## 3. Get a free name at DuckDNS

1. Go to **duckdns.org**, sign in (Google or GitHub).
2. Type a name (e.g. `coopatrada`) → **add domain**.
3. In the **current ip** box next to it, paste the static IP from step 2 → **update ip**.

Your site will be `https://coopatrada.duckdns.org`. The IP never changes, so this
never needs updating again.

## 4. Install the code

On the instance page click **Connect using SSH** (a terminal opens in the browser),
then:

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/nickp3057/coopa-trada.git
cd coopa-trada
bash packaging/server/install.sh
```

The repository is private: when git asks, the user name is your GitHub name and the
password is a **GitHub personal access token** (github.com → Settings → Developer
settings → Fine-grained tokens → read access to `coopa-trada` → copy it).

## 5. Add each person

```bash
bash packaging/server/add-person.sh cooper 8771
bash packaging/server/add-person.sh nick 8772
```

**Or send a sign-up link** instead (the person picks their own password and sets up the
6-digit codes on their phone, from anywhere):

```bash
bash packaging/server/add-person.sh cooper 8771 --invite
```

It prints a link like `https://coopatrada.duckdns.org/setup/...`. Text it to them
privately. It works **once** and expires after **24 hours**; make a new one with
`.venv/bin/python -m coopa_trada.gateway --config ~/coopa-data/gateway.yaml invite cooper --port 8771`.
You still choose the name and bot; the link can't make any other login.

Without `--invite`, each one asks for that person's **website password** (12+ characters, twice), then
shows a **QR code**. That person opens an authenticator app on their phone
(Google Authenticator, Microsoft Authenticator, 1Password…), taps **+**, scans it, and
types the 6-digit code to check it. If Cooper isn't with you, press Enter to skip the
check and send him the setup key it prints, privately (he chooses "enter a setup key").

## 6. Fill in each person's settings

The Schwab lines can be filled in from the website instead: log in → **Settings →
Your Schwab app** (saved to that person's own file only). Everything else, and
`LIVE_TRADING`, is set in the file on the server:

```bash
nano ~/coopa-data/cooper/.env
```

| Line | Cooper's file | Your file |
|---|---|---|
| `SCHWAB_API_KEY`, `SCHWAB_APP_SECRET` | from **his** Schwab developer app | from **your** Schwab developer app |
| `SCHWAB_CALLBACK_URL` | `https://127.0.0.1:8182` (as registered at Schwab) | same |
| `SCHWAB_ACCOUNT_ID` | his account number | yours |
| `ANTHROPIC_API_KEY` | your `coopa-trada` Claude key | same key |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` | **his** bot, his chat ID | a second bot of your own, or leave blank |
| `NTFY_TOPIC` | his alert channel | yours |
| `LIVE_TRADING`, `AUTO_START_LIVE` | `0` until the practice days look right | same |

Save with Ctrl-O, Enter, Ctrl-X, then restart that person's bot:

```bash
sudo systemctl restart coopa-trada@cooper
```

Leave `DASHBOARD_PASSWORD` blank: the login website handles logins. Each Telegram
bot can serve only one person's copy, so make a second bot (docs/TELEGRAM_SETUP.md) if
you want Telegram for yours too.

## 7. Turn on HTTPS

```bash
bash packaging/server/setup-https.sh coopatrada.duckdns.org
```

After a minute, open **https://coopatrada.duckdns.org** on any phone or computer:
the login page appears with a proper padlock.

## 8. First Schwab login (each person, once a week)

Log in to the website → Dashboard → **Reconnect Schwab** → **Open the Schwab login
page** → log in with **your own** Schwab user → **Allow**. The page then fails to load
(it points at `127.0.0.1:8182`); that's expected. Copy the whole address from the
address bar, paste it into the dashboard, click **Finish**. Schwab requires this every
7 days; the dashboard and the phone alerts warn 2 days ahead.

## 9. Check it

```bash
cd ~/coopa-data/cooper && ~/coopa-trada/.venv/bin/python -m coopa_trada readonly-check
```

OK lines only (no account numbers or amounts). Then **Settings → Alerts → Send test
alert**, and a test screenshot to the Telegram bot.

## Everyday

Nothing. At 7:00 AM MT on trading days each bot starts its run, watches what it holds,
buys when its picks are confirmed, and sells by its own settings.

## Handy commands

| What | Command |
|---|---|
| Is everything running? | `systemctl status coopa-gateway coopa-trada@cooper coopa-trada@nick caddy` |
| A bot's log | `journalctl -u coopa-trada@cooper -n 50` (add `-f` to follow) |
| Update the code | `cd ~/coopa-trada && git pull && bash packaging/server/install.sh` (not during market hours) |
| New website password | `.venv/bin/python -m coopa_trada.gateway --config ~/coopa-data/gateway.yaml reset-password cooper` then `sudo systemctl restart coopa-gateway` |
| New phone for the 6-digit codes | same, with `reset-2fa cooper` |
| Remove a person | `sudo systemctl disable --now coopa-trada@NAME`, then `remove-user NAME` |

Too many wrong tries locks a name for 15 minutes. A password or 2FA reset logs that
person out everywhere.

**Backups:** instance → **Snapshots** → **Enable automatic snapshots** (a small extra
monthly cost). Everything that matters is in `~/coopa-data/`.
