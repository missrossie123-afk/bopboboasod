import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv не установлен — берём переменные из окружения хостинга
    def load_dotenv(*_a, **_k):
        return False

from database import (
    init_db, create_deal, get_deal, update_deal, set_setting, get_setting,
    list_admins, add_admin, remove_admin, get_active_deals, expire_pending_deals
)
from verifier import verify_gift_transfer, VerificationStatus

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DEAL_HOURS = int(os.getenv("DEAL_HOURS", "24"))
# edit    — Business: бот редактирует ваше сообщение (безопаснее, без PEER_FLOOD)
# send    — бот отправляет НОВОЕ сообщение, ваше исходное остаётся
# replace — бот отправляет новое сообщение и удаляет ваше исходное
OFFER_MODE = os.getenv("OFFER_MODE", "send").strip().lower()
if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в .env")

router = Router()
URL_RE = re.compile(r"https://t\.me/nft/[A-Za-z0-9_-]+", re.I)
AMOUNT_RE = re.compile(r"(?<!\w)(\d{1,12})\s*(?:⭐️?|stars?)", re.I)

def utcnow():
    return datetime.now(timezone.utc)

# --- Защита от PEER_FLOOD ---------------------------------------------------
FLOOD_PAUSE_MIN = int(os.getenv("FLOOD_PAUSE_MIN", "30"))   # пауза после PEER_FLOOD
CHAT_COOLDOWN_SEC = int(os.getenv("CHAT_COOLDOWN_SEC", "20"))  # мин. интервал между офферами в одном чате
_send_paused_until = None
_last_send_by_chat = {}

def sending_paused():
    return _send_paused_until is not None and utcnow() < _send_paused_until

def chat_throttled(chat_id):
    last = _last_send_by_chat.get(chat_id)
    return last is not None and (utcnow() - last).total_seconds() < CHAT_COOLDOWN_SEC

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
EMOJI_BTN_TRANSFER = "5875465628285931233"  # иконка кнопки «Передать»
EMOJI_BTN_VERIFY = "5874960879434338403"    # иконка кнопки «Проверить»

def tg_emoji(custom_id, fallback):
    return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'

def offer_text(d):
    gift_emoji = tg_emoji(EMOJI_GIFT, "🎁")
    star_emoji = tg_emoji(EMOJI_STAR, "⭐")
    accept_emoji = tg_emoji(EMOJI_ACCEPT, "✅")
    cancel_emoji = tg_emoji(EMOJI_CANCEL, "❌")
    lang = (d.get("language") or "ru").lower()
    english = lang == "eng"
    gift_title = escape(d["gift_title"])
    gift_url = escape(d["gift_url"])
    amount = f'{int(d["amount"]):,}'.replace(",", " ")
    user = escape(d["creator_name"] or ("User" if english else "Пользователь"))
    if d["status"] == "PENDING":
        if english:
            return (f"{gift_emoji} <b>{user} has made you an offer</b>\n"
                    f"<b>{amount} {star_emoji} for the gift {gift_title}</b>\n"
                    f"{gift_url}\n\n"
                    f"This offer expires in <b>{remaining(d['expires_at'])}</b>.")
        return (f"{gift_emoji} <b>{user} предлагает вам</b>\n"
                f"<b>{amount} {star_emoji} за подарок {gift_title}</b>\n"
                f"{gift_url}\n\n"
                f"Предложение действительно ещё <b>{remaining(d['expires_at'])}</b>.")
    if d["status"] == "ACCEPTED":
        if english:
            return (f"{accept_emoji} <b>Offer accepted</b>\n\n"
                    f"{amount} {star_emoji} for the gift {gift_title}\n{gift_url}\n\n"
                    "Gift transfer is pending. The status will update after the transfer is independently verified.")
        return (f"{accept_emoji} <b>Предложение принято</b>\n\n"
                f"{amount} {star_emoji} за подарок {gift_title}\n{gift_url}\n\n"
                "Ожидается передача подарка. Статус обновится после независимого подтверждения передачи.")
    if d["status"] == "REJECTED":
        return f"{cancel_emoji} <b>{'Offer declined' if english else 'Сделка отменена — предложение отклонено'}</b>."
    if d["status"] == "CANCELLED":
        return f"{cancel_emoji} <b>{'Deal cancelled.' if english else 'Сделка отменена.'}</b>"
    if d["status"] == "EXPIRED":
        return f"⌛ <b>{'This offer has expired.' if english else 'Срок действия предложения истёк.'}</b>"
    if d["status"] == "COMPLETED":
        status = "Gift transfer confirmed." if english else "Передача подарка подтверждена проверкой."
        return f"{accept_emoji} <b>{status}</b>\n{gift_url}"
    return "Unknown deal status." if english else "Статус сделки неизвестен."

