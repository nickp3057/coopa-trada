# Coopa Trada: how to use it

This guide needs no Terminal or command-line skills. Everything happens in Finder
and in a web page the app opens for you.

**Safety first.** The app starts in **practice mode** (shown as **DRY‑RUN**). In
practice mode it uses made-up prices and never sends anything to Schwab. Real trading
stays switched off until Nick turns it on with you, and even then the app asks you
to type a confirmation phrase every time.

---

## 1. Install (once)

1. Make a new folder for the app, for example **Documents → Coopa Trada**.
2. Drag **Coopa Trada.app** into that folder. Keep it there: the app saves its
   settings, records and Schwab login **in the same folder**.
3. Open the app the first time:
   - **Right-click** (or Control-click) **Coopa Trada.app** and choose **Open**,
     then **Open** again.
   - If your Mac says it "cannot be opened" or "cannot verify the developer", open
     **System Settings → Privacy & Security**, scroll down, and click
     **Open Anyway** next to Coopa Trada. You only do this once.
4. Your web browser opens the **Coopa Trada Dashboard**. The app has no window of
   its own; the browser page *is* the app. The address is `http://127.0.0.1:8770`.

If you see a message asking you to **move the app into its own folder**, do step 2
again. macOS was running a temporary copy straight from Downloads.

The app adds a few files next to itself (`config`, `data`, `env.example.txt`, and
later `trada.db` and `logs`). Leave them there.

---

## 2. Schwab setup (once, with Nick)

### 2a. The settings file

The app reads your Schwab app details from a file named **`.env`** (dot env) in
the same folder as the app. Nick will give you the values.

1. In the app's folder, open **env.example.txt** in **TextEdit**.
2. Fill in the lines Nick gives you (`SCHWAB_API_KEY=…`, `SCHWAB_APP_SECRET=…`).
   Leave `LIVE_TRADING=0` until Nick says otherwise.
3. In TextEdit choose **Format → Make Plain Text** (skip this if it says "Make Rich Text").
4. Choose **File → Save As…**, type the name **`.env`**, untick **"If no extension
   is provided, use .txt"**, save it **in the app's folder**, and click **Use "."**
   when asked.
5. Files starting with a dot are hidden in Finder. Press **Command‑Shift‑.**
   (period) in Finder to show or hide them.
6. **Quit and reopen the app** (see section 7) so it reads the file.

### 2b. Log in to Schwab

(Nick: the Schwab Developer Portal checklist, the exact `.env` values, and a
longer version of these steps are in `docs/SCHWAB_SETUP.md`.)

1. On the Dashboard, scroll to **Schwab connection** and click **Reconnect Schwab**.
2. Click **Open the Schwab login page**. A new tab opens on Schwab's site.
3. Log in as usual (including any text-message code) and click **Allow**.
4. Schwab then sends that tab to an address starting with `https://127.0.0.1:8182`.
   The page will say **it can't be reached** or show a **security warning**. **That
   is expected.**
5. Click in that tab's **address bar**, select the whole address (Command‑A), and
   copy it (Command‑C).
6. Go back to the Coopa Trada tab, paste the address into the box (Command‑V), and
   click **Finish** straight away. The code in the address only lasts a short time.
7. The status changes to **Schwab: Connected**.

If it says Schwab did not accept the login, click **Reconnect Schwab** again and
repeat. Paste quickly.

### 2c. Alerts on your Mac and phone

When something needs you, the app doesn't just wait for you to look at the
Dashboard. It **sends an alert**:

- an order Schwab may or may not have received (it is never resent),
- a sell order that failed or isn't filling,
- a crash or restart that left something for you to decide,
- the Schwab login running out within 2 days, or not lasting through the next run,
- a run that was refused, or that stopped with an error.

**Mac notifications** are always on. The first one may ask whether to allow
notifications (they can show up as coming from **Script Editor**). Click **Allow**.
If you never see them, open **System Settings → Notifications**, find **Script
Editor**, and turn **Allow notifications** on. Focus / Do Not Disturb hides them.

**Phone alerts (optional)** use the free **ntfy** app:

1. Nick makes you a private channel name. It must be long and random, because
   anyone who knows the name can read the alerts. Nick runs this once in Terminal:

   ```
   python3 -c "import secrets; print('coopa-' + secrets.token_urlsafe(24))"
   ```

   It prints something like `coopa-q3Vh9...` (38 characters). Don't use a short
   or made-up name; the app refuses names under 24 characters.
2. Add a line to your `.env` file (section 2a): `NTFY_TOPIC=` followed by that name.
3. On your phone, install **ntfy** (App Store or Google Play), tap **+**, and
   subscribe to the same name. Leave the server as **ntfy.sh**.
4. Quit and reopen the app, open **Settings → Alerts**, and click **Send test
   alert**. You should get it on both the Mac and the phone.

Alerts never contain account numbers, dollar amounts or links; they say what
happened and where to look. The same alert is repeated at most every 6 hours
(12 for the Schwab login) while the problem lasts.

---

## 3. Every trading day

The full trading rules, with dollar examples, are in **docs/HOW_IT_TRADES.md**.

1. **Open the app** (double-click it) **before the market opens** (7:30 AM MT) if
   anything was held overnight, otherwise any time before 1:30 PM MT. The Dashboard opens.
2. **Check Schwab**: it should say **Connected** (or **Expiring soon**; see section 4).
3. **Start the run**: click **Start today's run (dry‑run)**. The run watches anything
   held from yesterday and waits for today's picks. The status line says what it's doing.
