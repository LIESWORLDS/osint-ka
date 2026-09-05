"""
Telegram Bot — full featured version.

Features:
- Forced multi-channel join before the bot can be used (channels managed
  dynamically from the admin panel)
- Welcome bonus (2 credits) for new verified users only
- Refer & Earn (1 credit per verified referral)
- My Profile
- Get Info (1 credit -> single lookup of a 10-digit number via an API,
  result returned as JSON)
- Blast (1 credit -> continuous API polling on a number, for up to 5
  minutes, with live "Round N" counter + Stop button)
- Daily Spin (once every 24h, uses a real Telegram 🎲 dice — credits
  awarded equal to the rolled number)
- Admin Panel (/admin): total users, manage force-join channels,
  maintenance mode on/off, set a banner image/video shown above every
  reply, add new admins
- Every bot message is wrapped in a native Telegram <blockquote> and
  rendered in a bold small-caps "fancy" font

Run:  python eren.py
Configure via .env (see .env.example)
"""

import os
import html
import json
import time
import random
import asyncio
import logging
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db
import style
import api_client

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot_username")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")  # fallback channel if none set via admin panel
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")
CODE_INFO_API_URL = os.getenv("CODE_INFO_API_URL", "")
BLAST_API_URL = os.getenv("BLAST_API_URL", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

WELCOME_BONUS = 2
REFERRAL_BONUS = 1
GET_INFO_COST = 1
BLAST_COST = 1
SPIN_COOLDOWN_SECONDS = 24 * 60 * 60
BLAST_DURATION_SECONDS = 5 * 60          # auto-stop after 5 minutes
BLAST_INTERVAL_SECONDS = int(os.getenv("BLAST_INTERVAL_SECONDS", "5"))  # gap between rounds
MIN_REFERRALS_FOR_SPIN = 2  # Daily Spin unlocks only after this many successful referrals

api_client.configure(CODE_INFO_API_URL, BLAST_API_URL)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# user_id -> asyncio.Task, tracks currently running blast loops
active_blasts: dict[int, asyncio.Task] = {}

# ---------------------------------------------------------------------------
# MENU LABELS
# Telegram Bot API 9.4 (Feb 2026) added a real `style` field for buttons —
# 'primary' (blue), 'success' (green), 'danger' (red). Requires
# python-telegram-bot >= 22.7 (see requirements.txt). Older Telegram clients
# that don't support this yet will just show the default button colour.
# ---------------------------------------------------------------------------
BTN_GET_INFO = "🔍 Get Info"
BTN_MY_PROFILE = "👤 My Profile"
BTN_REFER_EARN = "🎁 Refer & Earn"
BTN_DAILY_SPIN = "🎰 Daily Spin"
BTN_BLAST = "🚀 Blast"
BTN_LEADERBOARD = "🏆 Leaderboard"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton(BTN_GET_INFO, style="danger"),
            KeyboardButton(BTN_MY_PROFILE, style="danger"),
        ],
        [KeyboardButton(BTN_REFER_EARN, style="primary")],
        [
            KeyboardButton(BTN_DAILY_SPIN, style="danger"),
            KeyboardButton(BTN_BLAST, style="danger"),
        ],
        [KeyboardButton(BTN_LEADERBOARD, style="danger")],
    ],
    resize_keyboard=True,
)



# ---------------------------------------------------------------------------
# STYLED SEND HELPERS
# ---------------------------------------------------------------------------
async def send_banner(bot, chat_id: int):
    """Send the admin-configured banner image/video (if any) as its own
    message, right before the actual reply."""
    banner_id = db.get_setting("banner_file_id")
    if not banner_id:
        return
    banner_type = db.get_setting("banner_type", "photo")
    try:
        if banner_type == "video":
            await bot.send_video(chat_id=chat_id, video=banner_id)
        else:
            await bot.send_photo(chat_id=chat_id, photo=banner_id)
    except Exception as e:
        logger.warning(f"Could not send banner: {e}")