def keyboard_for(d, recipient=None):
    deal_id = d["id"]
    lang = (d.get("language") or "ru").lower()
    if lang not in {"ru", "eng"}:
        lang = "ru"
    if d["status"] == "PENDING":
        accept_text, reject_text = (("☑ Accept", "⊗ Reject") if lang == "eng" else ("☑ Принять", "⊗ Отклонить"))
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=accept_text, callback_data=f"accept:{deal_id}"),
            InlineKeyboardButton(text=reject_text, callback_data=f"reject:{deal_id}")
        ]])
    if d["status"] == "ACCEPTED":
        transfer_text, verify_text = (("Transfer", "Verify") if lang == "eng" else ("Передать", "Проверить"))
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=transfer_text, url="tg://send_gift?to=NikLa09",
                                 icon_custom_emoji_id=EMOJI_BTN_TRANSFER),
            InlineKeyboardButton(text=verify_text, callback_data=f"verify:{deal_id}",
                                 icon_custom_emoji_id=EMOJI_BTN_VERIFY)
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
    except TelegramBadRequest as exc:
        # Telegram rejects identical edits; this is harmless for the countdown.
        if "message is not modified" in str(exc).lower():
            return
        logging.warning("Не удалось обновить сделку %s: %s", d["id"], exc)
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
    global _send_paused_until
    if not message.from_user or message.from_user.is_bot:
        return
    # Ignore private messages only in the ordinary bot chat workflow.
    if not business_connection_id and message.chat.type == "private":
        return
    # Business-режим: бот пишет ОТ ИМЕНИ вашего личного аккаунта, поэтому
    # реагируем только на сообщения админов, иначе Telegram режет аккаунт за спам.
    if business_connection_id and not await is_admin(message.from_user.id):
        return
    parsed = parse_offer(message.text or "")
    if not parsed:
        return
    if not business_connection_id or OFFER_MODE in ("send", "replace"):
        if sending_paused():
            logging.warning("Отправка на паузе (PEER_FLOOD) до %s — оффер пропущен", _send_paused_until)
            return
        if chat_throttled(message.chat.id):
            logging.info("Chat %s: слишком частые офферы, пропуск", message.chat.id)
            return
        _last_send_by_chat[message.chat.id] = utcnow()

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
        language=language,
    )
    d = await get_deal(deal_id)
    recipient = await get_setting("recipient_username")

    try:
        if business_connection_id and OFFER_MODE == "edit":
            # Не отправляем новое сообщение (его считают спамом с вашего аккаунта),
            # а превращаем ваше исходное сообщение в оффер.
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                text=offer_text(d),
                reply_markup=keyboard_for(d, recipient),
                disable_web_page_preview=False,
                business_connection_id=business_connection_id,
            )
            sent = message  # message_id исходного сообщения сохраняется как id оффера
        elif business_connection_id:
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
    except Exception as exc:
        # PEER_FLOOD is a Telegram-side anti-spam/rate restriction.
        # Do not retry automatically; repeated sends can make the restriction worse.
        error_text = str(exc).upper()
        if "PEER_FLOOD" in error_text:
            _send_paused_until = utcnow() + timedelta(minutes=FLOOD_PAUSE_MIN)
            logging.error("PEER_FLOOD details (business=%s): %r", bool(business_connection_id), exc)
            logging.error(
                "Telegram PEER_FLOOD while sending offer; no retry. "
                "Deal %s marked CANCELLED. Pause sending and wait for Telegram restriction to clear.",
                deal_id,
            )
        else:
            logging.exception("Could not send offer for deal %s; marking it CANCELLED", deal_id)
        await update_deal(
            deal_id,
            status="CANCELLED",
            verification_note=("Telegram blocked the send with PEER_FLOOD." if "PEER_FLOOD" in error_text
                              else f"Send failed: {str(exc)[:300]}"),
        )
        return

    # Save a real Telegram message ID only after Telegram confirms the send.
    await update_deal(deal_id, message_id=sent.message_id)

    # replace-режим: исходное сообщение удаляем только ПОСЛЕ успешной отправки оффера,
    # чтобы при ошибке сообщение не пропало.
    if OFFER_MODE == "replace" or not business_connection_id:
        try:
            if business_connection_id:
                await bot.delete_business_messages(
                    business_connection_id=business_connection_id,
                    message_ids=[message.message_id],
                )
            elif OFFER_MODE == "replace":
                await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception as exc:
            logging.warning("Не удалось удалить исходное сообщение %s: %r", message.message_id, exc)


