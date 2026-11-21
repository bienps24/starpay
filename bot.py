"""
VIP Stars Payment Bot
---------------------
Sells access to private Telegram VIP channels using Telegram Stars (XTR).

Flow:
  1. Admin creates a plan (/addplan): VIP channel + name + peso price + Stars + duration.
  2. Admin posts to a public channel (/post <plan_id>) -> post gets a
     "⭐ STAR PAY - N Stars" button that deep-links to this bot.
  3. Buyer taps the button -> this bot sends a Stars invoice.
  4. After payment -> bot sends a personal join-request link.
     Only paid users get approved, so a shared link is useless to others.
  5. Timed plans are removed automatically when they expire (with a reminder 1 day before).

Environment variables:
  BOT_TOKEN          Bot token from BotFather (required)
  DATABASE_URL       PostgreSQL connection URL (required)
  ADMIN_IDS          Comma-separated Telegram user IDs of admins (required)
  PHP_PER_STAR       Pesos per Star used for the suggested price (default 0.75)
  SUPPORT_CONTACT    Username shown in /paysupport, e.g. @YourSupport (optional)
  INVITE_LINK_HOURS  Hours before a personal join link expires (default 24)
"""

import functools
import html
import logging
import math
import os
from datetime import datetime, timedelta, timezone

import asyncpg
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MessageOriginChannel,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {
    int(value)
    for value in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if value
}
PHP_PER_STAR = float(os.environ.get("PHP_PER_STAR", "0.75"))
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "").strip()
INVITE_LINK_HOURS = int(os.environ.get("INVITE_LINK_HOURS", "24"))

EXPIRY_CHECK_SECONDS = 600
REMINDER_BEFORE = timedelta(days=1)
MAX_STARS = 10000

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("vip-star-bot")


