# Running Coopa Trada on an AWS server

This guide moves Coopa Trada from the Mac to a small always-on Linux server at
Amazon Web Services (AWS). It is written for someone who has never run a server.
Take it one step at a time; every command can be copied as-is.

**What you end up with**

```
 your phone / Mac ──(Tailscale, private)──► AWS server (Ubuntu)
                                              └─ coopa-trada service
                                                   ├─ dashboard on 127.0.0.1:8770 (never public)
                                                   ├─ starts each trading day's run at 7:15 AM MT
                                                   ├─ Telegram intake (if set up)
                                                   └─ alerts to your phone (ntfy)
```

Nothing here turns on real trading. The server runs practice (DRY-RUN) runs until
you deliberately set the live opt-ins, exactly like the Mac app.

> **Run Coopa Trada in one place only.** Once the server is running, quit the Mac app
> for good (or at least never start a run there). The "one run per Schwab account"
> lock only works inside one computer, and a Telegram bot can only be read by one
> computer at a time.

---

## 1. Why systemd, not Docker

`deploy/` contains a **systemd service**, not a Dockerfile. systemd is the program
Ubuntu already uses to start everything at boot, so it adds nothing to install or learn:

- **Restart on crash**: `Restart=always` brings the app back 10 seconds after it dies
  (at most 5 times in 10 minutes, so a broken setup doesn't loop forever).
- **Restart on reboot**: `systemctl enable` starts it every time the server boots.
- **Logs** go to the system journal: `journalctl -u coopa-trada -f`.
- **Files stay plain files.** The database, `.env` and the Schwab login sit in one
  folder you can see, back up and lock down. Docker would put them behind volumes,
  file-ownership mapping and a container time zone, which are three more things to get
  wrong for one small Python app on one machine.
- **Less memory.** No Docker engine running next to the app, which matters on a 1 GB machine.

Docker would earn its keep if the app ran on several machines or needed tricky
system libraries. It doesn't.

## 2. Which AWS machine (instance) to pick

The app (Python, pandas, the market calendar, Flask, the Claude and Schwab clients)
peaks around **170 MB** of memory, measured. Ubuntu itself uses about 250–300 MB.

| Instance | Memory | Verdict |
|---|---|---|
| t4g.nano | 0.5 GB | Too small: installing pandas and a run at the same time can run out of memory. |
| **t4g.micro** | **1 GB** | **Smallest that fits.** The setup script adds a 1 GB swap file as a safety net. |
| t4g.small | 2 GB | Comfortable if you'd rather never think about memory. |

**Recommendation: t4g.micro** with **Ubuntu Server 24.04 LTS (64-bit Arm)** and a
**10 GB gp3** disk. "t4g" machines use Amazon's Arm (Graviton) chips, which are cheaper
than the Intel "t3" ones; everything this app uses runs on Arm. It costs in the region
of $6–8 a month; check the AWS pricing page for your region, since prices differ.

Pick a US region (for example **us-west-2, Oregon**). Any US region is fine for Schwab.

## 3. Create the server

1. Make an AWS account at <https://aws.amazon.com> and sign in to the **Console**.
2. Top right, choose the region (e.g. *US West (Oregon)*).
3. Search for **EC2** → **Launch instance**.
   - **Name**: `coopa-server`
   - **Application and OS image**: *Ubuntu*, **Ubuntu Server 24.04 LTS**,
     architecture **64-bit (Arm)**.
   - **Instance type**: `t4g.micro`
   - **Key pair**: *Create new key pair*, name `coopa`, type ED25519, format `.pem`.
     Your browser downloads `coopa.pem`. Keep it safe; it is the key to the server.
   - **Network settings** → *Edit*: allow SSH **from My IP** only. Do **not** add HTTP
     or HTTPS rules. Nothing else is ever opened to the internet.
   - **Configure storage**: 10 GiB, gp3. Under *Advanced*, make sure **Encrypted** is on.
   - **Advanced details** → *Metadata version*: **V2 only (token required)**.
4. **Launch instance**. After a minute, open it and copy its **Public IPv4 address**.

## 4. Connect and copy the code

On your Mac, in Terminal:

```bash
mv ~/Downloads/coopa.pem ~/.ssh/coopa.pem
chmod 600 ~/.ssh/coopa.pem
ssh -i ~/.ssh/coopa.pem ubuntu@<PUBLIC-IP>     # type "yes" the first time
```

You are now on the server (the prompt says `ubuntu@ip-...`). Type `exit` to come back.

Copy the code from your Mac (from the project folder). This leaves out the Mac-only
Python environment, build output and **all secrets**: those are handled separately below.

```bash
cd ~/Developer/projects/coopa-trada
rsync -av -e "ssh -i ~/.ssh/coopa.pem" \
  --exclude .venv --exclude build --exclude dist --exclude .git --exclude '__pycache__' \
  --exclude .env --exclude schwab_token.json --exclude '*.db*' --exclude imports \
  ./ ubuntu@<PUBLIC-IP>:/tmp/coopa-trada/
ssh -i ~/.ssh/coopa.pem ubuntu@<PUBLIC-IP> \
  'sudo mkdir -p /opt/coopa-trada && sudo cp -a /tmp/coopa-trada/. /opt/coopa-trada/ && rm -rf /tmp/coopa-trada'
```

## 5. Run the setup script

```bash
ssh -i ~/.ssh/coopa.pem ubuntu@<PUBLIC-IP>
sudo bash /opt/coopa-trada/deploy/setup_server.sh
```

It takes a few minutes. It:

1. sets the server's **time zone to America/Denver** (Mountain) and makes sure the
   clock syncs automatically. The app does its own market-time maths in New York time
   either way; this just makes logs and `date` read in your time;
2. installs Python;
3. adds a **1 GB swap file** on a 1 GB machine;
4. creates a user called **`coopa`** that runs the app and can't log in;
5. installs the app's Python packages into `/opt/coopa-trada/.venv`;
6. copies the example rules to `config/rules.yaml` if there isn't one (every setting
   unconfirmed, so live runs stay blocked);