@router.message(F.text)
async def create_offer(message: Message, bot: Bot):
    await _create_offer(message, bot)


@router.business_message(F.text.func(lambda t: t.strip().lower() == "/diag"))
async def business_diag(message: Message, bot: Bot):
    """Диагностика PEER_FLOOD: что именно Telegram не пропускает. Написать /diag в бизнес-чате."""
    cid = getattr(message, "business_connection_id", None)
    if not cid or not message.from_user or not await is_admin(message.from_user.id):
        return
    gift = "https://t.me/nft/InstantRamen-181091"
    tests = [
        ("1 простой текст", dict(text="тест 1")),
        ("2 текст со ссылкой", dict(text=f"тест 2 {gift}")),
        ("3 кастомные эмодзи", dict(text=f"{tg_emoji(EMOJI_GIFT, '🎁')} тест 3", parse_mode=ParseMode.HTML)),
        ("4 инлайн-кнопки", dict(text="тест 4", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Кнопка", callback_data="diag")]]))),
    ]
    # 5: настоящий оффер (тот же текст, разметка и кнопки), но с тестовыми данными
    fake = {"id": 0, "status": "PENDING", "gift_url": gift, "gift_title": "InstantRamen #181091",
            "amount": 444, "creator_name": message.from_user.full_name or "Тест",
            "expires_at": iso(utcnow() + timedelta(hours=DEAL_HOURS))}
    tests.append(("5 настоящий оффер", dict(text=offer_text(fake), reply_markup=keyboard_for(fake),
                                            disable_web_page_preview=False)))
    results, sent_ids = [], []
    for name, kw in tests:
        try:
            m = await bot.send_message(chat_id=message.chat.id, business_connection_id=cid, **kw)
            sent_ids.append(m.message_id)
            results.append(f"✅ {name}")
        except Exception as exc:
            results.append(f"❌ {name}: {str(exc)[:80]}")
            break
        await asyncio.sleep(3)
    logging.info("DIAG: %s", " | ".join(results))
    try:
        if sent_ids:
            await bot.delete_business_messages(business_connection_id=cid, message_ids=sent_ids)
    except Exception as exc:
        logging.warning("DIAG: не удалось удалить тестовые сообщения: %r", exc)
    try:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id,
                                    text="DIAG\n" + "\n".join(results), business_connection_id=cid, parse_mode=None)
    except Exception as exc:
        logging.warning("DIAG: не удалось показать результат: %r", exc)


@router.business_message(F.text.func(lambda t: t.strip().lower() == "/diag2"))
async def business_diag2(message: Message, bot: Bot):
    """Бисекция: какой элемент оффера вызывает PEER_FLOOD. Написать /diag2 в бизнес-чате."""
    cid = getattr(message, "business_connection_id", None)
    if not cid or not message.from_user or not await is_admin(message.from_user.id):
        return
    gift = "https://t.me/nft/InstantRamen-181091"
    user = escape(message.from_user.full_name or "Тест")
    fake = {"id": 0, "status": "PENDING", "gift_url": gift, "gift_title": "InstantRamen #181091",
            "amount": 444, "creator_name": message.from_user.full_name or "Тест",
            "expires_at": iso(utcnow() + timedelta(hours=DEAL_HOURS))}
    kb = keyboard_for(fake)
    full = offer_text(fake)
    plain_emoji = (f"🎁 <b>{user} предлагает вам</b>\n<b>444 ⭐ за подарок "
                   f'<a href="{gift}">InstantRamen #181091</a></b>\n\nПредложение действует ещё <b>24 ч</b>.')
    no_link = (f"{tg_emoji(EMOJI_GIFT, '🎁')} <b>{user} предлагает вам</b>\n"
               f"<b>444 {tg_emoji(EMOJI_STAR, '⭐')} за подарок InstantRamen #181091</b>\n\n"
               f"Предложение действует ещё <b>24 ч</b>.")
    tests = [
        ("A оффер без кнопок", dict(text=full)),
        ("B кнопки «Принять/Отклонить» + короткий текст", dict(text="тест B", reply_markup=kb)),
        ("C оффер с обычными эмодзи", dict(text=plain_emoji, reply_markup=kb)),
        ("D оффер без ссылки на подарок", dict(text=no_link, reply_markup=kb)),
        ("E оффер, превью ссылки выключено", dict(text=full, reply_markup=kb, disable_web_page_preview=True)),
        ("F текст оффера без разметки", dict(text=re.sub(r"<[^>]+>", "", full), parse_mode=None, reply_markup=kb)),
    ]
    results, sent_ids = [], []
    for name, kw in tests:
        try:
            m = await bot.send_message(chat_id=message.chat.id, business_connection_id=cid, **kw)
            sent_ids.append(m.message_id)
            results.append(f"✅ {name}")
        except Exception as exc:
            results.append(f"❌ {name}: {str(exc)[:60]}")
        await asyncio.sleep(3)
    logging.info("DIAG2: %s", " | ".join(results))
    try:
        if sent_ids:
            await bot.delete_business_messages(business_connection_id=cid, message_ids=sent_ids)
    except Exception as exc:
        logging.warning("DIAG2: не удалось удалить тестовые сообщения: %r", exc)
    try:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id,
                                    text="DIAG2\n" + "\n".join(results), business_connection_id=cid, parse_mode=None)
    except Exception as exc:
        logging.warning("DIAG2: не удалось показать результат: %r", exc)


