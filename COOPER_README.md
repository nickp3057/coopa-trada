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

---

## 3. Every trading day

1. **Open the app** (double-click it). The Dashboard opens.
2. **Check Schwab**: it should say **Connected** (or **Expiring soon**; see section 4).
3. **Upload the picks** under **Upload your picks**:
   - Choose the CSV file or a **screenshot** of the picks table, then click **Read file**.
   - Enter the **Trade date** and the **Expiration** date.
   - Check every row in the box against the original. Fix any misread text right there.
   - Click **Preview picks**, check the table, then click **Confirm and select these picks**.
4. **Start the run**: click **Start today's run (dry‑run)**. The bot waits until about
   20 minutes before the market closes, buys, then watches and sells according to the
   settings.
5. **Leave the Mac awake and the app open** until the run finishes after the close.
   If the Mac sleeps or shuts down, the bot stops watching your positions. Keep it
   plugged in, and in **System Settings → Battery** (or **Energy**) prevent automatic
   sleep while it runs.

**Stop** stops the bot *watching*; it does **not** sell anything. Anything already
bought stays in your Schwab account.

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

Do the reconnect (section 2b, steps 1–7) **before** starting a run. It is refused
while a run is in progress. A real-trading run also refuses to start if the login
would run out before that day's trading is finished.

---

## 5. What the Dashboard is telling you

| You see | Meaning | What to do |
|---|---|---|
| **MODE = DRY‑RUN** (green, top bar) | Practice mode: nothing goes to Schwab | Nothing |
| **MODE = LIVE** (red, flashing) | Real trading is switched on | Be sure you meant it |
| **status idle** | Not running | Start a run when ready |
| **RUNNING since …** | Working; watching prices | Keep the Mac awake |
| **ERROR: …** | The last run stopped for the reason shown | Read the message; tell Nick if unclear |
| **Schwab: Connected** | Logged in | Nothing |
| **Schwab: Expiring soon** | Under 2 days of login left | Reconnect soon (section 4) |
| **Schwab: Expired** | Login ran out | Reconnect now |
| **Schwab: Not set up** | No login saved yet, or the settings file is missing | Do section 2 |
| **Red banner: orders need your attention** | The bot sent an order but could not confirm whether Schwab received it. It did **not** resend it, and it stopped trading that option. | Open **Reconcile**, check **Orders** in your Schwab app, then click what you found |
| **"A previous run did not shut down cleanly"** | The app or Mac stopped during a real run | Check **Reconcile** and your Schwab positions, then click **Clear stale lock** |
| **Live runs are blocked: these settings are not confirmed** | Settings still need your OK | Go through **Settings** with Nick |

On the **Settings** page, **Unconfirmed** badges mark the choices waiting for your
decision (spending cap, the 10% stop, delta filter). Real trading stays off until
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