# ---------------------------------------------------------------------------
# Database schema (tables are prefixed with vip_ so they can share a database)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vip_plans (
    id             SERIAL PRIMARY KEY,
    chat_id        BIGINT NOT NULL,
    chat_title     TEXT NOT NULL,
    title          TEXT NOT NULL,
    php_price      NUMERIC(12, 2) NOT NULL DEFAULT 0,
    stars          INTEGER NOT NULL,
    duration_days  INTEGER NOT NULL DEFAULT 0,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vip_post_channels (
    chat_id   BIGINT PRIMARY KEY,
    title     TEXT NOT NULL,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vip_payments (
    id          SERIAL PRIMARY KEY,
    charge_id   TEXT UNIQUE NOT NULL,
    user_id     BIGINT NOT NULL,
    username    TEXT,
    plan_id     INTEGER NOT NULL,
    stars       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'paid',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vip_memberships (
    user_id     BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    plan_id     INTEGER NOT NULL,
    expires_at  TIMESTAMPTZ,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    reminded    BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, chat_id)
);

CREATE INDEX IF NOT EXISTS vip_memberships_expiry_idx
    ON vip_memberships (active, expires_at);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_pool(context: ContextTypes.DEFAULT_TYPE) -> asyncpg.Pool:
    return context.application.bot_data["db"]


def esc(value) -> str:
    return html.escape(str(value))


def duration_text(days: int) -> str:
    return "Lifetime access" if days == 0 else f"{days} days access"


def format_expiry(expires_at) -> str:
    if expires_at is None:
        return "Lifetime"
    return expires_at.strftime("%b %d, %Y %H:%M UTC")


def suggest_stars(php_price: float) -> int:
    return max(1, math.ceil(php_price / PHP_PER_STAR))


def deep_link(bot_username: str, plan_id: int) -> str:
    return f"https://t.me/{bot_username}?start=vip_{plan_id}"


def star_pay_keyboard(bot_username: str, plan) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(
        f"⭐ STAR PAY - {plan['stars']} Stars",
        url=deep_link(bot_username, plan["id"]),
    )
    return InlineKeyboardMarkup([[button]])


def admin_only(handler):
    """Silently ignore commands from non-admins."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id not in ADMIN_IDS:
            return
        return await handler(update, context)

    return wrapper


async def get_plan(context: ContextTypes.DEFAULT_TYPE, plan_id: int):
    async with get_pool(context).acquire() as conn:
        return await conn.fetchrow("SELECT * FROM vip_plans WHERE id = $1", plan_id)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as error:
            log.warning("Could not notify admin %s: %s", admin_id, error)


async def kick_member(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    """Remove a user without leaving a permanent ban."""
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except TelegramError as error:
        log.warning("Could not remove %s from %s: %s", user_id, chat_id, error)


async def check_bot_rights(
    context: ContextTypes.DEFAULT_TYPE,
    chat,
    need_invite: bool,
    need_post: bool,
):
    """Return an error message if the bot lacks the rights it needs, otherwise None."""
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
    except TelegramError as error:
        return f"I can't access that chat ({esc(error.message)}). Add me as an admin first."

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return "I'm not an admin in that chat yet. Add me as an admin, then try again."

    missing = []
    if need_invite and not member.can_invite_users:
        missing.append("Invite Users via Link")
    if need_invite and chat.type != ChatType.CHANNEL and not member.can_restrict_members:
        missing.append("Ban Users")
    if need_post and chat.type == ChatType.CHANNEL and not member.can_post_messages:
        missing.append("Post Messages")

    if missing:
        return "I'm missing these admin rights: <b>" + ", ".join(missing) + "</b>"
    return None


async def resolve_chat_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept a forwarded channel message, a -100... chat ID, or an @username."""
    message = update.message
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat

    text = (message.text or "").strip()
    if not text:
        return None
    try:
        if text.startswith("-"):
            return await context.bot.get_chat(int(text))
        if text.startswith("@"):
            return await context.bot.get_chat(text)
    except (TelegramError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# Buyer side
# ---------------------------------------------------------------------------

async def send_plan_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, plan) -> None:
    description = (
        f"{plan['chat_title']} — {duration_text(plan['duration_days'])}. "
        "Your private join link is sent instantly after payment."
    )
    try:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=plan["title"][:32],
            description=description[:255],
            payload=f"vip:{plan['id']}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(plan["title"][:32], plan["stars"])],
        )
    except TelegramError as error:
        log.error("Could not send invoice for plan %s: %s", plan["id"], error)
        await context.bot.send_message(
            chat_id,
            "Sorry, payment is temporarily unavailable for this plan. Please try again later.",
        )
        await notify_admins(
            context,
            f"⚠️ Invoice failed for plan #{plan['id']} ({esc(plan['title'])}): {esc(error.message)}",
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    args = context.args
    if args and args[0].startswith("vip_"):
        try:
            plan_id = int(args[0][4:])
        except ValueError:
            plan_id = None

        plan = await get_plan(context, plan_id) if plan_id else None
        if plan is None or not plan["active"]:
            await update.message.reply_text("This VIP plan is no longer available.")
            return

        await update.message.reply_text(
            f"⭐ <b>{esc(plan['title'])}</b>\n"
            f"Channel: {esc(plan['chat_title'])}\n"
            f"Access: {duration_text(plan['duration_days'])}\n"
            f"Price: <b>{plan['stars']} Stars</b>\n\n"
            "Tap <b>Pay</b> on the invoice below. Your join link arrives right after payment.",
            parse_mode=ParseMode.HTML,
        )
        await send_plan_invoice(context, update.effective_chat.id, plan)
        return

    async with get_pool(context).acquire() as conn:
        plans = await conn.fetch("SELECT * FROM vip_plans WHERE active ORDER BY id")

    if not plans:
        await update.message.reply_text("Welcome! There are no VIP plans available right now.")
        return

    buttons = [
        [InlineKeyboardButton(f"⭐ {plan['title']} — {plan['stars']} Stars", callback_data=f"buy:{plan['id']}")]
        for plan in plans
    ]
    await update.message.reply_text(
        "👋 <b>Welcome!</b>\n\n"
        "Pick a VIP plan below and pay with Telegram Stars. "
        "Your private join link is sent instantly after payment.\n\n"
        "Already paid? Send /myvip",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_buy_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    plan = await get_plan(context, int(query.data.split(":")[1]))
    if plan is None or not plan["active"]:
        await query.message.reply_text("This VIP plan is no longer available.")
        return
    await send_plan_invoice(context, query.message.chat.id, plan)


async def on_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    plan = None
    if query.invoice_payload.startswith("vip:"):
        try:
            plan = await get_plan(context, int(query.invoice_payload[4:]))
        except ValueError:
            plan = None

    if plan is None or not plan["active"]:
        await query.answer(ok=False, error_message="This VIP plan is no longer available.")
        return
    if query.currency != "XTR" or query.total_amount != plan["stars"]:
        await query.answer(
            ok=False,
            error_message="The price has changed. Please tap the STAR PAY button again.",
        )
        return
    await query.answer(ok=True)


async def grant_access(pool: asyncpg.Pool, user_id: int, plan):
    """Create or extend a membership. Returns the new expiry (None = lifetime)."""
    now = utcnow()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT expires_at, active FROM vip_memberships "
                "WHERE user_id = $1 AND chat_id = $2 FOR UPDATE",
                user_id,
                plan["chat_id"],
            )

            if plan["duration_days"] == 0:
                new_expiry = None
            elif row and row["active"] and row["expires_at"] is None:
                new_expiry = None  # Already has lifetime access
            else:
                base = now
                if row and row["active"] and row["expires_at"] and row["expires_at"] > now:
                    base = row["expires_at"]  # Renewal stacks on top of remaining time
                new_expiry = base + timedelta(days=plan["duration_days"])

            await conn.execute(
                """
                INSERT INTO vip_memberships (user_id, chat_id, plan_id, expires_at, active, reminded, updated_at)
                VALUES ($1, $2, $3, $4, TRUE, FALSE, NOW())
                ON CONFLICT (user_id, chat_id) DO UPDATE SET
                    plan_id = EXCLUDED.plan_id,
                    expires_at = EXCLUDED.expires_at,
                    active = TRUE,
                    reminded = FALSE,
                    updated_at = NOW()
                """,
                user_id,
                plan["chat_id"],
                plan["id"],
                new_expiry,
            )
    return new_expiry


async def send_join_link(context: ContextTypes.DEFAULT_TYPE, user_id: int, plan) -> bool:
    """Create a personal join-request link. Only paid users are approved."""
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id=plan["chat_id"],
            name=f"Paid {user_id}"[:32],
            expire_date=utcnow() + timedelta(hours=INVITE_LINK_HOURS),
            creates_join_request=True,
        )
    except TelegramError as error:
        log.error("Invite link failed for plan %s: %s", plan["id"], error)
        await context.bot.send_message(
            user_id,
            "✅ Payment received, but I couldn't create your join link right now. "
            "Our team has been notified and will send it shortly. You can also try /myvip in a few minutes.",
        )
        await notify_admins(
            context,
            f"⚠️ Could not create a join link for <b>{esc(plan['chat_title'])}</b> "
            f"(plan #{plan['id']}): {esc(error.message)}\n"
            f"Buyer ID: <code>{user_id}</code>\n"
            "Make sure the bot is an admin there with 'Invite Users via Link'.",
        )
        return False

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔓 Join VIP Channel", url=link.invite_link)]]
    )
    await context.bot.send_message(
        user_id,
        f"Tap the button below to join <b>{esc(plan['chat_title'])}</b>.\n\n"
        f"• This link only works for your account.\n"
        f"• It expires in {INVITE_LINK_HOURS} hours — send /myvip anytime for a fresh one.\n"
        f"• Already inside? No need to rejoin, your access was extended.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return True


async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    payment = message.successful_payment
    user = update.effective_user
    pool = get_pool(context)

    if not payment.invoice_payload.startswith("vip:"):
        return
    plan_id = int(payment.invoice_payload[4:])

    # Record payment once (Telegram can redeliver updates)
    async with pool.acquire() as conn:
        inserted = await conn.fetchval(
            """
            INSERT INTO vip_payments (charge_id, user_id, username, plan_id, stars)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (charge_id) DO NOTHING
            RETURNING id
            """,
            payment.telegram_payment_charge_id,
            user.id,
            user.username,
            plan_id,
            payment.total_amount,
        )
    if inserted is None:
        return

    plan = await get_plan(context, plan_id)
    if plan is None:
        await message.reply_text("✅ Payment received. Our team will contact you shortly.")
        await notify_admins(
            context,
            f"⚠️ Payment for missing plan #{plan_id} by <code>{user.id}</code>. "
            f"Charge: <code>{esc(payment.telegram_payment_charge_id)}</code>",
        )
        return

    expires_at = await grant_access(pool, user.id, plan)

    await message.reply_text(
        f"✅ <b>Payment received — thank you!</b>\n\n"
        f"Plan: {esc(plan['title'])}\n"
        f"Access until: <b>{format_expiry(expires_at)}</b>",
        parse_mode=ParseMode.HTML,
    )
    await send_join_link(context, user.id, plan)

    buyer = f"@{esc(user.username)}" if user.username else esc(user.full_name)
    await notify_admins(
        context,
        f"💰 <b>New sale</b> — {payment.total_amount} ⭐\n"
        f"Plan: #{plan['id']} {esc(plan['title'])}\n"
        f"Buyer: {buyer} (<code>{user.id}</code>)\n"
        f"Until: {format_expiry(expires_at)}\n"
        f"Charge: <code>{esc(payment.telegram_payment_charge_id)}</code>",
    )


async def cmd_myvip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    async with get_pool(context).acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.chat_id, m.expires_at, p.title
            FROM vip_memberships m
            JOIN vip_plans p ON p.id = m.plan_id
            WHERE m.user_id = $1 AND m.active
              AND (m.expires_at IS NULL OR m.expires_at > NOW())
            ORDER BY m.updated_at DESC
            """,
            update.effective_user.id,
        )

    if not rows:
        await update.message.reply_text(
            "You don't have an active VIP pass yet. Send /start to see the plans."
        )
        return

    lines = ["🎟 <b>Your VIP passes</b>\n"]
    buttons = []
    for row in rows:
        lines.append(f"• {esc(row['title'])} — until {format_expiry(row['expires_at'])}")
        buttons.append(
            [InlineKeyboardButton(f"🔓 Get join link: {row['title']}", callback_data=f"link:{row['chat_id']}")]
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_link_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    user_id = query.from_user.id

    async with get_pool(context).acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT plan_id FROM vip_memberships
            WHERE user_id = $1 AND chat_id = $2 AND active
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            user_id,
            chat_id,
        )

    if row is None:
        await query.answer("Your VIP pass for this channel has ended.", show_alert=True)
        return

    await query.answer()
    plan = await get_plan(context, row["plan_id"])
    if plan:
        await send_join_link(context, user_id, plan)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request = update.chat_join_request
    chat_id = request.chat.id
    user_id = request.from_user.id

    async with get_pool(context).acquire() as conn:
        managed = await conn.fetchval("SELECT 1 FROM vip_plans WHERE chat_id = $1 LIMIT 1", chat_id)
        if not managed:
            return  # Not one of our VIP channels
        row = await conn.fetchrow(
            """
            SELECT 1 FROM vip_memberships
            WHERE user_id = $1 AND chat_id = $2 AND active
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            user_id,
            chat_id,
        )

    try:
        if row:
            await request.approve()
            return

        await request.decline()
        await context.bot.send_message(
            user_id,
            "You don't have an active VIP pass for this channel yet. "
            "Tap the STAR PAY button on the post to get access.",
        )
    except TelegramError as error:
        log.warning("Join request handling failed for %s in %s: %s", user_id, chat_id, error)