@router.business_message(F.text.func(lambda t: t.strip().lower() == "/diag3"))
async def business_diag3(message: Message, bot: Bot):
    """Бисекция №2: как показать адрес подарка, чтобы не было PEER_FLOOD. Написать /diag3."""
    cid = getattr(message, "business_connection_id", None)
    if not cid or not message.from_user or not await is_admin(message.from_user.id):
        return
    gift = "https://t.me/nft/InstantRamen-181091"
    user = escape(message.from_user.full_name or "Тест")
    fake = {"id": 0, "status": "PENDING", "gift_url": gift, "gift_title": "InstantRamen #181091",
            "amount": 444, "creator_name": message.from_user.full_name or "Тест",
            "expires_at": iso(utcnow() + timedelta(hours=DEAL_HOURS))}
    kb = keyboard_for(fake)
    url_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять", callback_data="diag"), InlineKeyboardButton(text="Отклонить", callback_data="diag")],
        [InlineKeyboardButton(text="Открыть подарок", url=gift)]])
    tests = [
        ("1 URL + 444 ⭐ простым текстом", dict(text=f"444 ⭐ за подарок InstantRamen\n{gift}", parse_mode=None)),
        ("2 адрес только в URL-кнопке", dict(text=f"{user} предлагает вам\n444 ⭐ за подарок InstantRamen #181091",
                                           parse_mode=None, reply_markup=url_kb)),
        ("3 адрес в <code>", dict(text=f"{user} предлагает вам\n444 ⭐ за подарок InstantRamen #181091\n<code>{gift}</code>",
                                reply_markup=kb)),
        ("4 адрес без https://", dict(text=f"{user} предлагает вам\n444 ⭐ за подарок InstantRamen #181091\n"
                                     f"{gift.replace('https://', '')}", parse_mode=None, reply_markup=kb)),
        ("5 URL без слов «предлагает вам»", dict(text=f"444 ⭐ за подарок InstantRamen #181091\n{gift}", parse_mode=None, reply_markup=kb)),
        ("6 URL + «предлагает вам», без суммы", dict(text=f"{user} предлагает вам подарок\n{gift}", parse_mode=None, reply_markup=kb)),
    ]
    results, sent_ids = [], []
    for name, kw in tests:
        try:
            m = await bot.send_message(chat_id=message.chat.id, business_connection_id=cid, **kw)
            sent_ids.append(m.message_id)
            results.append(f"✅ {name}")
        except Exception as exc:
            results.append(f"❌ {name}: {str(exc)[:60]}")
        await asyncio.sleep(3)
    logging.info("DIAG3: %s", " | ".join(results))
    try:
        if sent_ids:
            await bot.delete_business_messages(business_connection_id=cid, message_ids=sent_ids)
    except Exception as exc:
        logging.warning("DIAG3: не удалось удалить тестовые сообщения: %r", exc)
    try:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id,
                                    text="DIAG3\n" + "\n".join(results), business_connection_id=cid, parse_mode=None)
    except Exception as exc:
        logging.warning("DIAG3: не удалось показать результат: %r", exc)


