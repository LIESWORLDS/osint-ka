# Telegram Refer & Earn Bot

A ready-to-run bot with forced multi-channel join, referral system, credit
wallet, paid "Get Info" lookup, a paid continuous "Blast" API poller, a
dice-based daily spin, real coloured buttons, and a full admin panel.

## ⚠️ Notes on your requests

1. **Button colours — you were right, this is real!** Telegram Bot API
   **9.4** (Feb 9, 2026) added a genuine `style` field on both
   `KeyboardButton` and `InlineKeyboardButton` — `primary` (blue),
   `success` (green), `danger` (red). This bot now uses it directly:
   - Main menu: **Refer & Earn** = blue (`primary`), everything else = red
     (`danger`), exactly as you asked.
   - Join Channel = blue, ✅ Verify = green, ⏹ Stop (Blast) = red, and the
     Admin Panel buttons are colour-coded too (destructive = red,
     positive = green, navigation = blue).
   - **Requires `python-telegram-bot >= 22.7`** — `requirements.txt` has
     been bumped to `22.8`. If your installed library is older, run
     `pip install -U python-telegram-bot` or the buttons will fall back to
     plain/default colour. Also note: colour only renders on Telegram
     clients updated to support Bot API 9.4 — very old client versions
     will just show the normal default button colour.
2. **Blockquote confirmed.** Telegram Bot API added the native
   `<blockquote>` HTML tag in **Bot API 7.0** (Jan 2024), so every bot
   message here is wrapped in one. One hard rule from Telegram's own docs:
   a `<blockquote>` **cannot contain** a `<pre>`/`<code>` block — so JSON
   results (Get Info / Blast) are always sent as a separate plain message
   right after the blockquoted status message, not inside it.
3. **Processing feel improved.** "Get Info" now shows a small live-updating
   loader (`⏳ Processing...`) that animates while the API call is in
   flight (the network call itself also now runs off the main event loop
   so the bot stays responsive to other users while it waits). "Blast"
   already had this covered via its live Round-N counter.

## Features

- `/start` — blocks access until the user has joined **every** required
  channel (channels are managed dynamically, see Admin Panel below).
- `/help` — shows the full command/feature list anytime.
- New (first-time) verified users get **2 free credits**.
- **🎁 Refer & Earn** (blue) — personal link `https://t.me/<bot>?start=<id>`.
  Referrer gets **+1 credit** when the invited user verifies.
- **👤 My Profile** (red) — name, user ID, credits, referrals, join date.
- **🔍 Get Info** (red, **1 credit**) — asks for a 10-digit number, does a
  single API lookup with an animated "Processing..." indicator, returns
  JSON. Shows "Insufficient Balance" if credits are 0, and auto-refunds
  the credit if the API call fails.
- **🚀 Blast** (red, **1 credit**) — asks for a number, then hits your
  configured API repeatedly (every `BLAST_INTERVAL_SECONDS`, default 5s)
  for **up to 5 minutes**, live-editing one message with the current
  **Round N** and the latest JSON result. A red **⏹ Stop** button cancels
  it early; it also auto-stops after 5 minutes.
- **🎰 Daily Spin** (red) — 🔒 **locked until a user has at least
  `MIN_REFERRALS_FOR_SPIN` (default 2) successful referrals.** Once
  unlocked: once every 24 hours, rolls a real Telegram 🎲 dice and awards
  credits equal to the number rolled (1–6). Spinning on **consecutive
  days builds a streak** — every 3rd day in a row adds +3 bonus credits,
  every 7th day adds +10.
- **🏆 Leaderboard** (red) — top 10 users by credit balance (🥇🥈🥉 for the
  top 3), plus your own current rank.
- **🎟 `/redeem <code>`** — enter a promo/gift code for free credits (codes
  are created by an admin, see Admin Panel below). Each code can be
  single-use or multi-use, and each user can redeem a given code once.
- **Every bot message** is shown in a bold small-caps "fancy" font
  (`Hello` → `𝐇ᴇʟʟᴏ`) inside a native Telegram blockquote.


## Admin Panel — `/admin`

Only users in the `admins` table (bootstrapped from `ADMIN_IDS` in `.env`,
or added later from the panel itself) can use this.

- **👥 Total Users** — current user count.
- **📋 All Users** — paginated list (10 per page) showing each user's name,
  ID, and credit balance, with ◀️ Prev / ▶️ Next buttons.
- **📢 Force Channels** — add/remove channels users must join. Adding asks
  for `@username | https://t.me/username | Channel Title`. The bot **must
  be an admin** of each channel to check membership.
- **🛠 Maintenance Mode** — toggle ON/OFF. When ON, only admins can use the
  bot; everyone else sees a maintenance message.
- **🖼 Set Banner Image/Video** — send a photo or video; it will be sent
  above every single bot reply from then on (the photo/video is sent as
  its own message first, then the actual text message).