# ---------------------------------------------------------------------------
# Expiry and reminders
# ---------------------------------------------------------------------------

async def expiry_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    pool = get_pool(context)
    now = utcnow()

    async with pool.acquire() as conn:
        expired = await conn.fetch(
            """
            SELECT user_id, chat_id, plan_id FROM vip_memberships
            WHERE active AND expires_at IS NOT NULL AND expires_at <= $1
            """,
            now,
        )
        due_soon = await conn.fetch(
            """
            SELECT user_id, chat_id, plan_id, expires_at FROM vip_memberships
            WHERE active AND NOT reminded AND expires_at IS NOT NULL
              AND expires_at > $1 AND expires_at <= $2
            """,
            now,
            now + REMINDER_BEFORE,
        )

    for row in expired:
        await kick_member(context, row["chat_id"], row["user_id"])
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE vip_memberships SET active = FALSE, updated_at = NOW() "
                "WHERE user_id = $1 AND chat_id = $2",
                row["user_id"],
                row["chat_id"],
            )

        plan = await get_plan(context, row["plan_id"])
        if plan and plan["active"]:
            try:
                await context.bot.send_message(
                    row["user_id"],
                    f"⏰ Your VIP pass for <b>{esc(plan['chat_title'])}</b> has ended.\n"
                    "Renew anytime to get back in.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=star_pay_keyboard(context.bot.username, plan),
                )
            except TelegramError:
                pass

    for row in due_soon:
        plan = await get_plan(context, row["plan_id"])
        if plan and plan["active"]:
            try:
                await context.bot.send_message(
                    row["user_id"],
                    f"🔔 Your VIP pass for <b>{esc(plan['chat_title'])}</b> ends on "
                    f"<b>{format_expiry(row['expires_at'])}</b>.\n"
                    "Renew now — the extra days are added on top of your remaining time.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=star_pay_keyboard(context.bot.username, plan),
                )
            except TelegramError:
                pass
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE vip_memberships SET reminded = TRUE WHERE user_id = $1 AND chat_id = $2",
                row["user_id"],
                row["chat_id"],
            )