4. **Send the picks** when they arrive (1:40–1:55 PM MT), either way:
   - **Telegram** (if set up, see docs/TELEGRAM_SETUP.md): forward the screenshot to the
     bot, check the list it sends back, press **✅ Confirm**.
   - **Dashboard**: under **Upload your picks**, choose the CSV or screenshot, click
     **Read file**, enter the **Trade date** and **Expiration**, check every row
     (fix misread text right there), click **Preview picks**, then **Confirm and select
     these picks**.

   The run **buys as soon as you confirm**, between 1:30 and 1:57 PM MT (earlier on half
   days). Then it watches and sells by the rules: a take-profit sells the same day;
   otherwise the position is held and sold on the next trading day.
5. **Leave the Mac plugged in, lid open, and the app open** until the run finishes
   after the close (see "Why plugged in and lid open" below).

**Stop** stops the bot *watching*; it does **not** sell anything. Anything already
bought stays in your Schwab account.

#### Why plugged in and lid open

The bot only sells when it is awake to see the prices. A sleeping Mac stops it
watching, and anything it bought just sits there.

- While a run is going, or while a real position or order is still open, the app
  **stops the Mac falling asleep by itself**. The Dashboard then says **"Keeping
  Mac awake"**. When nothing needs watching it lets the Mac sleep normally again.
  The screen can still turn off; that's fine.
- **Closing the lid puts the Mac to sleep anyway.** No app can stop that (unless a
  separate monitor is plugged in). So keep the lid **open**.
- **Plug it in.** On battery the Mac can still sleep when the battery runs low,
  and macOS is less willing to stay awake. A dead battery stops the bot too.
- Don't choose **Apple menu → Sleep** or **Shut Down** while the Dashboard says
  "Keeping Mac awake".

### Real trading (only after Nick sets it up)

Real trading needs all of these, every time:

- `LIVE_TRADING=1` in the `.env` file (Nick changes this with you);
- every setting on the **Settings** page ticked **Confirmed with Cooper**;
- **Schwab: Connected**;
- under **Enable live trading**, open **Show the live‑run control**, type
  **ENABLE LIVE TRADING** exactly, and click **Start a LIVE run**.

If anything is missing, the red box explains what, in plain words.

---

## 4. Weekly Schwab reconnect

Schwab makes everyone **log in again every 7 days**. The app counts down for you:

- Under **Schwab connection**: "Login expires in 3 days 4 hr".
- With **under 2 days left**, a yellow banner appears at the top of the Dashboard.
- When it has expired, the banner turns red and runs will not start.
- The login has to last until the bot has **sold** what it buys, and selling
  happens on the **next trading day**. So a run on Friday needs the login to last
  until Monday afternoon (longer over a holiday weekend). The Schwab card says
  "The next run needs Schwab until …". If the login won't last that long, the banner
  turns red even when there are still several days left, and the run won't start.

Do the reconnect (section 2b, steps 1–7) **before** starting a run. It is refused
while a run is in progress. The easy habit: **reconnect every Thursday or
Friday morning**, then a Friday run is always covered.

---

## 5. What the Dashboard is telling you

| You see | Meaning | What to do |
|---|---|---|
| **MODE = DRY‑RUN** (green, top bar) | Practice mode: nothing goes to Schwab | Nothing |
| **MODE = LIVE** (red, flashing) | Real trading is switched on | Be sure you meant it |
| **status idle** | Not running | Start a run when ready |
| **RUNNING since …** | Working; watching prices | Keep the Mac plugged in, lid open |
| **Keeping Mac awake: …** | The app is stopping the Mac from sleeping while it has something to watch | Keep the lid open; nothing else |
| **ERROR: …** | The last run stopped for the reason shown | Read the message; tell Nick if unclear |
| **Schwab: Connected** | Logged in | Nothing |
| **Schwab: Expiring soon** | Under 2 days of login left | Reconnect soon (section 4) |
| **Schwab: Expired** | Login ran out | Reconnect now |
| **An alert on your Mac or phone** | Something needs you; the alert says what | Open the Dashboard and follow it |
| **Red banner: "This login runs out before the next run is finished"** | The login would lapse before the bot sells on the next trading day | Reconnect before starting the run |
| **Schwab: Not set up** | No login saved yet, or the settings file is missing | Do section 2 |
| **Red banner: orders need your attention** | The bot sent an order but could not confirm whether Schwab received it. It did **not** resend it, and it stopped trading that option. | Open **Reconcile**, check **Orders** in your Schwab app, then click what you found |
| **"A previous run did not shut down cleanly"** | The app or Mac stopped during a real run | Check **Reconcile** and your Schwab positions, then click **Clear stale lock** |
| **Live runs are blocked: these settings are not confirmed** | Settings still need your OK | Go through **Settings** with Nick |

On the **Settings** page, **Unconfirmed** badges mark the choices waiting for your
decision (the budget, the 10-point rule, hold and stop timing, sell repricing, the
buying window, and more; the questions are at the end of docs/HOW_IT_TRADES.md). Real trading stays off until
each one is ticked **Confirmed with Cooper**. Changing a confirmed value un-ticks
it, so you have to confirm the new value.

---

## 6. Things the app never does on its own

- Switch from practice to real trading.
- Resend an order it is unsure about.
- Sell when you press **Stop**.
- Send your Schwab password anywhere. You type it only on Schwab's own website.

---

## 7. Quitting and reopening

- To quit, scroll to the bottom of any page and click **Quit Coopa Trada**. It
  refuses while a run is in progress; press **Stop** first.
- Closing the browser tab does **not** quit the app. Double-click the app again to
  get the page back.
- If the page doesn't load, double-click the app again, or go to
  `http://127.0.0.1:8770` in your browser.

## 8. If something looks wrong

Send Nick the file **logs/coopa-trada.log** from the app's folder, plus a
screenshot of the Dashboard.