- **💰 Add / Remove Credits** — send `USER_ID AMOUNT` (e.g. `123456789 5` to
  add 5 credits, `123456789 -3` to remove 3). The target user gets notified
  of their new balance automatically.
- **🎟 Redeem Codes** — create promo/gift codes (`CODE CREDITS [MAX_USES]`,
  e.g. `WELCOME2026 5 100`), view usage, or delete a code. Users redeem
  with `/redeem <code>` or by sending `/redeem` alone and then the code.
- **➕ Add Admin** — send a numeric Telegram user ID to grant admin access.

## Setup

> **🔒 Security note:** a bot token was shared in this chat. Treat it like a
> password — anyone who has it can fully control your bot (read messages,
> send as your bot, change settings). Since it was pasted in a chat, please
> **revoke it now** and generate a fresh one: open [@BotFather](https://t.me/BotFather) →
> `/mybots` → select your bot → **API Token** → **Revoke current token**.
> Put the *new* token only in your local `.env` file — never in chat
> messages, code you paste elsewhere, or anything you might share/commit.

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your values (see comments in
   the file for what each variable does).

3. Add your bot as an **admin** of every force-join channel — otherwise
   membership checks silently fail.

4. Run the bot:
   ```bash
   python eren.py
   ```

## Deploying on Render

This bot polls Telegram (no incoming webhook), but Render's **Web
Service** type expects your app to bind to a port and answer HTTP
requests — otherwise it thinks your deploy failed and, on the **free**
plan, spins the instance down after 15 minutes of no traffic. `bot.py`
already handles this: if a `PORT` env var is present (Render sets one
automatically), it starts a tiny background HTTP server that just replies
`200 OK`, so Render's health check is satisfied.

**Steps:**
1. Push this project to a GitHub/GitLab repo.
2. On Render, click **New +** → **Web Service**, connect the repo.
3. Fill in the form:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python eren.py`
   - **Instance Type:** Free (or Starter if you want no sleep at all)
4. Under **Environment**, add every variable from `.env.example` with
   your real values (`BOT_TOKEN`, `BOT_USERNAME`, `CODE_INFO_API_URL`,
   etc.) — do **not** upload your `.env` file itself, just add each key
   individually in Render's dashboard.
5. Click **Deploy**.
6. **Free plan only:** Render still spins the instance down after 15 min
   with zero HTTP traffic (Telegram polling is *outgoing*, so it doesn't
   count). Use a free uptime pinger like [UptimeRobot](https://uptimerobot.com)
   to hit your Render URL (`https://your-app.onrender.com`) every 5–10
   minutes, so it never falls asleep and misses messages.

**Alternative:** if you'd rather pay for zero sleep and skip the keepalive
trick entirely, choose **Background Worker** instead of **Web Service**
when creating the service (Render's correct type for a pure polling
process) — same Build/Start commands and environment variables, but it
requires at least the Starter plan (~$7/month); there's no free tier for
Background Workers.

**⚠️ Important — your data will NOT survive a redeploy.** This bot stores
users/credits/settings in a local SQLite file (`bot.db`). Render's free
and Starter plans use an **ephemeral filesystem** — every time you deploy
or the instance restarts, `bot.db` is wiped and everyone's credits reset
to zero. For anything beyond testing, either:
- Attach a Render **Disk** (paid plans only) and point `DB_PATH` in
  `db.py` at a path on that disk, or
- Swap SQLite for Render's managed **PostgreSQL** (has a free tier, but
  expires after 30 days) — tell me if you'd like this migrated for you.

## Files

- `eren.py` — all bot logic and handlers (main entry point)
- `db.py` — SQLite storage: users, settings, admins, force_channels
- `style.py` — fancy-font + blockquote text formatting helpers
- `api_client.py` — HTTP wrappers for the Get Info / Blast APIs
- `requirements.txt` — Python dependencies
- `.env.example` — configuration template
- `bot.db` — created automatically on first run

## Notes / assumptions made (tell me if you want these changed)

- `BLAST_API_URL` and `CODE_INFO_API_URL` are generic `GET` requests
  returning JSON. If your real APIs need POST, headers, or an auth token,
  tell me and I'll wire it in.
- Blast polls every **5 seconds** by default (`BLAST_INTERVAL_SECONDS` in
  `.env`) — roughly 60 rounds over the 5-minute window. Adjust as needed;
  going much faster risks Telegram's message-edit rate limits.
- Only **one active Blast per user** at a time is allowed.
- Daily Spin uses the 🎲 emoji (values 1–6). Telegram also supports 🎯, 🏀,
  ⚽, 🎳, 🎰 dice with different value ranges if you'd prefer a different
  one (🎰 slot machine could look cooler for a "spin" feature — let me know).