# ---------------------------------------------------------------------------
# Required payment commands
# ---------------------------------------------------------------------------

async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact = SUPPORT_CONTACT or "the channel admin"
    await update.message.reply_text(
        f"💬 For payment issues, contact {contact}.\n"
        f"Please include your Telegram ID: {update.effective_user.id}"
    )


async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📄 Terms\n\n"
        "• Payments are made in Telegram Stars for access to private VIP channels.\n"
        "• Access starts right after payment and lasts for the period shown on the plan.\n"
        "• Sharing your join link does not work — only the buyer's account is approved.\n"
        "• Access may be removed for abuse, spam, or leaking channel content.\n"
        "• For payment problems use /paysupport."
    )


# ---------------------------------------------------------------------------
# Admin: plans
# ---------------------------------------------------------------------------

@admin_only
async def cmd_addplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = {"name": "addplan", "step": "channel", "data": {}}
    await update.message.reply_text(
        "<b>New VIP plan — Step 1/5</b>\n\n"
        "Forward any message from the <b>VIP channel</b> here, "
        "or send its chat ID (-100...) or @username.\n\n"
        "The bot must be an admin there with <b>Invite Users via Link</b>.\n\n"
        "Send /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )


async def addplan_step(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    message = update.message
    step = flow["step"]
    data = flow["data"]
    text = (message.text or "").strip()

    if step == "channel":
        chat = await resolve_chat_from_message(update, context)
        if chat is None:
            await message.reply_text("I couldn't read that chat. Forward a message from the channel or send its -100... ID.")
            return
        problem = await check_bot_rights(context, chat, need_invite=True, need_post=False)
        if problem:
            await message.reply_text(problem, parse_mode=ParseMode.HTML)
            return
        data["chat_id"] = chat.id
        data["chat_title"] = chat.title or str(chat.id)
        flow["step"] = "title"
        await message.reply_text(
            f"✅ VIP channel: <b>{esc(data['chat_title'])}</b>\n\n"
            "<b>Step 2/5</b> — Send the plan name buyers will see (max 32 characters).\n"
            "Example: <code>VIP Channel 30 Days</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "title":
        if not text or len(text) > 32:
            await message.reply_text("Please send a name between 1 and 32 characters.")
            return
        data["title"] = text
        flow["step"] = "php"
        await message.reply_text(
            "<b>Step 3/5</b> — Price in pesos (example: <code>500</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "php":
        try:
            php_price = float(text.replace(",", "").replace("₱", ""))
        except ValueError:
            php_price = 0
        if php_price <= 0:
            await message.reply_text("Please send a valid peso amount, like 500.")
            return
        data["php_price"] = php_price
        suggested = suggest_stars(php_price)
        flow["step"] = "stars"
        await message.reply_text(
            f"<b>Step 4/5</b> — ₱{php_price:,.2f} ≈ <b>{suggested} Stars</b> "
            f"(rate: ₱{PHP_PER_STAR} per Star)\n\n"
            "Tap the button to use it, or send a different Star amount.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"Use {suggested} ⭐", callback_data=f"fs:{suggested}")]]
            ),
        )
        return

    if step == "stars":
        if not text.isdigit() or not 1 <= int(text) <= MAX_STARS:
            await message.reply_text(f"Please send a whole number from 1 to {MAX_STARS}.")
            return
        await addplan_set_stars(message, flow, int(text))
        return

    if step == "days":
        if not text.isdigit():
            await message.reply_text("Please send a whole number. Use 0 for lifetime.")
            return
        data["duration_days"] = int(text)
        context.user_data.pop("flow", None)
        await save_plan_and_show(update, context, data)


async def addplan_set_stars(message, flow: dict, stars: int) -> None:
    flow["data"]["stars"] = stars
    flow["step"] = "days"
    await message.reply_text(
        f"✅ Price: <b>{stars} Stars</b>\n\n"
        "<b>Step 5/5</b> — Access duration in days.\n"
        "Send <code>30</code> for 30 days, or <code>0</code> for lifetime.",
        parse_mode=ParseMode.HTML,
    )


async def on_flow_stars_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer()
        return
    flow = context.user_data.get("flow")
    if not flow or flow.get("name") != "addplan" or flow.get("step") != "stars":
        await query.answer("This step has expired.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await addplan_set_stars(query.message, flow, int(query.data.split(":")[1]))


async def save_plan_and_show(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    async with get_pool(context).acquire() as conn:
        plan = await conn.fetchrow(
            """
            INSERT INTO vip_plans (chat_id, chat_title, title, php_price, stars, duration_days)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            data["chat_id"],
            data["chat_title"],
            data["title"],
            data["php_price"],
            data["stars"],
            data["duration_days"],
        )

    link = deep_link(context.bot.username, plan["id"])
    await update.message.reply_text(
        f"🎉 <b>Plan #{plan['id']} created</b>\n\n"
        f"Name: {esc(plan['title'])}\n"
        f"VIP channel: {esc(plan['chat_title'])}\n"
        f"Price: ₱{plan['php_price']:,.2f} → <b>{plan['stars']} Stars</b>\n"
        f"Access: {duration_text(plan['duration_days'])}\n\n"
        f"Pay link:\n<code>{link}</code>\n\n"
        f"Post it with a button: /post {plan['id']}\n\n"
        "Button preview:",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await update.message.reply_text(
        "👇 This is how the button looks on your post.",
        reply_markup=star_pay_keyboard(context.bot.username, plan),
    )


@admin_only
async def cmd_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with get_pool(context).acquire() as conn:
        plans = await conn.fetch("SELECT * FROM vip_plans ORDER BY id")

    if not plans:
        await update.message.reply_text("No plans yet. Create one with /addplan")
        return

    blocks = []
    for plan in plans:
        status = "🟢 ACTIVE" if plan["active"] else "🔴 OFF"
        blocks.append(
            f"<b>#{plan['id']} {esc(plan['title'])}</b> — {status}\n"
            f"Channel: {esc(plan['chat_title'])}\n"
            f"₱{plan['php_price']:,.2f} → {plan['stars']} ⭐ · {duration_text(plan['duration_days'])}\n"
            f"<code>{deep_link(context.bot.username, plan['id'])}</code>"
        )

    footer = (
        "\n\n<b>Commands</b>\n"
        "/post ID — post with STAR PAY button\n"
        "/setstars ID STARS — change price\n"
        "/setdays ID DAYS — change duration (0 = lifetime)\n"
        "/toggle ID — turn a plan on/off"
    )
    await update.message.reply_text(
        "\n\n".join(blocks) + footer,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def parse_plan_args(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    needs_value: bool,
    usage: str,
):
    """Parse 'ID' or 'ID VALUE' arguments. Replies with usage/errors and returns (None, None) on failure."""
    args = context.args
    if not args or not args[0].isdigit() or (needs_value and (len(args) < 2 or not args[1].isdigit())):
        await update.message.reply_text(f"Usage: {usage}")
        return None, None
    plan = await get_plan(context, int(args[0]))
    if plan is None:
        await update.message.reply_text("Plan not found. See /plans")
        return None, None
    value = int(args[1]) if needs_value else None
    return plan, value


@admin_only
async def cmd_setstars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan, stars = await parse_plan_args(update, context, True, "/setstars ID STARS")
    if plan is None:
        return
    if not 1 <= stars <= MAX_STARS:
        await update.message.reply_text(f"Stars must be from 1 to {MAX_STARS}.")
        return
    async with get_pool(context).acquire() as conn:
        await conn.execute("UPDATE vip_plans SET stars = $1 WHERE id = $2", stars, plan["id"])
    await update.message.reply_text(
        f"✅ Plan #{plan['id']} is now {stars} Stars.\n"
        "Note: buttons on old posts still show the old number — repost with /post to update them."
    )


@admin_only
async def cmd_setdays(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan, days = await parse_plan_args(update, context, True, "/setdays ID DAYS (0 = lifetime)")
    if plan is None:
        return
    async with get_pool(context).acquire() as conn:
        await conn.execute("UPDATE vip_plans SET duration_days = $1 WHERE id = $2", days, plan["id"])
    await update.message.reply_text(f"✅ Plan #{plan['id']}: {duration_text(days)} for new buyers.")


@admin_only
async def cmd_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan, _ = await parse_plan_args(update, context, False, "/toggle ID")
    if plan is None:
        return
    async with get_pool(context).acquire() as conn:
        active = await conn.fetchval(
            "UPDATE vip_plans SET active = NOT active WHERE id = $1 RETURNING active", plan["id"]
        )
    await update.message.reply_text(f"Plan #{plan['id']} is now {'🟢 ACTIVE' if active else '🔴 OFF'}.")


# ---------------------------------------------------------------------------
# Admin: post channels and posting
# ---------------------------------------------------------------------------

@admin_only
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = {"name": "addchannel", "step": "channel", "data": {}}
    await update.message.reply_text(
        "<b>Add a posting channel</b>\n\n"
        "Forward any message from the channel where STAR PAY posts will go, "
        "or send its chat ID (-100...) or @username.\n\n"
        "The bot must be an admin there with <b>Post Messages</b>.\n\n"
        "Send /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )


async def addchannel_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = await resolve_chat_from_message(update, context)
    if chat is None:
        await update.message.reply_text("I couldn't read that chat. Forward a message from the channel or send its -100... ID.")
        return
    problem = await check_bot_rights(context, chat, need_invite=False, need_post=True)
    if problem:
        await update.message.reply_text(problem, parse_mode=ParseMode.HTML)
        return

    async with get_pool(context).acquire() as conn:
        await conn.execute(
            """
            INSERT INTO vip_post_channels (chat_id, title) VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
            """,
            chat.id,
            chat.title or str(chat.id),
        )
    context.user_data.pop("flow", None)
    await update.message.reply_text(
        f"✅ Posting channel added: <b>{esc(chat.title)}</b>\nSee all with /channels",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with get_pool(context).acquire() as conn:
        rows = await conn.fetch("SELECT * FROM vip_post_channels ORDER BY added_at")
    if not rows:
        await update.message.reply_text("No posting channels yet. Add one with /addchannel")
        return
    lines = ["📢 <b>Posting channels</b>\n"]
    for row in rows:
        lines.append(f"• {esc(row['title'])} — <code>{row['chat_id']}</code>")
    lines.append("\nRemove one with /delchannel CHAT_ID")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_delchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /delchannel CHAT_ID")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /delchannel CHAT_ID")
        return
    async with get_pool(context).acquire() as conn:
        result = await conn.execute("DELETE FROM vip_post_channels WHERE chat_id = $1", chat_id)
    removed = result.endswith("1")
    await update.message.reply_text("✅ Removed." if removed else "Channel not found. See /channels")


@admin_only
async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plan, _ = await parse_plan_args(update, context, False, "/post PLAN_ID — see /plans")
    if plan is None:
        return
    if not plan["active"]:
        await update.message.reply_text("That plan is OFF. Turn it on first with /toggle")
        return

    context.user_data["flow"] = {"name": "post", "step": "content", "data": {"plan_id": plan["id"]}}
    await update.message.reply_text(
        f"📝 <b>Post for plan #{plan['id']} ({plan['stars']} ⭐)</b>\n\n"
        "Send the post now — text, photo, video, or GIF with caption (one message, not an album).\n"
        "The STAR PAY button will be attached automatically.\n\n"
        "Send /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )


async def post_content_step(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    message = update.message
    plan = await get_plan(context, flow["data"]["plan_id"])
    context.user_data.pop("flow", None)
    if plan is None:
        await message.reply_text("Plan not found.")
        return

    async with get_pool(context).acquire() as conn:
        channels = await conn.fetch("SELECT * FROM vip_post_channels ORDER BY added_at")

    keyboard = star_pay_keyboard(context.bot.username, plan)

    # Preview exactly how the post will look
    await context.bot.copy_message(
        chat_id=message.chat.id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_markup=keyboard,
    )

    context.user_data["pending_post"] = {
        "plan_id": plan["id"],
        "from_chat_id": message.chat.id,
        "message_id": message.message_id,
    }

    if not channels:
        await message.reply_text(
            "👆 Preview above. You have no posting channels yet — add one with /addchannel, "
            "then run /post again.\n\nYou can also forward the preview manually."
        )
        return

    buttons = [
        [InlineKeyboardButton(f"📢 {row['title']}", callback_data=f"postto:{row['chat_id']}")]
        for row in channels
    ]
    if len(channels) > 1:
        buttons.append([InlineKeyboardButton("📢 All channels", callback_data="postto:all")])
    buttons.append([InlineKeyboardButton("✖️ Cancel", callback_data="postto:cancel")])

    await message.reply_text(
        "👆 Preview above. Where should I post it?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_postto_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer()
        return

    target = query.data.split(":", 1)[1]
    pending = context.user_data.get("pending_post")

    if target == "cancel":
        context.user_data.pop("pending_post", None)
        await query.answer()
        await query.edit_message_text("Cancelled.")
        return
    if not pending:
        await query.answer("This post has expired. Run /post again.", show_alert=True)
        return

    plan = await get_plan(context, pending["plan_id"])
    if plan is None:
        await query.answer("Plan not found.", show_alert=True)
        return

    async with get_pool(context).acquire() as conn:
        if target == "all":
            channels = await conn.fetch("SELECT * FROM vip_post_channels ORDER BY added_at")
        else:
            channels = await conn.fetch("SELECT * FROM vip_post_channels WHERE chat_id = $1", int(target))

    await query.answer("Posting...")
    keyboard = star_pay_keyboard(context.bot.username, plan)
    results = []
    for row in channels:
        try:
            await context.bot.copy_message(
                chat_id=row["chat_id"],
                from_chat_id=pending["from_chat_id"],
                message_id=pending["message_id"],
                reply_markup=keyboard,
            )
            results.append(f"✅ {esc(row['title'])}")
        except TelegramError as error:
            results.append(f"❌ {esc(row['title'])} — {esc(error.message)}")

    context.user_data.pop("pending_post", None)
    await query.edit_message_text(
        "<b>Post results</b>\n" + "\n".join(results),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Admin: stats and refunds
# ---------------------------------------------------------------------------

@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with get_pool(context).acquire() as conn:
        total = await conn.fetchrow(
            "SELECT COUNT(*) AS n, COALESCE(SUM(stars), 0) AS s FROM vip_payments WHERE status = 'paid'"
        )
        today = await conn.fetchrow(
            "SELECT COUNT(*) AS n, COALESCE(SUM(stars), 0) AS s FROM vip_payments "
            "WHERE status = 'paid' AND created_at > NOW() - INTERVAL '24 hours'"
        )
        members = await conn.fetchval(
            "SELECT COUNT(*) FROM vip_memberships WHERE active "
            "AND (expires_at IS NULL OR expires_at > NOW())"
        )
        recent = await conn.fetch(
            """
            SELECT pay.*, p.title FROM vip_payments pay
            LEFT JOIN vip_plans p ON p.id = pay.plan_id
            ORDER BY pay.created_at DESC LIMIT 10
            """
        )

    lines = [
        "📊 <b>Sales</b>\n",
        f"Last 24h: <b>{today['n']}</b> sales · <b>{today['s']}</b> ⭐",
        f"All time: <b>{total['n']}</b> sales · <b>{total['s']}</b> ⭐",
        f"Active VIP members: <b>{members}</b>",
    ]
    if recent:
        lines.append("\n<b>Recent payments</b>")
        for pay in recent:
            who = f"@{esc(pay['username'])}" if pay["username"] else f"<code>{pay['user_id']}</code>"
            tag = "" if pay["status"] == "paid" else f" [{pay['status'].upper()}]"
            lines.append(
                f"• {pay['created_at']:%b %d %H:%M} — {who} — {pay['stars']} ⭐ — "
                f"{esc(pay['title'] or '#' + str(pay['plan_id']))}{tag}\n"
                f"  <code>{esc(pay['charge_id'])}</code>"
            )
        lines.append("\nRefund with /refund CHARGE_ID")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_refund(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /refund CHARGE_ID (see /stats)")
        return
    charge_id = context.args[0].strip()
    pool = get_pool(context)

    async with pool.acquire() as conn:
        payment = await conn.fetchrow("SELECT * FROM vip_payments WHERE charge_id = $1", charge_id)
    if payment is None:
        await update.message.reply_text("Payment not found.")
        return
    if payment["status"] != "paid":
        await update.message.reply_text(f"This payment is already {payment['status']}.")
        return

    try:
        await context.bot.refund_star_payment(
            user_id=payment["user_id"],
            telegram_payment_charge_id=charge_id,
        )
    except TelegramError as error:
        await update.message.reply_text(f"Refund failed: {error.message}")
        return

    plan = await get_plan(context, payment["plan_id"])
    async with pool.acquire() as conn:
        await conn.execute("UPDATE vip_payments SET status = 'refunded' WHERE charge_id = $1", charge_id)
        if plan:
            await conn.execute(
                "UPDATE vip_memberships SET active = FALSE, updated_at = NOW() "
                "WHERE user_id = $1 AND chat_id = $2",
                payment["user_id"],
                plan["chat_id"],
            )
    if plan:
        await kick_member(context, plan["chat_id"], payment["user_id"])

    await update.message.reply_text(
        f"✅ Refunded {payment['stars']} ⭐ to {payment['user_id']} and removed their VIP access."
    )


# ---------------------------------------------------------------------------
# Admin: flow routing and misc
# ---------------------------------------------------------------------------

@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("flow", None)
    context.user_data.pop("pending_post", None)
    await update.message.reply_text("Cancelled.")


@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🛠 <b>Admin commands</b>\n\n"
        "/addplan — create a VIP plan\n"
        "/plans — list plans and pay links\n"
        "/post ID — post with STAR PAY button\n"
        "/setstars ID STARS · /setdays ID DAYS · /toggle ID\n\n"
        "/addchannel — add a posting channel\n"
        "/channels · /delchannel CHAT_ID\n\n"
        "/stats — sales and recent payments\n"
        "/refund CHARGE_ID — refund and remove access\n"
        "/cancel — stop the current step",
        parse_mode=ParseMode.HTML,
    )


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    flow = context.user_data.get("flow")

    if user.id in ADMIN_IDS and flow:
        if flow["name"] == "addplan":
            await addplan_step(update, context, flow)
        elif flow["name"] == "addchannel":
            await addchannel_step(update, context)
        elif flow["name"] == "post":
            await post_content_step(update, context, flow)
        return

    if user.id in ADMIN_IDS:
        await update.message.reply_text("Send /admin to see admin commands.")
        return

    await update.message.reply_text("Send /start to see VIP plans, or /myvip if you already paid.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

USER_COMMANDS = [
    BotCommand("start", "See VIP plans"),
    BotCommand("myvip", "My VIP passes and join links"),
    BotCommand("paysupport", "Payment support"),
    BotCommand("terms", "Terms of purchase"),
]

ADMIN_COMMANDS = [
    BotCommand("admin", "Admin commands"),
    BotCommand("addplan", "Create a VIP plan"),
    BotCommand("plans", "List plans and pay links"),
    BotCommand("post", "Post with STAR PAY button"),
    BotCommand("addchannel", "Add a posting channel"),
    BotCommand("channels", "List posting channels"),
    BotCommand("stats", "Sales and recent payments"),
    BotCommand("refund", "Refund a payment"),
    BotCommand("cancel", "Stop the current step"),
]


async def post_init(app: Application) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    app.bot_data["db"] = pool

    await app.bot.set_my_commands(USER_COMMANDS)
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                USER_COMMANDS + ADMIN_COMMANDS,
                scope=BotCommandScopeChat(admin_id),
            )
        except TelegramError:
            log.info("Admin %s has not started the bot yet; admin menu skipped.", admin_id)

    app.job_queue.run_repeating(expiry_job, interval=EXPIRY_CHECK_SECONDS, first=30)
    log.info("Bot @%s is running. Admins: %s", app.bot.username, ADMIN_IDS or "NONE SET")


async def post_shutdown(app: Application) -> None:
    pool = app.bot_data.get("db")
    if pool:
        await pool.close()


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Payments (registered first so nothing else swallows them)
    app.add_handler(PreCheckoutQueryHandler(on_pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
    app.add_handler(ChatJoinRequestHandler(on_join_request))

    # Buyer commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myvip", cmd_myvip))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    app.add_handler(CommandHandler("terms", cmd_terms))

    # Admin commands
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("addplan", cmd_addplan))
    app.add_handler(CommandHandler("plans", cmd_plans))
    app.add_handler(CommandHandler("setstars", cmd_setstars))
    app.add_handler(CommandHandler("setdays", cmd_setdays))
    app.add_handler(CommandHandler("toggle", cmd_toggle))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("delchannel", cmd_delchannel))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("refund", cmd_refund))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Buttons
    app.add_handler(CallbackQueryHandler(on_buy_button, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(on_link_button, pattern=r"^link:-?\d+$"))
    app.add_handler(CallbackQueryHandler(on_flow_stars_button, pattern=r"^fs:\d+$"))
    app.add_handler(CallbackQueryHandler(on_postto_button, pattern=r"^postto:"))

    # Admin wizard input and fallback replies (private chats only)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT,
            on_private_message,
        )
    )

    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