@router.business_message(F.text.func(lambda t: t.strip().lower() == "/diag4"))
async def business_diag4(message: Message, bot: Bot):
    """Воспроизводим формат другого бота и добавляем наши отличия по одному. Написать /diag4."""
    cid = getattr(message, "business_connection_id", None)
    if not cid or not message.from_user or not await is_admin(message.from_user.id):
        return
    gift = "https://t.me/nft/DiamondRing-28438"
    link = f'<a href="{gift}">Diamond Ring #28438</a>'
    real = escape(message.from_user.full_name or "Тест")
    fake = {"id": 0, "status": "PENDING", "gift_url": gift, "gift_title": "Diamond Ring #28438",
            "amount": 9500, "creator_name": message.from_user.full_name or "Тест",
            "expires_at": iso(utcnow() + timedelta(hours=DEAL_HOURS))}
    kb = keyboard_for(fake)
    tail = "\n\nПредложение действует ещё 5 ч. 59 мин."
    tests = [
        ("1 формат другого бота (без жирного, ⭐, «Пользователь»)",
         dict(text=f"Пользователь предлагает вам\n9500 ⭐ за подарок {link}.{tail}", reply_markup=kb)),
        ("2 то же + жирный шрифт",
         dict(text=f"<b>Пользователь предлагает вам</b>\n<b>9500 ⭐ за подарок {link}</b>.{tail}", reply_markup=kb)),
        ("3 то же с моим реальным именем",
         dict(text=f"{real} предлагает вам\n9500 ⭐ за подарок {link}.{tail}", reply_markup=kb)),
        ("4 то же с кастомными эмодзи",
         dict(text=f"{tg_emoji(EMOJI_GIFT, '🎁')} Пользователь предлагает вам\n9500 {tg_emoji(EMOJI_STAR, '⭐')} за подарок {link}.{tail}", reply_markup=kb)),
        ("5 мой оффер, имя «Пользователь»", dict(text=offer_text({**fake, "creator_name": "Пользователь"}), reply_markup=kb)),
        ("6 мой оффер как есть", dict(text=offer_text(fake), reply_markup=kb)),
    ]
    results, sent_ids = [], []
    for name, kw in tests:
        try:
            m = await bot.send_message(chat_id=message.chat.id, business_connection_id=cid, **kw)
            sent_ids.append(m.message_id)
            results.append(f"✅ {name}")
        except Exception as exc:
            results.append(f"❌ {name}: {str(exc)[:60]}")
        await asyncio.sleep(3)
    logging.info("DIAG4: %s", " | ".join(results))
    try:
        if sent_ids:
            await bot.delete_business_messages(business_connection_id=cid, message_ids=sent_ids)
    except Exception as exc:
        logging.warning("DIAG4: не удалось удалить тестовые сообщения: %r", exc)
    try:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id,
                                    text="DIAG4\n" + "\n".join(results), business_connection_id=cid, parse_mode=None)
    except Exception as exc:
        logging.warning("DIAG4: не удалось показать результат: %r", exc)


