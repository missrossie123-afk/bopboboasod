import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from dotenv import load_dotenv

from database import (
    init_db, create_deal, get_deal, update_deal, set_setting, get_setting,
    list_admins, add_admin, remove_admin, get_active_deals, expire_pending_deals
)
from verifier import verify_gift_transfer, VerificationStatus

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DEAL_HOURS = int(os.getenv("DEAL_HOURS", "24"))
if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в .env")

router = Router()
URL_RE = re.compile(r"https://t\.me/nft/[A-Za-z0-9_-]+", re.I)
AMOUNT_RE = re.compile(r"(?<!\w)(\d{1,12})\s*(?:⭐️?|stars?)", re.I)

def utcnow():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()

def parse_offer(text: str):
    um = URL_RE.search(text or "")
    am = AMOUNT_RE.search(text or "")
    if not um or not am:
        return None
    url = um.group(0)
    amount = int(am.group(1))
    if amount <= 0:
        return None
    slug = url.rstrip("/").split("/")[-1]
    number = re.search(r"-(\d+)$", slug)
    base_name = re.sub(r"-\d+$", "", slug)
    title = re.sub(r"[_-]+", " ", base_name).strip() or "NFT-подарок"
    if number:
        title += f" #{number.group(1)}"
    return {"url": url, "amount": amount, "title": title}

def remaining(expires_at):
    try:
        end = datetime.fromisoformat(expires_at)
        sec = max(0, int((end - utcnow()).total_seconds()))
    except Exception:
        sec = 0
    hours, sec = divmod(sec, 3600)
    mins = sec // 60
    return f"{hours} ч. {mins:02d} мин."

# Telegram custom emoji IDs supplied for this bot.
EMOJI_GIFT = "6032937473162614352"
EMOJI_STAR = "5406812184359507637"
EMOJI_ACCEPT = "5776375003280838798"
EMOJI_CANCEL = "5778527486270770928"

def tg_emoji(custom_id, fallback):
    return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'

def offer_text(d):
    gift_emoji = tg_emoji(EMOJI_GIFT, "🎁")
    star_emoji = tg_emoji(EMOJI_STAR, "⭐")
    accept_emoji = tg_emoji(EMOJI_ACCEPT, "✅")
    cancel_emoji = tg_emoji(EMOJI_CANCEL, "❌")
    gift_link = f'<a href="{escape(d["gift_url"], quote=True)}">{escape(d["gift_title"])}</a>'
    amount = f'{int(d["amount"]):,}'.replace(",", " ")
    user = escape(d["creator_name"] or "Пользователь")
    if d["status"] == "PENDING":
        return (
            f"{gift_emoji} <b>{user} предлагает вам</b>\n"
            f"<b>{amount} {star_emoji} за подарок {gift_link}</b>\n\n"
            f"Предложение действует ещё <b>{remaining(d['expires_at'])}</b>."
        )
    if d["status"] == "ACCEPTED":
        return (
            f"{accept_emoji} <b>Предложение принято</b>\n\n"
            f"{amount} {star_emoji} за подарок {gift_link}\n\n"
            "Ожидается фактическая передача подарка. "
            "После независимой проверки передачи статус сделки обновится."
        )
    if d["status"] == "REJECTED":
        return f"{cancel_emoji} <b>Сделка отменена</b> — предложение отклонено."
    if d["status"] == "CANCELLED":
        return f"{cancel_emoji} <b>Сделка отменена.</b>"
    if d["status"] == "EXPIRED":
        return "⌛ <b>Срок действия предложения истёк.</b>"
    if d["status"] == "COMPLETED":
        return f"{accept_emoji} <b>Передача подарка {gift_link} подтверждена проверкой.</b>"
    return "Статус сделки неизвестен."

def keyboard_for(d, recipient=None):
    deal_id = d["id"]
    if d["status"] == "PENDING":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="☑ Принять", callback_data=f"accept:{deal_id}"),
            InlineKeyboardButton(text="⊗ Отклонить", callback_data=f"reject:{deal_id}")
        ]])
    if d["status"] == "ACCEPTED":
        username = (recipient or "").strip().lstrip("@")
        transfer_url = f"https://t.me/{username}" if username else "https://t.me/"
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Передать", url=transfer_url),
            InlineKeyboardButton(text="🔎 Проверить", callback_data=f"verify:{deal_id}")
        ]])
    return None

async def is_admin(uid):
    return uid in ADMIN_IDS or await list_admins(uid)

async def render(bot, d):
    # The ticker can see a new deal before send_message returns its message_id.
    if not d or not d.get("message_id") or int(d["message_id"]) <= 0:
        return
    recipient = await get_setting("recipient_username")
    try:
        kwargs = {
            "chat_id": d["chat_id"],
            "message_id": d["message_id"],
            "text": offer_text(d),
            "reply_markup": keyboard_for(d, recipient),
            "disable_web_page_preview": False,
        }
        # Business messages are edited using their original connection.
        if d.get("business_connection_id"):
            kwargs["business_connection_id"] = d["business_connection_id"]
        await bot.edit_message_text(**kwargs)
    except Exception as exc:
        logging.warning("Не удалось обновить сделку %s: %s", d["id"], exc)

@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Я бот текстовых предложений NFT-подарков для групп.\n\n"
        "Пример:\nhttps://t.me/nft/InstantRamen-181091 444 stars\n\n"
        "Создаётся обычное текстовое сообщение с кликабельным названием подарка "
        "и настоящими inline-кнопками. Используйте /admin для настроек."
    )