async def send_styled(bot, chat_id: int, text: str, reply_markup=None, with_banner: bool = True):
    """Send a fancy-font, blockquoted message (with banner above it)."""
    if with_banner:
        await send_banner(bot, chat_id)
    return await bot.send_message(
        chat_id=chat_id,
        text=style.styled(text),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def send_json_block(bot, chat_id: int, data: dict):
    """JSON output is sent as a <pre> block, unstyled, OUTSIDE any
    blockquote (Telegram doesn't allow nesting pre inside blockquote)."""
    pretty = json.dumps(data, indent=2, ensure_ascii=False)
    safe = html.escape(pretty)
    if len(safe) > 3500:
        safe = safe[:3500] + "\n... (truncated)"
    await bot.send_message(chat_id=chat_id, text=f"<pre>{safe}</pre>", parse_mode=ParseMode.HTML)


PROCESSING_FRAMES = [
    "⏳ Processing",
    "⏳ Processing.",
    "⏳ Processing..",
    "⏳ Processing...",
]


async def animate_processing(bot, chat_id: int, message_id: int, stop_event: asyncio.Event,
                              subtitle: str = "Contacting the server, please hold on..."):
    """Live-updates a status message with a small animated loader while an
    API call is in flight, so waiting feels active rather than frozen."""
    i = 0
    while not stop_event.is_set():
        frame = PROCESSING_FRAMES[i % len(PROCESSING_FRAMES)]
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=style.styled(f"{frame}\n━━━━━━━━━━━━━━━\n{subtitle}"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.9)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# MEMBERSHIP / MAINTENANCE GUARD
# ---------------------------------------------------------------------------
def get_required_channels():
    channels = db.list_force_channels()
    if not channels and CHANNEL_ID:
        # fallback to the single .env channel if none configured via /admin
        return [{"chat_id": CHANNEL_ID, "invite_link": CHANNEL_LINK, "title": "Channel"}]
    return channels


async def is_channel_member(bot, chat_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Membership check failed for {user_id} on {chat_id}: {e}")
        return False


async def check_all_channels(bot, user_id: int):
    """Returns (all_joined: bool, missing_channels: list)."""
    channels = get_required_channels()
    missing = []
    for c in channels:
        if not await is_channel_member(bot, c["chat_id"], user_id):
            missing.append(c)
    return (len(missing) == 0, missing)


def force_join_keyboard(missing_channels) -> InlineKeyboardMarkup:
    rows = []
    for c in missing_channels:
        link = c["invite_link"] or f"https://t.me/{str(c['chat_id']).lstrip('@')}"
        rows.append([InlineKeyboardButton(f"📢 Join {c['title']}", url=link, style="primary")])
    rows.append([InlineKeyboardButton("✅ Verify", callback_data="verify", style="success")])
    return InlineKeyboardMarkup(rows)


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Run before any feature handler. Returns True if the user may proceed.
    Admins always bypass maintenance mode and the channel-join requirement."""
    user = update.effective_user

    if db.is_admin(user.id):
        return True

    if db.get_setting("maintenance_mode", "0") == "1":
        await send_styled(
            context.bot,
            user.id,
            "🛠 Under Maintenance\n━━━━━━━━━━━━━━━\nWe're currently performing scheduled maintenance to improve your experience.\n\n⏳ Please check back again shortly. Thank you for your patience! 🙏",
            with_banner=False,
        )
        return False

    ok, missing = await check_all_channels(context.bot, user.id)
    if not ok:
        await send_styled(
            context.bot,
            user.id,
            "🚫 Membership Required\n━━━━━━━━━━━━━━━\n"
            "You need to join all the required channels below before continuing.\n\n"
            "Tap Join, then tap Verify once you're in. ✅",
            reply_markup=force_join_keyboard(missing),
            with_banner=False,
        )
        return False

    return True


# ---------------------------------------------------------------------------
# ACCESS GRANTING (new user bonus / referral bonus)
# ---------------------------------------------------------------------------
def sanitize_name(name: str) -> str:
    """Strip HTML-special characters from a user-supplied name so it can't
    break HTML parse_mode messages (e.g. a Telegram display name containing
    '<' or '&')."""
    name = (name or "").replace("<", "").replace(">", "").replace("&", "").strip()
    return name or "Unknown"


async def grant_access(bot, user, referred_by):
    existing = db.get_user(user.id)

    if existing is None:
        db.create_user(user.id, sanitize_name(user.full_name), referred_by)
        db.update_credits(user.id, WELCOME_BONUS)

        if referred_by and db.get_user(referred_by):
            db.update_credits(referred_by, REFERRAL_BONUS)
            try:
                await send_styled(
                    bot,
                    referred_by,
                    f"🎉 Referral Bonus Unlocked!\n━━━━━━━━━━━━━━━\n"
                    f"Great news — someone just joined using your referral link and "
                    f"successfully verified their membership.\n\n"
                    f"💰 Reward Credited: +{REFERRAL_BONUS} credit\n\n"
                    f"Keep sharing your link to earn even more! 🚀",
                )
            except Exception as e:
                logger.warning(f"Could not notify referrer {referred_by}: {e}")

        welcome_text = (
            f"✅ Verification Successful!\n━━━━━━━━━━━━━━━\n"
            f"Welcome aboard! You now have full access to the bot.\n\n"
            f"🎁 Welcome Bonus: +{WELCOME_BONUS} credits have been added to your wallet\n\n"
            f"✨ Here's what you can do:\n"
            f"🔍 Get Info — instantly look up details for a number\n"
            f"🚀 Blast — run a live, continuously updating query\n"
            f"👤 My Profile — view your credits & stats\n"
            f"🎁 Refer & Earn — invite friends for free credits\n"
            f"🎰 Daily Spin — claim free credits daily, build a streak (unlocks at {MIN_REFERRALS_FOR_SPIN} referrals)\n"
            f"🏆 Leaderboard — see how you rank\n"
            f"🎟 /redeem — enter a promo code for free credits\n\n"
            f"👇 Tap a button below to begin! Send /help anytime for this list again."
        )
    else:
        db.set_verified(user.id)
        welcome_text = (
            "✅ Verified!\n━━━━━━━━━━━━━━━\n"
            "Welcome back! 👋 Your access has been restored — enjoy using the bot."
        )

    await send_styled(bot, user.id, welcome_text, reply_markup=MAIN_MENU)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if db.get_setting("maintenance_mode", "0") == "1" and not db.is_admin(user.id):
        await send_styled(
            context.bot, user.id,
            "🛠 Under Maintenance\n━━━━━━━━━━━━━━━\nWe're currently performing scheduled maintenance to improve your experience.\n\n⏳ Please check back again shortly. Thank you for your patience! 🙏",
            with_banner=False,
        )
        return

    referred_by = None
    if context.args:
        arg = context.args[0]
        if arg.isdigit() and int(arg) != user.id:
            referred_by = int(arg)
            context.user_data["pending_ref"] = referred_by

    ok, missing = await check_all_channels(context.bot, user.id)
    if not ok:
        await send_styled(
            context.bot, user.id,
            "🚫 Access Restricted\n━━━━━━━━━━━━━━━\n"
            "Before you can use this bot, please join our official channel(s) below.\n\n"
            "1️⃣ Tap the Join Channel button\n"
            "2️⃣ Come back here and tap Verify\n\n"
            "It only takes a few seconds! ✅",
            reply_markup=force_join_keyboard(missing),
            with_banner=False,
        )
        return

    await grant_access(context.bot, user, referred_by)


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    ok, missing = await check_all_channels(context.bot, user.id)
    if not ok:
        await query.answer("❌ You haven't joined all the channels yet!", show_alert=True)
        return

    await query.answer("✅ Verified!")
    try:
        await query.message.delete()
    except Exception:
        pass

    referred_by = context.user_data.get("pending_ref")
    await grant_access(context.bot, user, referred_by)


# ---------------------------------------------------------------------------
# MY PROFILE
# ---------------------------------------------------------------------------
async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if row is None:
        await send_styled(context.bot, user.id, "⚠️ Not Registered\n━━━━━━━━━━━━━━━\nPlease send /start to register and unlock access to the bot.")
        return

    referrals = db.get_referral_count(user.id)
    joined = time.strftime("%d %b %Y", time.localtime(row["joined_at"]))

    text = (
        "👤 My Profile\n"
        "━━━━━━━━━━━━━━━\n"
        f"📛 Name: {row['name']}\n"
        f"🆔 User ID: {row['user_id']}\n"
        f"💎 Credits: {row['credits']}\n"
        f"🤝 Total Referrals: {referrals}\n"
        f"📅 Joined: {joined}\n"
        f"✅ Status: Verified\n"
        "━━━━━━━━━━━━━━━\n"
        "Keep referring friends to grow your balance! 🚀"
    )
    await send_styled(context.bot, user.id, text)


# ---------------------------------------------------------------------------
# REFER & EARN
# ---------------------------------------------------------------------------
async def refer_earn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if row is None:
        await send_styled(context.bot, user.id, "⚠️ Not Registered\n━━━━━━━━━━━━━━━\nPlease send /start to register and unlock access to the bot.")
        return

    referrals = db.get_referral_count(user.id)
    link = f"https://t.me/{BOT_USERNAME}?start={user.id}"

    text = (
        "🎁 Refer & Earn\n"
        "━━━━━━━━━━━━━━━\n"
        "Invite your friends and earn free credits for every successful referral!\n\n"
        f"💰 Reward: +{REFERRAL_BONUS} credit per verified friend\n\n"
        "📌 How it works:\n"
        "1️⃣ Share your referral link below\n"
        "2️⃣ Your friend joins the channel & verifies\n"
        "3️⃣ You instantly receive your reward\n\n"
        f"🔗 Your Referral Link:\n{link}\n\n"
        f"🤝 Total Referrals: {referrals}\n"
        f"💎 Current Credits: {row['credits']}\n"
        "━━━━━━━━━━━━━━━\n"
        "The more you share, the more you earn! 🚀"
    )
    await send_styled(context.bot, user.id, text)


# ---------------------------------------------------------------------------
# GET INFO  (1 credit, single API lookup on a 10-digit number)
# ---------------------------------------------------------------------------
async def get_info_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if row is None:
        await send_styled(context.bot, user.id, "⚠️ Not Registered\n━━━━━━━━━━━━━━━\nPlease send /start to register and unlock access to the bot.")
        return

    if row["credits"] < GET_INFO_COST:
        await send_styled(
            context.bot, user.id,
            "❌ Insufficient Balance\n━━━━━━━━━━━━━━━\n"
            "You don't have enough credits to use this feature right now.\n\n"
            f"💎 Required: {GET_INFO_COST} credit\n"
            f"💰 Your Balance: {row['credits']} credit(s)\n\n"
            "👉 Tap 🎁 Refer & Earn to top up your balance for free!",
        )
        return

    context.user_data["awaiting_get_info_number"] = True
    await send_styled(context.bot, user.id, f"🔍 Get Info\n━━━━━━━━━━━━━━━\n"
        f"This lookup costs {GET_INFO_COST} credit.\n\n"
        "📩 Please send me the 10-digit number you'd like to look up.")


async def handle_get_info_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    if not (text.isdigit() and len(text) == 10):
        await send_styled(context.bot, user.id, "⚠️ Invalid Format\n━━━━━━━━━━━━━━━\n"
        "That doesn't look right. Please send a valid 10-digit number and try again.")
        return

    row = db.get_user(user.id)
    if row["credits"] < GET_INFO_COST:
        context.user_data["awaiting_get_info_number"] = False
        await send_styled(
            context.bot, user.id,
            "❌ Insufficient Balance\n━━━━━━━━━━━━━━━\n"
            "👉 Use 🎁 Refer & Earn to get more free credits!",
        )
        return

    context.user_data["awaiting_get_info_number"] = False
    db.update_credits(user.id, -GET_INFO_COST)

    status = await send_styled(context.bot, user.id, f"⏳ Processing\n━━━━━━━━━━━━━━━\n"
        f"Fetching your information from the server, this will only take a moment...")

    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(
        animate_processing(context.bot, user.id, status.message_id, stop_event,
                            subtitle="Fetching your information from the server...")
    )

    try:
        data = await asyncio.to_thread(api_client.fetch_number_info, text)
        stop_event.set()
        await anim_task
        try:
            await status.edit_text(style.styled("✅ Info Found\n━━━━━━━━━━━━━━━\nHere are the details you requested:"), parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await send_json_block(context.bot, user.id, data)
    except Exception as e:
        stop_event.set()
        await anim_task
        logger.error(f"Number lookup failed: {e}")
        db.update_credits(user.id, GET_INFO_COST)  # refund on failure
        try:
            await status.edit_text(
                style.styled(
                    "⚠️ Lookup Failed\n━━━━━━━━━━━━━━━\n"
                    "Something went wrong while contacting the server. Don't worry — "
                    "your credit has been refunded automatically."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# BLAST  (1 credit, continuous API polling for up to 5 minutes)
# ---------------------------------------------------------------------------
async def blast_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if row is None:
        await send_styled(context.bot, user.id, "⚠️ Not Registered\n━━━━━━━━━━━━━━━\nPlease send /start to register and unlock access to the bot.")
        return

    if user.id in active_blasts and not active_blasts[user.id].done():
        await send_styled(context.bot, user.id, "⚠️ Blast Already Running\n━━━━━━━━━━━━━━━\n"
        "You already have an active blast in progress. Please stop it before starting a new one.")
        return

    if row["credits"] < BLAST_COST:
        await send_styled(
            context.bot, user.id,
            "❌ Insufficient Balance\n━━━━━━━━━━━━━━━\n"
            "You don't have enough credits to use this feature right now.\n\n"
            f"💎 Required: {BLAST_COST} credit\n"
            f"💰 Your Balance: {row['credits']} credit(s)\n\n"
            "👉 Tap 🎁 Refer & Earn to top up your balance for free!",
        )
        return

    context.user_data["awaiting_blast_number"] = True
    await send_styled(context.bot, user.id, f"🚀 Blast\n━━━━━━━━━━━━━━━\n"
        f"This feature costs {BLAST_COST} credit and runs continuously for up to 5 minutes.\n\n"
        "📩 Please send me the number you'd like to blast.")


async def handle_blast_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    if not text.isdigit():
        await send_styled(context.bot, user.id, "⚠️ Invalid Input\n━━━━━━━━━━━━━━━\nPlease send a valid number to continue.")
        return

    row = db.get_user(user.id)
    if row["credits"] < BLAST_COST:
        context.user_data["awaiting_blast_number"] = False
        await send_styled(context.bot, user.id, "❌ Insufficient Balance\n━━━━━━━━━━━━━━━\n"
        "👉 Use 🎁 Refer & Earn to get more free credits!")
        return

    context.user_data["awaiting_blast_number"] = False
    db.update_credits(user.id, -BLAST_COST)

    status_msg = await send_styled(context.bot, user.id, "📡 Blast in Progress\n━━━━━━━━━━━━━━━\n"
        "🔄 Round: 0\n"
        "⏱ Live results will appear below — tap Stop anytime to end early.")
    task = asyncio.create_task(run_blast(context.bot, user.id, text, status_msg.message_id))
    active_blasts[user.id] = task


async def run_blast(bot, chat_id: int, number: str, message_id: int):
    start_time = time.time()
    round_num = 0
    stopped_by_user = False

    try:
        while time.time() - start_time < BLAST_DURATION_SECONDS:
            round_num += 1
            try:
                data = api_client.blast_hit(number)
                json_text = json.dumps(data, indent=2, ensure_ascii=False)
            except Exception as e:
                json_text = f"Error: {e}"

            safe_json = html.escape(json_text)
            if len(safe_json) > 2500:
                safe_json = safe_json[:2500] + "\n... (truncated)"

            body = style.styled(
                f"📡 Blast in Progress\n━━━━━━━━━━━━━━━\n"
                f"🔄 Round: {round_num}\n"
                "⏱ Live results below — tap Stop anytime to end early."
            ) + f"\n<pre>{safe_json}</pre>"
            keyboard = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(f"🔄 Round {round_num}", callback_data="noop", style="primary"),
                    InlineKeyboardButton("⏹ Stop", callback_data="stop_blast", style="danger"),
                ]]
            )
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=body,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            except Exception as e:
                logger.warning(f"Blast edit failed: {e}")

            await asyncio.sleep(BLAST_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        stopped_by_user = True
    finally:
        active_blasts.pop(chat_id, None)

    final_text = (
        f"🛑 Blast Stopped\n━━━━━━━━━━━━━━━\n"
        f"The blast was stopped manually.\n"
        f"📊 Total Rounds Completed: {round_num}"
        if stopped_by_user
        else (
            f"✅ Blast Completed\n━━━━━━━━━━━━━━━\n"
            f"The 5-minute session has finished.\n"
            f"📊 Total Rounds Completed: {round_num}"
        )
    )
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=style.styled(final_text),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def stop_blast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    task = active_blasts.get(user.id)
    if task and not task.done():
        task.cancel()
        await query.answer("🛑 Stopping blast...")
    else:
        await query.answer("No active blast running.")


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ---------------------------------------------------------------------------
# DAILY SPIN  (real 🎲 dice, credits = dice value, once per 24h, with a
# streak bonus for spinning on consecutive days — every 3rd day in a row
# adds a small bonus, every 7th day adds a bigger one)
# ---------------------------------------------------------------------------
STREAK_WINDOW_SECONDS = 48 * 60 * 60  # spin again within 48h of the last one to keep the streak


async def daily_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if row is None:
        await send_styled(context.bot, user.id, "⚠️ Not Registered\n━━━━━━━━━━━━━━━\nPlease send /start to register and unlock access to the bot.")
        return

    referrals = db.get_referral_count(user.id)
    if referrals < MIN_REFERRALS_FOR_SPIN:
        needed = MIN_REFERRALS_FOR_SPIN - referrals
        await send_styled(
            context.bot, user.id,
            "🔒 Daily Spin Locked\n━━━━━━━━━━━━━━━\n"
            f"Daily Spin unlocks once you have {MIN_REFERRALS_FOR_SPIN} successful referrals.\n\n"
            f"🤝 Your Referrals: {referrals}/{MIN_REFERRALS_FOR_SPIN}\n"
            f"📩 {needed} more referral(s) needed\n\n"
            "👉 Tap 🎁 Refer & Earn to invite friends and unlock this feature!",
        )
        return

    now = int(time.time())
    remaining = SPIN_COOLDOWN_SECONDS - (now - row["last_spin"])

    if remaining > 0:
        hrs, rem = divmod(remaining, 3600)
        mins = rem // 60
        await send_styled(context.bot, user.id, f"⏳ Daily Spin Already Used\n━━━━━━━━━━━━━━━\n"
        f"You've already claimed today's spin.\n"
        f"🔥 Current Streak: {row['spin_streak']} day(s)\n"
        f"🕒 Next spin available in: {hrs}h {mins}m")
        return

    # Work out the streak: continues if this spin lands within 48h of the
    # last one (i.e. the very next eligible day), otherwise it resets.
    if row["last_spin"] and (now - row["last_spin"]) <= STREAK_WINDOW_SECONDS:
        streak = row["spin_streak"] + 1
    else:
        streak = 1

    dice_msg = await context.bot.send_dice(chat_id=user.id, emoji="🎲")
    value = dice_msg.dice.value
    await asyncio.sleep(4)  # let the dice animation finish

    bonus = 0
    milestone_line = ""
    if streak % 7 == 0:
        bonus = 10
        milestone_line = f"\n🔥🔥 {streak}-Day Streak Milestone! Bonus: +{bonus} credits!"
    elif streak % 3 == 0:
        bonus = 3
        milestone_line = f"\n🔥 {streak}-Day Streak! Bonus: +{bonus} credits!"

    total = value + bonus
    db.update_credits(user.id, total)
    db.record_spin(user.id, now, streak)

    await send_styled(context.bot, user.id, f"🎉 Daily Spin Result\n━━━━━━━━━━━━━━━\n"
        f"🎲 You rolled a {value}!\n"
        f"💎 Credits Won: {value}"
        f"{milestone_line}\n\n"
        f"🔥 Current Streak: {streak} day(s)\n\n"
        "Come back tomorrow to keep your streak alive! ✨")


# ---------------------------------------------------------------------------
# LEADERBOARD  (top 10 by credits, plus the requester's own rank)
# ---------------------------------------------------------------------------
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if row is None:
        await send_styled(context.bot, user.id, "⚠️ Not Registered\n━━━━━━━━━━━━━━━\nPlease send /start to register and unlock access to the bot.")
        return

    top_users = db.top_by_credits(limit=10)
    my_rank = db.get_rank(user.id)

    lines = []
    for i, r in enumerate(top_users, start=1):
        prefix = MEDALS.get(i, f"{i}.")
        marker = "  ← You" if r["user_id"] == user.id else ""
        lines.append(f"{prefix} {r['name']} — 💎{r['credits']}{marker}")

    board = "\n".join(lines) if lines else "No users yet."

    text = (
        "🏆 Leaderboard — Top Credit Holders\n"
        "━━━━━━━━━━━━━━━\n"
        f"{board}\n"
        "━━━━━━━━━━━━━━━\n"
        f"📊 Your Rank: #{my_rank}\n"
        f"💎 Your Credits: {row['credits']}\n\n"
        "Refer friends and spin daily to climb the ranks! 🚀"
    )
    await send_styled(context.bot, user.id, text)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        "📖 Help & Commands\n"
        "━━━━━━━━━━━━━━━\n"
        "🔍 Get Info — look up a number (1 credit)\n"
        "🚀 Blast — live continuous lookup for 5 min (1 credit)\n"
        "👤 My Profile — your ID, credits & stats\n"
        f"🎁 Refer & Earn — +{REFERRAL_BONUS} credit per verified friend\n"
        f"🎰 Daily Spin — free daily credits (needs {MIN_REFERRALS_FOR_SPIN}+ referrals)\n"
        "🏆 Leaderboard — top credit holders\n"
        "━━━━━━━━━━━━━━━\n"
        "🔤 Commands:\n"
        "/start — register / re-verify\n"
        "/help — show this message\n"
        "/redeem <code> — redeem a promo/gift code"
    )
    if db.is_admin(user.id):
        text += "\n/admin — open the admin panel"
    await send_styled(context.bot, user.id, text, reply_markup=MAIN_MENU)


# ---------------------------------------------------------------------------
# /redeem  (promo/gift codes for free credits)
# ---------------------------------------------------------------------------
async def process_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_code: str):
    user = update.effective_user
    code = raw_code.strip().upper()

    record = db.get_redeem_code(code)
    if record is None:
        await send_styled(
            context.bot, user.id,
            "❌ Invalid Code\n━━━━━━━━━━━━━━━\nThis code doesn't exist or has expired.",
        )
        return

    if db.has_redeemed(code, user.id):
        await send_styled(
            context.bot, user.id,
            "⚠️ Already Redeemed\n━━━━━━━━━━━━━━━\nYou've already used this code before.",
        )
        return

    if record["used_count"] >= record["max_uses"]:
        await send_styled(
            context.bot, user.id,
            "❌ Code Exhausted\n━━━━━━━━━━━━━━━\nThis code has reached its maximum number of uses.",
        )
        return

    db.redeem_code_for_user(code, user.id, int(time.time()))
    db.update_credits(user.id, record["credits"])
    new_balance = db.get_user(user.id)["credits"]

    await send_styled(
        context.bot, user.id,
        f"✅ Code Redeemed!\n━━━━━━━━━━━━━━━\n"
        f"🎟 Code: {code}\n"
        f"🎁 Credits Added: +{record['credits']}\n"
        f"💎 New Balance: {new_balance}",
    )


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db.get_user(user.id)
    if row is None:
        await send_styled(context.bot, user.id, "⚠️ Not Registered\n━━━━━━━━━━━━━━━\nPlease send /start to register and unlock access to the bot.")
        return

    if not await guard(update, context):
        return

    clear_awaiting_states(context)

    if context.args:
        await process_redeem(update, context, context.args[0])
        return

    context.user_data["awaiting_redeem_code"] = True
    await send_styled(context.bot, user.id, "🎟 Redeem Code\n━━━━━━━━━━━━━━━\nPlease send your promo/gift code.")


# ---------------------------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------------------------
def admin_menu_keyboard() -> InlineKeyboardMarkup:
    maint = db.get_setting("maintenance_mode", "0") == "1"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Total Users", callback_data="adm_users", style="primary")],
            [InlineKeyboardButton("📋 All Users", callback_data="adm_userlist:0", style="primary")],
            [InlineKeyboardButton("📢 Force Channels", callback_data="adm_channels", style="primary")],
            [InlineKeyboardButton(
                f"🛠 Maintenance: {'ON' if maint else 'OFF'}",
                callback_data="adm_maintenance",
                style="danger" if maint else "success",
            )],
            [InlineKeyboardButton("🖼 Set Banner Image/Video", callback_data="adm_banner", style="primary")],
            [InlineKeyboardButton("💰 Add / Remove Credits", callback_data="adm_credits", style="success")],
            [InlineKeyboardButton("🎟 Redeem Codes", callback_data="adm_codes", style="primary")],
            [InlineKeyboardButton("➕ Add Admin", callback_data="adm_addadmin", style="success")],
        ]
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_admin(user.id):
        return
    await send_styled(context.bot, user.id, "⚙️ Admin Panel\n━━━━━━━━━━━━━━━\n"
        "Manage users, channels, maintenance mode and more from the options below.",
        reply_markup=admin_menu_keyboard())


USERS_PER_PAGE = 10


def user_list_view(page: int):
    total = db.count_users()
    total_pages = max(1, -(-total // USERS_PER_PAGE))  # ceil division
    page = max(0, min(page, total_pages - 1))
    rows = db.list_users(offset=page * USERS_PER_PAGE, limit=USERS_PER_PAGE)

    if rows:
        lines = "\n".join(
            f"{page * USERS_PER_PAGE + i + 1}. {r['name']} — ID {r['user_id']} — 💎{r['credits']}"
            for i, r in enumerate(rows)
        )
    else:
        lines = "No users yet."

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_userlist:{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️ Next", callback_data=f"adm_userlist:{page + 1}", style="primary"))

    kb = []
    if nav_row:
        kb.append(nav_row)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back", style="primary")])

    text = style.styled(
        f"📋 All Users (Page {page + 1}/{total_pages})\n━━━━━━━━━━━━━━━\n{lines}\n━━━━━━━━━━━━━━━\n"
        f"Total Users: {total}"
    )
    return text, InlineKeyboardMarkup(kb)


def channels_view():
    channels = db.list_force_channels()
    if channels:
        lines = "\n".join([f"• {c['title']} ({c['chat_id']})" for c in channels])
    else:
        lines = "No channels added yet (falling back to .env CHANNEL_ID)."
    kb = [
        [InlineKeyboardButton(f"➖ Remove {c['title']}", callback_data=f"adm_rmch:{c['id']}", style="danger")]
        for c in channels
    ]
    kb.append([InlineKeyboardButton("➕ Add Channel", callback_data="adm_addch", style="success")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back", style="primary")])
    return (
        style.styled(
            f"📢 Force Channels\n━━━━━━━━━━━━━━━\n{lines}\n━━━━━━━━━━━━━━━\n"
            "Users must join all of the above before they can use the bot."
        ),
        InlineKeyboardMarkup(kb),
    )


def redeem_codes_view():
    codes = db.list_redeem_codes()
    if codes:
        lines = "\n".join(
            f"🎟 {c['code']} — 💎{c['credits']} — {c['used_count']}/{c['max_uses']} used"
            for c in codes
        )
    else:
        lines = "No codes created yet."
    kb = [
        [InlineKeyboardButton(f"🗑 Delete {c['code']}", callback_data=f"adm_delcode:{c['code']}", style="danger")]
        for c in codes
    ]
    kb.append([InlineKeyboardButton("➕ Create Code", callback_data="adm_createcode", style="success")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back", style="primary")])
    return (
        style.styled(f"🎟 Redeem Codes\n━━━━━━━━━━━━━━━\n{lines}"),
        InlineKeyboardMarkup(kb),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if not db.is_admin(user.id):
        await query.answer("Not authorized.", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "adm_users":
        count = db.count_users()
        await query.edit_message_text(
            style.styled(f"👥 Bot Statistics\n━━━━━━━━━━━━━━━\nTotal Registered Users: {count}"), parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )

    elif data.startswith("adm_userlist:"):
        page = int(data.split(":", 1)[1])
        text, kb = user_list_view(page)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm_channels":
        text, kb = channels_view()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm_addch":
        context.user_data["awaiting_channel_add"] = True
        await query.edit_message_text(
            style.styled(
                "➕ Add Force Channel\n━━━━━━━━━━━━━━━\n"
                "Please send the channel details in this exact format:\n\n"
                "@username | https://t.me/username | Channel Title\n\n"
                "Make sure the bot is an admin of that channel."
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("adm_rmch:"):
        row_id = int(data.split(":", 1)[1])
        db.remove_force_channel(row_id)
        text, kb = channels_view()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm_codes":
        text, kb = redeem_codes_view()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm_createcode":
        context.user_data["awaiting_redeem_create"] = True
        await query.edit_message_text(
            style.styled(
                "🎟 Create Redeem Code\n━━━━━━━━━━━━━━━\n"
                "Send: CODE CREDITS [MAX_USES]\n\n"
                "MAX_USES is optional (defaults to 1 = single-use).\n\n"
                "Examples:\n"
                "WELCOME2026 5 100   (100 people, 5 credits each)\n"
                "VIP50 20            (single-use, 20 credits)"
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("adm_delcode:"):
        code = data.split(":", 1)[1]
        db.delete_redeem_code(code)
        text, kb = redeem_codes_view()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm_maintenance":
        cur = db.get_setting("maintenance_mode", "0")
        db.set_setting("maintenance_mode", "0" if cur == "1" else "1")
        await query.edit_message_text(
            style.styled(
            "⚙️ Admin Panel\n━━━━━━━━━━━━━━━\n"
            "Manage users, channels, maintenance mode and more from the options below."
        ), parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )

    elif data == "adm_banner":
        context.user_data["awaiting_banner"] = True
        await query.edit_message_text(
            style.styled(
                "🖼 Set Banner\n━━━━━━━━━━━━━━━\n"
                "Send a photo or video and it will be shown above every single bot reply from now on."
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data == "adm_credits":
        context.user_data["awaiting_credit_adjust"] = True
        await query.edit_message_text(
            style.styled(
                "💰 Add / Remove Credits\n━━━━━━━━━━━━━━━\n"
                "Send the User ID and amount separated by a space.\n\n"
                "Positive number = add credits\n"
                "Negative number = remove credits\n\n"
                "Examples:\n"
                "123456789 5   (adds 5 credits)\n"
                "123456789 -3  (removes 3 credits)"
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data == "adm_addadmin":
        context.user_data["awaiting_new_admin"] = True
        await query.edit_message_text(
            style.styled(
                "➕ Add New Admin\n━━━━━━━━━━━━━━━\n"
                "Please send the numeric Telegram User ID of the person you'd like to grant admin access to."
            ),
            parse_mode=ParseMode.HTML,
        )

    elif data == "adm_back":
        await query.edit_message_text(
            style.styled(
            "⚙️ Admin Panel\n━━━━━━━━━━━━━━━\n"
            "Manage users, channels, maintenance mode and more from the options below."
        ), parse_mode=ParseMode.HTML,
            reply_markup=admin_menu_keyboard(),
        )


async def handle_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not (context.user_data.get("awaiting_banner") and db.is_admin(user.id)):
        return

    if update.message.photo:
        db.set_setting("banner_file_id", update.message.photo[-1].file_id)
        db.set_setting("banner_type", "photo")
    elif update.message.video:
        db.set_setting("banner_file_id", update.message.video.file_id)
        db.set_setting("banner_type", "video")
    else:
        return

    context.user_data["awaiting_banner"] = False
    await send_styled(context.bot, user.id, "✅ Banner Updated\n━━━━━━━━━━━━━━━\nYour new banner will now appear above every bot reply.",
        with_banner=False)


# ---------------------------------------------------------------------------
# GENERIC TEXT ROUTER
# ---------------------------------------------------------------------------
# All the "waiting for input" flags used across the bot. Tapping a main
# menu button always clears every one of these first — this prevents the
# bot getting permanently "stuck" replying "Invalid Input" to every future
# button tap if a previous flow (e.g. Blast/Get Info/admin prompts) was
# left incomplete.
AWAITING_KEYS = [
    "awaiting_get_info_number",
    "awaiting_blast_number",
    "awaiting_channel_add",
    "awaiting_new_admin",
    "awaiting_banner",
    "awaiting_credit_adjust",
    "awaiting_redeem_code",
    "awaiting_redeem_create",
]


def clear_awaiting_states(context: ContextTypes.DEFAULT_TYPE):
    for key in AWAITING_KEYS:
        context.user_data[key] = False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    # --- main menu routes: always take priority, and reset any stuck state ---
    routes = {
        BTN_GET_INFO: get_info_prompt,
        BTN_MY_PROFILE: my_profile,
        BTN_REFER_EARN: refer_earn,
        BTN_DAILY_SPIN: daily_spin,
        BTN_BLAST: blast_prompt,
        BTN_LEADERBOARD: leaderboard,
    }

    if text in routes:
        clear_awaiting_states(context)
        if not await guard(update, context):
            return
        await routes[text](update, context)
        return

    # --- admin awaiting-states (already authorized when the state was set) ---
    if context.user_data.get("awaiting_channel_add") and db.is_admin(user.id):
        context.user_data["awaiting_channel_add"] = False
        parts = [p.strip() for p in text.split("|")]
        if len(parts) != 3:
            await send_styled(
                context.bot, user.id,
                "⚠️ Invalid Format\n━━━━━━━━━━━━━━━\n"
                "Please use this exact format:\n@username | https://t.me/username | Channel Title",
            )
            return
        chat_id, invite_link, title = parts
        db.add_force_channel(chat_id, invite_link, title)
        await send_styled(
            context.bot, user.id,
            f"✅ Channel Added\n━━━━━━━━━━━━━━━\n{title} has been added to the required channel list.",
        )
        return

    if context.user_data.get("awaiting_new_admin") and db.is_admin(user.id):
        context.user_data["awaiting_new_admin"] = False
        if not text.isdigit():
            await send_styled(context.bot, user.id, "⚠️ Invalid ID\n━━━━━━━━━━━━━━━\nPlease send a valid numeric Telegram User ID.")
            return
        db.add_admin(int(text))
        await send_styled(
            context.bot, user.id,
            f"✅ Admin Added\n━━━━━━━━━━━━━━━\nUser ID {text} now has full admin access.",
        )
        return

    if context.user_data.get("awaiting_credit_adjust") and db.is_admin(user.id):
        context.user_data["awaiting_credit_adjust"] = False
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not (parts[1].lstrip("-").isdigit()):
            await send_styled(
                context.bot, user.id,
                "⚠️ Invalid Format\n━━━━━━━━━━━━━━━\n"
                "Please send: USER_ID AMOUNT\nExample: 123456789 5",
            )
            return
        target_id, amount = int(parts[0]), int(parts[1])
        target_row = db.get_user(target_id)
        if target_row is None:
            await send_styled(
                context.bot, user.id,
                f"⚠️ User Not Found\n━━━━━━━━━━━━━━━\nNo user with ID {target_id} has started the bot yet.",
            )
            return
        db.update_credits(target_id, amount)
        new_balance = db.get_user(target_id)["credits"]
        action = "added to" if amount >= 0 else "removed from"
        await send_styled(
            context.bot, user.id,
            f"✅ Credits Updated\n━━━━━━━━━━━━━━━\n"
            f"{abs(amount)} credit(s) {action} User {target_id}.\n"
            f"💎 New Balance: {new_balance}",
        )
        try:
            await send_styled(
                context.bot, target_id,
                "💎 Your Balance Was Updated\n━━━━━━━━━━━━━━━\n"
                f"An admin has adjusted your credits.\n"
                f"💎 New Balance: {new_balance}",
            )
        except Exception as e:
            logger.warning(f"Could not notify user {target_id} of credit change: {e}")
        return

    if context.user_data.get("awaiting_redeem_create") and db.is_admin(user.id):
        context.user_data["awaiting_redeem_create"] = False
        parts = text.split()
        if len(parts) not in (2, 3) or not parts[1].isdigit() or (len(parts) == 3 and not parts[2].isdigit()):
            await send_styled(
                context.bot, user.id,
                "⚠️ Invalid Format\n━━━━━━━━━━━━━━━\n"
                "Please send: CODE CREDITS [MAX_USES]\n\n"
                "Examples:\nWELCOME2026 5 100\nVIP50 20",
            )
            return
        code = parts[0].strip().upper()
        credits = int(parts[1])
        max_uses = int(parts[2]) if len(parts) == 3 else 1
        if db.get_redeem_code(code) is not None:
            await send_styled(
                context.bot, user.id,
                f"⚠️ Code Already Exists\n━━━━━━━━━━━━━━━\nThe code {code} is already in use. Choose a different one.",
            )
            return
        db.create_redeem_code(code, credits, max_uses, user.id)
        await send_styled(
            context.bot, user.id,
            f"✅ Redeem Code Created\n━━━━━━━━━━━━━━━\n"
            f"🎟 Code: {code}\n💎 Credits: {credits}\n👥 Max Uses: {max_uses}",
        )
        return

    if context.user_data.get("awaiting_redeem_code"):
        context.user_data["awaiting_redeem_code"] = False
        if not await guard(update, context):
            return
        await process_redeem(update, context, text)
        return

    # --- feature input states ---
    if context.user_data.get("awaiting_blast_number"):
        if not await guard(update, context):
            return
        await handle_blast_number(update, context)
        return

    if context.user_data.get("awaiting_get_info_number"):
        if not await guard(update, context):
            return
        await handle_get_info_number(update, context)
        return

    await send_styled(context.bot, user.id, "🤔 I Didn't Quite Get That\n━━━━━━━━━━━━━━━\n"
        "Please use one of the menu buttons below to continue.", reply_markup=MAIN_MENU)


# ---------------------------------------------------------------------------
# TINY HTTP SERVER (Render "Web Service" requires the app to bind to a port
# and respond to HTTP requests, even though this bot itself only polls
# Telegram over outgoing HTTPS. This runs in a background thread and just
# answers 200 OK — Render's health check and an external uptime pinger
# (e.g. UptimeRobot) hitting this URL every ~10 min keep the free instance
# from spinning down. If you deploy as a Render "Background Worker" instead,
# this isn't needed — see README for details.
# ---------------------------------------------------------------------------
def start_keepalive_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is running.")

        def log_message(self, format, *args):
            pass  # silence per-request logging

    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Keepalive HTTP server listening on port {port}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def post_init(app: Application):
    """Registers the '/' command menu shown by Telegram clients."""
    await app.bot.set_my_commands([
        BotCommand("start", "Register / verify channel membership"),
        BotCommand("help", "Show help & command list"),
        BotCommand("redeem", "Redeem a promo/gift code"),
    ])


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Please configure your .env file.")

    db.init_db()
    for admin_id in ADMIN_IDS:
        db.add_admin(admin_id)

    if os.getenv("PORT"):  # Render (and most PaaS) set this automatically
        start_keepalive_server()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(stop_blast_callback, pattern="^stop_blast$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_admin_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