7. locks down file permissions (section 7);
8. installs and enables the **coopa-trada** service.

It never creates `.env` and never touches a Schwab login.

## 6. Create `.env` on the server

```bash
sudo nano /opt/coopa-trada/.env
```

Paste what you need (copy values from your Mac's `.env`; the full list with
explanations is in `.env.example`):

```ini
SCHWAB_API_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
SCHWAB_TOKEN_PATH=./schwab_token.json
SCHWAB_ACCOUNT_ID=...
LIVE_TRADING=0

# The server has no Mac notifications: ntfy is the only way alerts reach you.
NTFY_TOPIC=...

# Start each trading day's practice run at 7:15 AM MT.
AUTO_START=1
AUTO_START_TIME=07:15
DAILY_SUMMARY=1

# No Apple picture reader on Linux: screenshots are read by Claude.
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=...
```

Save with **Ctrl+O**, Enter, **Ctrl+X**. Then:

```bash
sudo bash /opt/coopa-trada/deploy/lock_down.sh
sudo systemctl restart coopa-trada
journalctl -u coopa-trada -n 30 --no-pager    # should end with "Coopa Trada UI on http://127.0.0.1:8770"
```

Copy your `config/rules.yaml` from the Mac too if you've ticked confirmations there
(same `rsync`/`scp` as above, then `lock_down.sh`).

## 7. Locking down the secrets

Two files are as sensitive as your Schwab password:

- **`.env`**: the Schwab app key and secret, the Telegram bot token, the Claude key.
- **`schwab_token.json`**: the Schwab login itself. Whoever has it can trade your
  account until it expires.

`deploy/lock_down.sh` (run it again any time you copy a file in) makes sure:

| Path | Permission | Meaning |
|---|---|---|
| `/opt/coopa-trada` | `drwx------ coopa` | Only the `coopa` user can even look inside. |
| `.env`, `schwab_token.json` | `-rw------- coopa` | Only `coopa` can read them. |
| `trada.db`, `imports/`, `config/` | owner only | Your trade history and pick lists. |

The service also starts the app with `UMask=0077`, so files the app writes later
(a new Schwab login after a reconnect, the database) are private automatically, and
with `ProtectSystem=strict`, so the app can't write anywhere outside its folder.

Also:

- Never put `.env` or the token in git, email, Slack, S3 or a screenshot.
- The disk is encrypted (step 3). The server needs no AWS IAM role; leave it without one.
- Check permissions any time: `sudo ls -la /opt/coopa-trada`.

## 8. Private dashboard access with Tailscale

The dashboard has no password, so it must never be on the public internet. It listens
only on `127.0.0.1` inside the server. **Tailscale** makes a private network between
your own devices; only devices logged into your Tailscale account can reach it.

1. Make a free account at <https://tailscale.com> and install the Tailscale app on your
   **Mac** and your **phone**; log in on both.
2. On the server:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --hostname coopa-server
   ```

   It prints a link. Open it on your Mac and approve the server.
3. Still on the server, publish the dashboard **to your Tailscale network only**:

   ```bash
   sudo tailscale serve --bg 8770
   ```

   The first time, it may ask you to enable HTTPS certificates in the Tailscale admin
   console (click the link it prints). It then shows an address like
   `https://coopa-server.<your-tailnet>.ts.net`. Open it on your Mac or phone (with
   Tailscale on): that's the dashboard. Bookmark it.

   Use `tailscale serve`, **never** `tailscale funnel`: funnel would put the dashboard
   on the public internet.
4. Check that buttons work: **Settings → Send test alert**. Your phone should buzz.
   If the page instead says *"Cross-origin writes are not allowed"*, the Tailscale
   proxy changed the page's address on the way in (not tested yet with this app). Use
   the SSH tunnel instead, which always works, from your Mac:

   ```bash
   ssh -N -L 8770:127.0.0.1:8770 -i ~/.ssh/coopa.pem ubuntu@coopa-server
   ```

   and open <http://127.0.0.1:8770> while it runs (Ctrl+C to stop).