@router.business_message(F.text.func(lambda t: t.strip().lower().startswith("/diag5")))
async def business_diag5(message: Message, bot: Bot):
    """/diag5 URL AMOUNT — send a NEW offer message, then delete the command."""
    cid = getattr(message, "business_connection_id", None)
    if not cid or not message.from_user or not await is_admin(message.from_user.id):
        return

    args = re.sub(r"^/diag5(?:@\w+)?\s*", "", message.text or "", count=1, flags=re.I).strip()
    tokens = args.split()
    language = "ru"
    if tokens and tokens[-1].lower() in {"ru", "eng"}:
        language = tokens.pop().lower()
    args_without_language = " ".join(tokens)
    parse_input = args_without_language if re.search(r"\bstars?\b", args_without_language, re.I) else f"{args_without_language} stars"
    parsed = parse_offer(parse_input)
    if not parsed:
        # Leave the command in place if its arguments are invalid, and explain the format.
        try:
            await bot.send_message(
                chat_id=message.chat.id,
                business_connection_id=cid,
                text=("Format: /diag5 https://t.me/nft/InstantRamen-181091 444 eng" if language == "eng"
                      else "Формат: /diag5 https://t.me/nft/InstantRamen-181091 444 ru"),
                parse_mode=None,
            )
        except Exception as exc:
            logging.warning("DIAG5: не удалось отправить подсказку формата: %r", exc)
        return

    expiry = utcnow() + timedelta(hours=DEAL_HOURS)
    creator = message.from_user.full_name or message.from_user.username or "Пользователь"
    deal_id = await create_deal(
        chat_id=message.chat.id,
        message_id=message.message_id,  # replaced with the new offer's ID below
        business_connection_id=cid,
        creator_id=message.from_user.id,
        creator_name=creator,
        gift_url=parsed["url"],
        gift_title=parsed["title"],
        amount=parsed["amount"],
        expires_at=iso(expiry),
        language=language,
    )
    deal = await get_deal(deal_id)
    recipient = await get_setting("recipient_username")

    amount_text = f'{int(parsed["amount"]):,}'.replace(",", " ")
    if language == "eng":
        hidden_text = (
            f"{tg_emoji(EMOJI_GIFT, '🎁')} <b>{escape(creator)} has made you an offer</b>\n"
            f"<b>{amount_text} {tg_emoji(EMOJI_STAR, '⭐')} for the gift "
            f'<a href="{escape(parsed["url"], quote=True)}">{escape(parsed["title"])}</a></b>\n\n'
            f"This offer expires in <b>{remaining(deal['expires_at'])}</b>."
        )
    else:
        hidden_text = (
            f"{tg_emoji(EMOJI_GIFT, '🎁')} <b>{escape(creator)} предлагает вам</b>\n"
            f"<b>{amount_text} {tg_emoji(EMOJI_STAR, '⭐')} за подарок "
            f'<a href="{escape(parsed["url"], quote=True)}">{escape(parsed["title"])}</a></b>\n\n'
            f"Предложение действительно ещё <b>{remaining(deal['expires_at'])}</b>."
        )
    try:
        # First send the replacement offer. Only after success do we remove the command.
        sent = await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=cid,
            text=hidden_text,
            reply_markup=keyboard_for(deal, recipient),
            disable_web_page_preview=True,
        )
        await update_deal(deal_id, message_id=sent.message_id)
        try:
            await bot.delete_message(
                chat_id=message.chat.id,
                message_id=message.message_id,
                business_connection_id=cid,
            )
        except Exception as exc:
            # Offer is already posted; don't cancel it if Telegram refuses deletion.
            logging.warning("DIAG5: offer sent as message_id=%s, but command deletion failed: %r", sent.message_id, exc)
        logging.info("DIAG5: sent new offer deal_id=%s message_id=%s url=%s amount=%s",
                     deal_id, sent.message_id, parsed["url"], parsed["amount"])
    except Exception as exc:
        logging.exception("DIAG5: could not send new offer: %r", exc)
        await update_deal(
            deal_id,
            status="CANCELLED",
            verification_note=f"Could not send /diag5 offer: {str(exc)[:250]}",
        )


@router.business_message(F.text)
async def create_business_offer(message: Message, bot: Bot):
    connection_id = getattr(message, "business_connection_id", None)
    if not connection_id:
        logging.warning("Получено business_message без business_connection_id")
        return
    await _create_offer(message, bot, business_connection_id=connection_id)

@router.callback_query(F.data.startswith(("accept:", "reject:", "verify:")))
async def callbacks(call: CallbackQuery, bot: Bot):
    # Verify shows a localized alert; other actions are acknowledged immediately.
    action_hint = (call.data or "").split(":", 1)[0]
    if action_hint != "verify":
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
        lang = (d.get("language") or "ru").lower()
        if lang == "eng":
            alert = ("Gift transfer has not been detected yet. Please send the gift using the Transfer button, "
                     "then try verification again.")
        else:
            alert = ("Передача подарка пока не подтверждена. Пожалуйста, отправьте подарок через кнопку «Передать», "
                     "затем повторите проверку.")
        try:
            await call.answer(alert, show_alert=True)
        except Exception as exc:
            logging.warning("Could not show verification alert for deal_id=%s: %r", deal_id, exc)
        return

async def ticker(bot):
    while True:
        # Update expiry status in the database only. Do not edit offer messages every minute:
        # Telegram marks those messages as edited, and it is unnecessary for the offer itself.
        now = utcnow()
        await expire_pending_deals(now.isoformat())
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