@router.message(Command("admin"))
async def admin(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.reply("⛔ Команда доступна только администраторам бота.")
    recipient = await get_setting("recipient_username") or "не задан"
    await message.answer(
        f"⚙️ <b>Настройки</b>\nПолучатель: @{escape(recipient.lstrip('@')) if recipient != 'не задан' else recipient}\n\n"
        "/setrecipient username — изменить получателя\n"
        "/addadmin user_id — добавить администратора (только владелец)\n"
        "/deladmin user_id — удалить администратора (только владелец)"
    )

@router.message(Command("setrecipient"))
async def setrecipient(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.reply("Формат: /setrecipient username")
    username = parts[1].strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return await message.reply("Укажите корректный Telegram username.")
    await set_setting("recipient_username", username)
    await message.answer(f"✅ Получатель установлен: @{escape(username)}")

@router.message(Command("addadmin"))
async def addadmin_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.reply("⛔ Только владелец может добавлять администраторов.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.reply("Формат: /addadmin user_id")
    await add_admin(int(parts[1]))
    await message.answer("✅ Администратор добавлен.")

@router.message(Command("deladmin"))
async def deladmin_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.reply("⛔ Только владелец может удалять администраторов.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.reply("Формат: /deladmin user_id")
    await remove_admin(int(parts[1]))
    await message.answer("✅ Администратор удалён.")

async def _create_offer(message: Message, bot: Bot, *, business_connection_id: str | None = None):
    if not message.from_user or message.from_user.is_bot:
        return
    # Ignore private messages only in the ordinary bot chat workflow.
    if not business_connection_id and message.chat.type == "private":
        return
    parsed = parse_offer(message.text or "")
    if not parsed:
        return

    expiry = utcnow() + timedelta(hours=DEAL_HOURS)
    creator = message.from_user.full_name or message.from_user.username or "Пользователь"
    deal_id = await create_deal(
        chat_id=message.chat.id,
        message_id=0,
        business_connection_id=business_connection_id,
        creator_id=message.from_user.id,
        creator_name=creator,
        gift_url=parsed["url"],
        gift_title=parsed["title"],
        amount=parsed["amount"],
        expires_at=iso(expiry),
    )
    d = await get_deal(deal_id)
    recipient = await get_setting("recipient_username")

    if business_connection_id:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            text=offer_text(d),
            reply_markup=keyboard_for(d, recipient),
            disable_web_page_preview=False,
            business_connection_id=business_connection_id,
        )
    else:
        sent = await message.answer(
            offer_text(d),
            reply_markup=keyboard_for(d, recipient),
            disable_web_page_preview=False,
        )
    await update_deal(deal_id, message_id=sent.message_id)


@router.message(F.text)
async def create_offer(message: Message, bot: Bot):
    await _create_offer(message, bot)


@router.business_message(F.text)
async def create_business_offer(message: Message, bot: Bot):
    connection_id = getattr(message, "business_connection_id", None)
    if not connection_id:
        logging.warning("Получено business_message без business_connection_id")
        return
    await _create_offer(message, bot, business_connection_id=connection_id)

@router.callback_query(F.data.startswith(("accept:", "reject:", "verify:")))
async def callbacks(call: CallbackQuery, bot: Bot):
    # Acknowledge immediately; database and edit calls may take longer than Telegram's callback window.
    try:
        await call.answer()
    except Exception as exc:
        logging.warning("Callback acknowledgement failed (continuing): %r", exc)

    try:
        action, raw_id = (call.data or "").split(":", 1)
        deal_id = int(raw_id)
    except Exception:
        logging.warning("Invalid callback data: %r", call.data)
        return

    d = await get_deal(deal_id)
    if not d:
        logging.error(
            "Deal not found for callback: deal_id=%s callback_message_id=%s chat_id=%s. "
            "Check persistent DB/volume and DB_PATH.",
            deal_id,
            getattr(getattr(call, "message", None), "message_id", None),
            getattr(getattr(call, "message", None), "chat", None).id
            if getattr(call, "message", None) and getattr(call.message, "chat", None) else None,
        )
        return

    if d["status"] == "PENDING" and utcnow() >= datetime.fromisoformat(d["expires_at"]):
        await update_deal(deal_id, status="EXPIRED")
        await render(bot, await get_deal(deal_id))
        return

    if action == "accept":
        if d["status"] != "PENDING":
            return
        # Creator is allowed to accept their own offer as requested.
        await update_deal(deal_id, status="ACCEPTED", accepted_by=call.from_user.id)
        await render(bot, await get_deal(deal_id))
        return

    if action == "reject":
        if d["status"] != "PENDING":
            return
        new_status = "CANCELLED" if call.from_user.id == d["creator_id"] else "REJECTED"
        await update_deal(deal_id, status=new_status, rejected_by=call.from_user.id)
        await render(bot, await get_deal(deal_id))
        return

    if action == "verify":
        if d["status"] != "ACCEPTED":
            return
        recipient = await get_setting("recipient_username")
        result = await verify_gift_transfer(d["gift_url"], recipient, d)
        if result.status == VerificationStatus.CONFIRMED:
            await update_deal(deal_id, status="COMPLETED", verification_note=result.message)
            await render(bot, await get_deal(deal_id))
        elif result.status == VerificationStatus.NOT_FOUND:
            logging.info("Gift transfer not found yet for deal_id=%s", deal_id)
        else:
            logging.warning("Could not verify transfer for deal_id=%s: %s", deal_id, result.message)

async def ticker(bot):
    while True:
        now = utcnow()
        await expire_pending_deals(now.isoformat())
        deals = await get_active_deals()
        for d in deals:
            if d.get("message_id") and int(d["message_id"]) > 0:
                await render(bot, d)  # refreshes countdown every minute
        await asyncio.sleep(60)

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Инструкция"),
        BotCommand(command="admin", description="Настройки")
    ])
    asyncio.create_task(ticker(bot))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