5. Close the public SSH door. Now that Tailscale works, connect with
   `ssh -i ~/.ssh/coopa.pem ubuntu@coopa-server` (the Tailscale name), then in the AWS
   console → EC2 → your instance → **Security** → the security group → **Edit inbound
   rules** → delete the SSH rule. The server then has no open ports on the internet at all.
   (If Tailscale ever breaks, re-add "SSH from My IP" in the same place.)

## 9. The Schwab reconnect on a server with no screen

Schwab makes you log in again every 7 days. The server has no screen, but it doesn't
need one: the dashboard's **Reconnect Schwab** works from your phone or Mac through
Tailscale.

**When:** while no run is active (Reconnect is refused during a run). With auto-start,
runs go from 7:15 AM MT until shortly after the selling window (about 2 PM MT; 11 AM on
half days). Afternoons, evenings and weekends are all fine.

**How often:** about **every 4 trading days**, not once a week. A run needs the login to
last until 30 minutes after the *next* trading day's selling window, because anything
it buys is held overnight. One 7-day login therefore covers 4 runs (e.g. a Monday
3 PM login covers Tuesday to Friday; Friday's run needs it until Monday afternoon).
**The easy habit: reconnect every Monday and Thursday afternoon** (after that day's run);
that covers every run, holiday weeks included. If you forget, you're still warned:
when the login won't cover the next run, your phone
gets **"Reconnect Schwab before the next run"** (twice a day until fixed), the Dashboard
turns red, and the 7:15 AM start is skipped with an alert saying why.

**Steps (phone or Mac):**

1. Open the dashboard (your `ts.net` bookmark). Check that no run is active.
2. Click **Reconnect Schwab** → it opens Schwab's login page.
3. Log in to Schwab, approve the app, click **Allow**/**Done**.
4. Your browser goes to an address starting with `https://127.0.0.1:8182/?code=` and
   shows an error such as *"can't connect"*. **That is expected.** The address itself
   is what's needed.
5. Copy the **whole address** from the address bar (on an iPhone: tap the address bar,
   then tap and hold → Select All → Copy).
6. Back on the dashboard, paste it into the box and click **Finish**. Do this within a
   minute or two; the code in that address expires quickly. Finish the whole thing
   within 10 minutes of step 2.
7. The Schwab pill says **Connected** with the new expiry. The new login is saved on the
   server with private permissions (UMask, section 7).

**Fallback (no dashboard access):** log in with the Mac app's Reconnect (with the Mac
app's run stopped), then copy the token file over and quit the Mac app:

```bash
scp -i ~/.ssh/coopa.pem schwab_token.json ubuntu@coopa-server:/tmp/
ssh -i ~/.ssh/coopa.pem ubuntu@coopa-server \
  'sudo mv /tmp/schwab_token.json /opt/coopa-trada/ && sudo bash /opt/coopa-trada/deploy/lock_down.sh && sudo systemctl restart coopa-trada'
```

Don't restart the service while a run is active (see below).

## 10. Day to day

| I want to… | Command (on the server) |
|---|---|
| See if it's running | `systemctl status coopa-trada` |
| Watch the log live | `journalctl -u coopa-trada -f` |
| See today's log | `journalctl -u coopa-trada --since today` |
| Restart it | `sudo systemctl restart coopa-trada` |
| Stop it | `sudo systemctl stop coopa-trada` |
| Check the time | `timedatectl` |

**Restarts during a run.** Stopping or restarting the service in the middle of a run
ends that run abruptly. For a practice run that's harmless. For a **real** run, the
app treats it like a crash on purpose: the next run is blocked until a person checks
the Reconcile page and Schwab, then clicks **Clear stale lock** (you get an alert).
So restart outside trading hours. Ubuntu's automatic security updates don't reboot the
server by themselves.

**Updating the code:** rerun the `rsync` from step 4, then
`sudo cp -a /tmp/coopa-trada/. /opt/coopa-trada/ && sudo bash /opt/coopa-trada/deploy/setup_server.sh`
(outside trading hours). The setup script is safe to re-run; it keeps `.env`,
`config/rules.yaml`, the database and the Schwab login.

**Backups:** the history is in `/opt/coopa-trada/trada.db`. Copy it to your Mac now
and then:
`ssh -i ~/.ssh/coopa.pem ubuntu@coopa-server 'sudo cat /opt/coopa-trada/trada.db' > trada-backup.db`.

## 11. Not tested yet

These files were written and checked for syntax, but **nobody has run them on a real
AWS server yet**. Expect to adjust small things the first time:

- `setup_server.sh` on a fresh Ubuntu 24.04 Arm machine (package installs, swap, service start).
- Whether `tailscale serve` keeps the dashboard's buttons working (step 8.4 has the fallback).
- The Reconnect flow from a phone browser through Tailscale.
- Keep-awake is a Mac feature; on the server the Dashboard shows it as unavailable, which
  is expected (servers don't sleep).
