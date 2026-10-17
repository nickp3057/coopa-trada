# Schwab setup checklist

What has to be true, at Schwab and on the Mac, for this app's **Reconnect
Schwab** login to work. The first part is for Nick (portal and `.env`); the
walkthrough at the end is for Cooper.

Sources: the installed **schwab-py 1.5.1** (`.venv/lib/python3.14/site-packages/schwab/auth.py`),
**Authlib 1.8.0** (which schwab-py uses for OAuth), and this app's
`coopa_trada/schwab_login.py`. Anything those sources can't show, such as the
portal's screens and Schwab's server-side rules, is marked **(unverified)** and
collected in "Things I'm not sure of" at the end.

## How the app's login works (what the code does)

The app uses schwab-py's *manual* flow, split into its two halves so a web page
can stand in for the terminal prompt (`client_from_manual_flow` calls exactly
these two functions):

1. **Reconnect Schwab** calls `schwab.auth.get_auth_context(SCHWAB_API_KEY, SCHWAB_CALLBACK_URL)`.
   That builds, without any network call, the URL
   `https://api.schwabapi.com/v1/oauth/authorize?response_type=code&client_id=<app key>&redirect_uri=<callback URL>&state=<random>`.
   The random `state` is kept in the app's memory.
2. The person opens that URL, logs in at Schwab, and clicks **Allow**. Schwab
   sends the browser to `<callback URL>?code=…` (reportedly with a `session=…`
   parameter too **(unverified)**; the app ignores anything but `code` and `state`).
   Nothing listens on `127.0.0.1:8182`, so the page fails to load. That is
   expected: the address bar holds the one-time code.
3. The person pastes that address. The app checks that it has the same scheme,
   host, port and path as `SCHWAB_CALLBACK_URL` and contains `code=`, then calls
   `schwab.auth.client_from_received_url`. That POSTs to
   `https://api.schwabapi.com/v1/oauth/token` with `grant_type=authorization_code`,
   the code, the same `redirect_uri`, and HTTP Basic auth `app key : app secret`.
   Authlib refuses the address first if its `state` doesn't match step 1.
4. schwab-py wraps the result as `{"creation_timestamp": <now>, "token": {...}}`
   and the app writes it atomically, readable only by this user, to
   `SCHWAB_TOKEN_PATH` (default `schwab_token.json` next to the app).
5. Later runs load that file with `client_from_token_file`. schwab-py refreshes the
   short-lived access token by itself (`leeway=300`, i.e. 5 minutes before it
   expires; Schwab reportedly issues 30-minute access tokens **(unverified)**) and
   rewrites the file, keeping the original `creation_timestamp`. The refresh token
   can't be renewed this way. After 7 days a fresh login is needed, which is why
   the app counts down from `creation_timestamp` (7 days is Schwab's rule, not
   something schwab-py enforces; see below).

The app never uses `client_from_login_flow` or `easy_client`. Those start a local
HTTPS server on the callback port and open a browser on the same machine.

## Checklist: Schwab Developer Portal (developer.schwab.com)

- [ ] **A developer-portal account exists.** It is separate from the
      schwab.com brokerage login, though it can use the same email
      **(unverified)**.
- [ ] **An app is registered** for the individual Trader API, i.e. the
      "Accounting and Trading Production" product. Market data comes with it
      **(unverified)**. The app places orders, reads accounts, orders and
      positions, and gets option chains and quotes. It needs both trading and
      market data.
- [ ] **App status is "Ready For Use".** schwab-py's docstrings say the app secret
      is "provided upon app approval" and point to an "Approved – Pending"
      section. Logging in while the app is still pending is reported to fail
      **(unverified)**. New apps and changed apps can take several days to approve.
- [ ] **The callback URL registered on the app is exactly the value in `.env`.**
      Recommended: `https://127.0.0.1:8182`.
      - schwab-py: the callback "must *exactly* match the value you've entered in
        your application configuration, otherwise login will fail with a security
        error. Be sure to check case and trailing slashes."
      - `https://127.0.0.1:8182` and `https://127.0.0.1:8182/` are different
        strings. Pick one and use it in both places. (The app's own paste check
        ignores a trailing slash, but Schwab compares the `redirect_uri` it is
        sent, which is the `.env` value verbatim.)
      - Use `127.0.0.1`, not `localhost`. schwab-py only allows `127.0.0.1` for its
        browser flow. The manual flow doesn't check, but staying with the schwab-py
        recommendation keeps both flows usable.
      - Include a port above 1024 (8182). No port means 443.
      - Use `https://`. With `http://` schwab-py's manual flow prints a security
        warning. Whether the portal accepts `http` at all is **(unverified)**.
      - Changing the callback URL later probably sends the app back for approval
        for a few days (schwab-py says it "will likely require app re-approval").
- [ ] **Copy the App Key and Secret** from the app's page. They go in `.env` as
      `SCHWAB_API_KEY` and `SCHWAB_APP_SECRET`. The key is the OAuth `client_id`.
      Both are used as HTTP Basic credentials at the token endpoint. A wrong secret
      fails only at step 3 above, with "Schwab did not accept that login".

## Checklist: the `.env` next to the app

```
SCHWAB_API_KEY=<App Key from the portal>
SCHWAB_APP_SECRET=<Secret from the portal>
SCHWAB_CALLBACK_URL=https://127.0.0.1:8182      # character-for-character the portal value
SCHWAB_TOKEN_PATH=./schwab_token.json           # default; relative to the app's folder
SCHWAB_ACCOUNT_ID=<optional: the plain account number to trade>
LIVE_TRADING=0
```

