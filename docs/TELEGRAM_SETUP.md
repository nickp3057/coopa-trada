# Telegram setup: send the picks screenshot to the bot

When this is set up, Cooper forwards the pick screenshot to a private Telegram bot.
The bot reads it and replies with every pick and two buttons, **✅ Confirm** and
**❌ Reject**. Nothing is bought until someone presses Confirm, either in Telegram or
on the dashboard. A run that is already going buys the confirmed picks straight
away, as long as it's inside the buying window (normally 1:30–1:57 PM Mountain).

It takes about 15 minutes. You need Telegram on a phone, and access to the
computer that runs Coopa Trada (to edit its `.env` file).

---

## Step 1. Create the bot with BotFather

BotFather is Telegram's official bot for making bots.

1. In Telegram, tap the search bar and type **BotFather**. Open the one with the
   blue check mark (its username is exactly `@BotFather`).
2. Tap **Start** (or send `/start`).
3. Send `/newbot`.
4. It asks for a **name**. This is what you see in chats. Type something like
   `Coopa Trada Picks`.
5. It asks for a **username**. This must end in `bot` and nobody else can already
   have it, for example `coopa_trada_picks_bot`. If it's taken, try another.
6. BotFather replies with a **token**. It looks like
   `7123456789:AAH4k...` (numbers, a colon, then about 35 letters and symbols).
   **Copy it.** Treat it like a password: anyone who has it can control the bot.
   Don't paste it in a group chat, an email, or a screenshot.

Optional but recommended, still in the BotFather chat:

- `/setjoingroups` → choose your bot → **Disable**. Then nobody can add the bot to
  a group.
- `/setdescription` → choose your bot → type `Private bot for Cooper's picks`.

If the token ever leaks: send `/revoke` to BotFather, choose the bot, and put the
new token in `.env` (Step 3).

## Step 2. Find the chat IDs

Telegram identifies every person by a number called the **chat ID**. The bot
answers only the chat IDs you list in `.env`. It ignores everyone else
completely: no reply, no reading, no buttons.

You need Cooper's chat ID (and Nick's too, if Nick should be able to send picks).

**Easiest way, through the app:**

1. First do Step 3 with only the token filled in, and put any number (for example
   `1`) as the allowed chat ID, so the bot starts.
2. Cooper opens Telegram, searches for the bot's username (from Step 1, step 5),
   taps **Start**, and sends it any message, like `hi`.
3. The bot ignores that message, and the app records the ID it came from. On the
   **Dashboard**, the **Picks sent by Telegram** card says
   *"Ignored messages from chat ID(s) 123456789…"*. That number is Cooper's chat ID.
   (It is also in `logs/coopa-trada.log`.)

**Another way, with a web browser:**

1. Cooper sends the bot any message, as above.
2. On a computer, open this address, replacing `<TOKEN>` with the bot token from
   Step 1 (keep the word `bot` in front of it):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. The page shows text. Find `"chat":{"id":` followed by a number, such as
   `"chat":{"id":123456789,"first_name":"Cooper"...`. That number is the chat ID.
   Do this while the app is **not** running, because the app reads these messages
   itself.

Use a **private chat** with the bot, not a group. Chat IDs of people are positive
numbers. Group IDs start with a minus sign, and anyone in an allowed group could
press Confirm.

## Step 3. Put the token and IDs in `.env`

`.env` is the settings file in the app's folder (see COOPER_README section 2a for
how to edit it on a Mac). Add these lines. Leave out the `< >` signs.

```
TELEGRAM_BOT_TOKEN=<the token from Step 1>
TELEGRAM_ALLOWED_CHAT_IDS=<Cooper's chat ID>
```

For more than one person, separate the IDs with commas:
`TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321`.

**Reading the picture.** Away from a Mac, and on a server, the bot reads screenshots
with the Claude API, which needs a key:

```
ANTHROPIC_API_KEY=<key from console.anthropic.com>
```

On a Mac, the app uses Apple's built-in reader unless `.env` says
`COOPA_IMAGE_READER=claude`. The screenshot is sent to Anthropic only when the
Claude reader is used.

Save the file, then **quit and reopen** the app (or restart
`python -m coopa_trada.web`) so it reads the new settings.

## Step 4. Test it

1. Open **Dashboard**. A card called **Picks sent by Telegram** appears (it is
   hidden while Telegram is off).
2. Cooper sends the bot a screenshot of a pick list. The bot replies
   *"Reading the picks…"*, then lists every pick:

   ```
   Picks for Thu Oct 22, 2026, expiring Fri Oct 23, 2026 (8):
   • IBIT 44P  last 0.71  TVT 66.4  Schwab -0.554
   ...
   [✅ Confirm] [❌ Reject]
   ```

3. Check every row against the screenshot. If something was misread, press
   **Reject** and use the dashboard upload instead, where the text can be corrected.
4. Press **Confirm**. The message changes to *"Confirmed at 1:47 PM MT…"* and says
   whether a run is active to buy the picks.

The same pending list also shows on the dashboard with its own Confirm and Reject
buttons. Confirming in one place updates the other.

## What the bot refuses

- **A list that isn't for today.** The title's first date (the "9/2" in
  *"FOMC Picks for 9/2, 9/4 Expiration"*) must be today. For other dates, use the
  dashboard.
- **A title it can't read.** It needs both dates. Use the dashboard and type the
  dates.
- **A row it can't make sense of** (unknown ticker, unreadable option). The reply
  shows what it read, so it can be fixed on the dashboard.
- **A preview older than 1 hour.** Send the screenshot again.
- **Anything from a chat that isn't listed.** Ignored without a reply.

Confirming a second list for the same day **replaces** the first one. Picks already
bought stay bought; picks that only appeared on the replaced list are not bought.

## Troubleshooting

| What you see | What to do |
|---|---|
| The bot never replies | Check the app is running and `.env` has both `TELEGRAM_` lines, then restart the app. Check the chat ID is in the list (Step 2). |
| "Could not use that picture: … ANTHROPIC_API_KEY …" | Add the Claude key to `.env` (Step 3) and restart. |
| "Could not download that picture" | Telegram had a hiccup; send it again. |
| The dashboard card is missing | Telegram is off: one of the two `TELEGRAM_` lines is empty or wrong. |
| "Preview not found" after pressing Confirm | The app restarted since the preview was sent (previews live in memory). Send the screenshot again. |

The bot uses Telegram's "long polling": the app asks Telegram for new messages
every few seconds, so the computer needs internet access but no open ports and no
public web address.