- Any of the first three missing: Reconnect refuses with a message naming it.
- The app reads only the `.env` in the app's own folder. After editing it, quit
  and reopen the app.

## Which Schwab login, and which account

- **The person who logs in at step 2 decides whose account it is.** That is the
  **schwab.com brokerage login** (the one used for the Schwab app or website),
  *not* the developer-portal login. The token can only reach the accounts that
  login grants. Cooper should log in as himself, on his own computer.
- Schwab's consent screens appear to let the person choose which of their
  accounts to share with the app **(unverified)**. Make sure the account meant
  for this bot is selected.
- The app calls `get_account_numbers()` and trades:
  - the account whose number equals `SCHWAB_ACCOUNT_ID`, if set. It stops with
    "account … not found" if the login can't see that account; or
  - the **first account Schwab lists**, if not set. **Set `SCHWAB_ACCOUNT_ID`** if
    the login can see more than one account, so the bot never picks the wrong one.
- Each database is bound to the first account a live run connects to
  (`Store.bind_live_account`). A different account later is refused ("This database
  belongs to another Schwab account").
- The account needs options approval at a level that allows buying calls and
  puts **(unverified; Schwab account setting, not app setting)**.

## First login: plain-language walkthrough (for Cooper)

You need: the app installed (COOPER_README section 1), the `.env` file Nick set up
(section 2a), and your normal Schwab username, password and phone for the text
code. Allow 5 minutes. Do it all in one go: Schwab's code only works for a short
time.

1. Open **Coopa Trada**. On the Dashboard, find **Schwab connection**. It says
   **Schwab: Not set up**. That's normal the first time.
2. Click **Reconnect Schwab**. Three numbered steps appear.
   - If instead it says app details are missing from the `.env` file, stop and
     tell Nick.
3. Click **Open the Schwab login page**. A new tab opens on Schwab's own website
   (the address starts with `https://api.schwabapi.com`).
4. Log in with **your normal Schwab username and password**, then enter the text
   code Schwab sends you.
5. Schwab asks you to accept terms and **choose which accounts** to share with
   "Coopa Trada" (or whatever Nick named the app). Tick the account you want the
   bot to trade, then click **Allow** (or **Done**).
6. The tab now shows an error such as **"This site can't be reached"**, **"Safari
   can't connect to the server"**, or a security warning. **That's expected.**
   Don't close the tab.
7. Click in that tab's **address bar**, select the **whole** address (it starts
   with `https://127.0.0.1:8182/?code=`), and copy it (**Command‑C**).
8. Go back to the Coopa Trada tab, click in the box under step 3, paste
   (**Command‑V**), and click **Finish** straight away.
9. The card should say **Schwab: Connected**, with "Login expires in 6 days 23 hr".
   You can close the Schwab tab.

If it says **Schwab did not accept that login**, the code expired or was already
used. Click **Reconnect Schwab** and do steps 3–8 again, a bit faster. If it says
**that is not the address Schwab sent you to**, you copied only part of the
address, or a different tab's. Copy the whole thing from the tab that showed
the error.

Every 7 days Schwab makes you do this again (COOPER_README section 4). The
Dashboard and alerts remind you, and a run won't start if the login would run out
before the bot has sold what it buys.

## If it fails: what to check

| Symptom | Likely cause |
|---|---|
| Schwab's page itself shows an error before you can log in (often mentioning the redirect URI or client) | Callback URL in `.env` differs from the portal's by even one character; the app isn't Ready For Use; or a wrong App Key |
| Login works, then "Schwab did not accept that login" every time | Wrong App Secret; code pasted too slowly; the pasted address isn't from the most recent Reconnect click (the `state` must match) |
| "That is not the address Schwab sent you to" | Pasted something else, or `.env` callback differs from where Schwab actually redirected (portal value ≠ `.env` value) |
| Connected, but a live run says "account … not found" | `SCHWAB_ACCOUNT_ID` is not among the accounts the login shared |

## Things I'm not sure of

These can't be confirmed from the code. Check them in the portal, or on the
first supervised login:

1. The exact portal wording and statuses ("Approved – Pending", "Ready For Use",
   "Modification Pending") and the product name ("Accounting and Trading
   Production"). These are from schwab-py's docs and community reports, not the
   installed code.
2. Whether a pending app fails at the authorize page, at the token exchange, or
   only on later API calls.
3. Whether the portal accepts `http://` callbacks, or several callback URLs on one app.
4. How long Schwab's one-time `code` lasts. Reports range from about 30 seconds to
   a few minutes. The app gives a started attempt 10 minutes, but Schwab's limit is
   what counts.
5. That the refresh token lasts exactly 7 days from login and can't be extended.
   schwab-py doesn't enforce it; the app assumes it (`REFRESH_TOKEN_LIFETIME`).
6. Whether the consent screen lets you choose accounts, and whether
   `get_account_numbers()` lists only the chosen ones.
7. The options-approval level the account needs for single-leg buy-to-open and
   sell-to-close limit orders.
8. Whether Safari, Chrome or Firefox differ at step 6 (an error page vs. a
   certificate warning). Either way the address bar should hold the code.
9. Whether Schwab requires the developer-portal account and the brokerage login
   to be the same person for an individual-use app.
