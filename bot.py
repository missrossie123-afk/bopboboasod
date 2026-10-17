"""
OLX.ua -> Telegram бот-мониторинг новых объявлений.

Установка:   pip install aiogram aiohttp aiosqlite
Запуск:      BOT_TOKEN=123:ABC python olx_bot.py
(Windows PowerShell:  $env:BOT_TOKEN="123:ABC"; python olx_bot.py)
"""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "olx_bot.db")
POLL_INTERVAL = max(20, int(os.getenv("POLL_INTERVAL", "45")))  # не чаще раза в 20 секунд
MAX_PHOTOS = 10
TZ = ZoneInfo("Europe/Kyiv")
API = "https://www.olx.ua/api/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "ru,uk;q=0.9",
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("olx")
router = Router()
http: aiohttp.ClientSession | None = None


# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS searches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            keywords TEXT DEFAULT '',
            minus TEXT DEFAULT '',
            price_max INTEGER,
            category_id INTEGER,
            category_name TEXT DEFAULT '',
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS seen(
            search_id INTEGER, offer_id INTEGER,
            PRIMARY KEY(search_id, offer_id)
        );
        """)
        await db.commit()


async def q_all(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            return await cur.fetchall()


async def q_exec(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid


# ───────────────────────── OLX API ─────────────────────────
async def api_get(path, params=None):
    for attempt in range(3):
        try:
            async with http.get(f"{API}{path}", params=params, headers=HEADERS,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    return await r.json()
                log.warning("OLX %s -> %s", path, r.status)
        except Exception as e:
            log.warning("OLX error %s", e)
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def fetch_offers(keywords, category_id, price_max, limit=40):
    # Получаем свежие объявления небольшими порциями, чтобы не перегружать OLX.
    params = {"offset": 0, "limit": min(max(int(limit), 1), 40),
              "sort_by": "created_at:desc", "currency": "UAH"}
    if keywords:
        params["query"] = keywords
    if category_id:
        params["category_id"] = category_id
    if price_max:
        params["filter_float_price:to"] = price_max
    data = await api_get("/offers/", params)
    return (data or {}).get("data", [])


_cats = None


async def get_categories():
    global _cats
    if _cats is None:
        data = await api_get("/categories/")
        if data and data.get("data"):
            _cats = data["data"]
    return _cats or []


# ───────────────────────── ФОРМАТИРОВАНИЕ ─────────────────────────
def get_price(o):
    for p in o.get("params", []):
        if p.get("key") == "price":
            v = p.get("value", {})
            label = v.get("label") or f"{v.get('value')} {v.get('currency', '')}"
            return label, v.get("value")
    return "не указана", None


def passes_filters(o, minus, price_max):
    text = f"{o.get('title', '')} {o.get('description', '')}".lower()
    for w in minus:
        if w and w in text:
            return False
    _, val = get_price(o)
    if price_max and val is not None and val > price_max:
        return False
    # Если OLX не вернул цену, не отбрасываем объявление автоматически.
    return True


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def ago(dt):
    if not dt:
        return "—"
    sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d} дн. {h} ч."
    if h:
        return f"{h} ч. {m} мин."
    return f"{m} мин. {s} сек."


def format_offer(o, search_title):
    price, _ = get_price(o)
    loc = o.get("location", {}) or {}
    parts = [(loc.get(k) or {}).get("name") for k in ("district", "city", "region")]
    location = ", ".join(p for p in parts if p) or "—"

    deliv = (o.get("delivery") or {}).get("rock") or {}
    delivery = "✅ Подключена" if deliv.get("active") else "➖ Не подключена"

    user = o.get("user") or {}
    since = parse_dt(user.get("created", ""))
    kind = "Бизнес" if o.get("business") else "Продавец"
    seller = f"{kind}" + (f" (на OLX с {since.year} г.)" if since else "")

    created = parse_dt(o.get("created_time", ""))
    sent = created.astimezone(TZ).strftime("%H:%M:%S") if created else "—"

    desc = re.sub(r"\s+", " ", o.get("description", "") or "").strip()
    if len(desc) > 500:
        desc = desc[:500].rstrip() + "…"

    e = html.escape
    text = (
        f"📥 Новое объявление (🟢 OLX.ua):\n"
        f"🔎 Поиск: {e(search_title)}\n\n"
        f"📌 <a href=\"{e(o.get('url', ''))}\">{e(o.get('title', ''))}</a>\n"
        f"💰 Цена: {e(price)}\n"
        f"📦 OLX Доставка: {delivery}\n"
        f"📍 Локация: {e(location)}\n"
        f"👤 Продавец: {e(seller)}\n"
        f"🖼 Фото в объявлении: {len(o.get('photos', []))} шт.\n"
        f"🕒 Отправлено продавцом: в {sent}\n"
        f"⏱ Прошло с момента подачи: {ago(created)}\n\n"
        f"📝 {e(desc)}"
    )
    return text[:4096]


def photo_urls(o):
    urls = []
    for p in (o.get("photos") or [])[:MAX_PHOTOS]:
        link = p.get("link", "")
        if link:
            urls.append(link.replace("{width}", "800").replace("{height}", "600"))
    return urls


def search_title(s):
    kw = s["keywords"] or "Все объявления"
    cat = f" в «{s['category_name']}»" if s["category_name"] else ""
    price = f" (до {s['price_max']} грн)" if s["price_max"] else ""
    minus = f", минус: {s['minus']}" if s["minus"] else ""
    return f"{kw}{cat}{price}{minus}"


# ───────────────────────── ОТПРАВКА ─────────────────────────
async def safe_call(coro_factory):
    for _ in range(4):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return None


async def send_offer(bot: Bot, chat_id: int, o, title):
    """Сначала фото альбомом, затем сообщение с описанием."""
    urls = photo_urls(o)
    if urls:
        media = [InputMediaPhoto(media=u) for u in urls]
        try:
            await safe_call(lambda: bot.send_media_group(chat_id, media))
        except (TelegramBadRequest, Exception) as e:
            log.warning("Фото не отправились (%s), шлём текст объявления", e)
    try:
        await safe_call(lambda: bot.send_message(
            chat_id, format_offer(o, title), parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔗 Открыть на OLX", url=o.get("url", "https://olx.ua"))
            ]])))
    except Exception as e:
        log.error("send_message error: %s", e)
    await asyncio.sleep(0.4)  # анти-флуд Telegram


# ───────────────────────── ФОНОВЫЙ ОПРОС ─────────────────────────
async def poller(bot: Bot):
    while True:
        try:
            searches = await q_all("SELECT * FROM searches WHERE active=1")
            cache = {}
            for s in searches:
                key = (s["keywords"], s["category_id"], s["price_max"])
                if key not in cache:
                    cache[key] = await fetch_offers(*key)
                    await asyncio.sleep(1)
                offers = cache[key]
                if not offers:
                    continue
                seen = {r["offer_id"] for r in await q_all(
                    "SELECT offer_id FROM seen WHERE search_id=?", (s["id"],))}
                new = [o for o in offers if o["id"] not in seen]
                new.sort(key=lambda o: o.get("created_time", ""))  # старые -> новые
                minus = [w.strip().lower() for w in s["minus"].split(",") if w.strip()]
                for o in new:
                    await q_exec("INSERT OR IGNORE INTO seen VALUES(?,?)", (s["id"], o["id"]))
                    if passes_filters(o, minus, s["price_max"]):
                        await send_offer(bot, s["user_id"], o, search_title(s))
        except Exception as e:
            log.exception("poller error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


# ───────────────────────── ИНТЕРФЕЙС ─────────────────────────
class New(StatesGroup):
    keywords = State()
    minus = State()
    price = State()
    category = State()


def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Новый поиск", callback_data="new")
    b.button(text="📋 Мои поиски", callback_data="list")
    b.adjust(1)
    return b.as_markup()


def skip_kb(cb="skip"):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Пропустить", callback_data=cb)]])


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 Я слежу за новыми объявлениями на OLX.ua и сразу присылаю их сюда.\n"
                   "Фильтры: ключевые слова, категория, минус-слова, максимальная цена.",
                   reply_markup=main_kb())


@router.callback_query(F.data == "new")
async def new_search(c: CallbackQuery, state: FSMContext):
    await state.set_state(New.keywords)
    await c.message.answer("🔎 Введите <b>ключевые слова</b> (например: <i>iphone 11</i>)\n"
                           "Или пропустите, чтобы искать всё в категории.",
                           parse_mode="HTML", reply_markup=skip_kb())
    await c.answer()


@router.message(New.keywords)
async def got_kw(m: Message, state: FSMContext):
    await state.update_data(keywords=m.text.strip())
    await ask_minus(m, state)


@router.callback_query(New.keywords, F.data == "skip")
async def skip_kw(c: CallbackQuery, state: FSMContext):
    await state.update_data(keywords="")
    await ask_minus(c.message, state)
    await c.answer()


async def ask_minus(m: Message, state: FSMContext):
    await state.set_state(New.minus)
    await m.answer("🚫 Введите <b>минус-слова</b> через запятую (объявления с ними будут "
                   "отброшены), например: <i>разбит, запчасти, ремонт</i>",
                   parse_mode="HTML", reply_markup=skip_kb())


@router.message(New.minus)
async def got_minus(m: Message, state: FSMContext):
    await state.update_data(minus=m.text.strip())
    await ask_price(m, state)


@router.callback_query(New.minus, F.data == "skip")
async def skip_minus(c: CallbackQuery, state: FSMContext):
    await state.update_data(minus="")
    await ask_price(c.message, state)
    await c.answer()


async def ask_price(m: Message, state: FSMContext):
    await state.set_state(New.price)
    await m.answer("💰 Введите <b>максимальную цену</b> в грн (числом):",
                   parse_mode="HTML", reply_markup=skip_kb())


@router.message(New.price)
async def got_price(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text or "")
    if not digits:
        return await m.answer("Нужно число, например 6000")
    await state.update_data(price_max=int(digits))
    await show_categories(m, state, 0)


@router.callback_query(New.price, F.data == "skip")
async def skip_price(c: CallbackQuery, state: FSMContext):
    await state.update_data(price_max=None)
    await show_categories(c.message, state, 0)
    await c.answer()


async def show_categories(m: Message, state: FSMContext, parent: int, edit=False):
    await state.set_state(New.category)
    cats = await get_categories()
    if not cats:
        await state.update_data(category_id=None, category_name="")
        await m.answer("⚠️ Не удалось загрузить категории, ищу по всему OLX.")
        return await finish_search(m, state)
    children = [c for c in cats if (c.get("parent_id") or 0) == parent]
    b = InlineKeyboardBuilder()
    if parent:
        cur = next((c for c in cats if c["id"] == parent), None)
        b.button(text=f"✅ Выбрать «{cur['name']}»", callback_data=f"catsel:{parent}")
    for c in children:
        has_kids = any((x.get("parent_id") or 0) == c["id"] for x in cats)
        b.button(text=("📂 " if has_kids else "") + c["name"],
                 callback_data=f"cat:{c['id']}" if has_kids else f"catsel:{c['id']}")
    if parent:
        up = next((c for c in cats if c["id"] == parent), {}).get("parent_id") or 0
        b.button(text="⬅️ Назад", callback_data=f"cat:{up}")
    b.button(text="⏭ Все категории", callback_data="skip")
    b.adjust(1)
    text = "📂 Выберите <b>категорию</b>:"
    if edit:
        try:
            return await m.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
        except TelegramBadRequest:
            pass
    await m.answer(text, parse_mode="HTML", reply_markup=b.as_markup())


@router.callback_query(New.category, F.data.startswith("cat:"))
async def cat_open(c: CallbackQuery, state: FSMContext):
    await show_categories(c.message, state, int(c.data.split(":")[1]), edit=True)
    await c.answer()


@router.callback_query(New.category, F.data.startswith("catsel:"))
async def cat_select(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    cats = await get_categories()
    name = next((x["name"] for x in cats if x["id"] == cid), "")
    await state.update_data(category_id=cid, category_name=name)
    await finish_search(c.message, state, c.from_user.id)
    await c.answer()


@router.callback_query(New.category, F.data == "skip")
async def cat_skip(c: CallbackQuery, state: FSMContext):
    await state.update_data(category_id=None, category_name="")
    await finish_search(c.message, state, c.from_user.id)
    await c.answer()


async def finish_search(m: Message, state: FSMContext, user_id=None):
    d = await state.get_data()
    await state.clear()
    user_id = user_id or m.chat.id
    sid = await q_exec(
        "INSERT INTO searches(user_id,keywords,minus,price_max,category_id,category_name)"
        " VALUES(?,?,?,?,?,?)",
        (user_id, d.get("keywords", ""), d.get("minus", ""), d.get("price_max"),
         d.get("category_id"), d.get("category_name", "")))
    s = (await q_all("SELECT * FROM searches WHERE id=?", (sid,)))[0]
    await m.answer(f"✅ Поиск создан:\n<b>{html.escape(search_title(s))}</b>\n\n"
                   f"Сейчас пришлю несколько последних объявлений, дальше — только новые.",
                   parse_mode="HTML", reply_markup=main_kb())

    # первый запуск: помечаем всё как увиденное и показываем 3 последних подходящих
    offers = await fetch_offers(s["keywords"], s["category_id"], s["price_max"])
    minus = [w.strip().lower() for w in s["minus"].split(",") if w.strip()]
    for o in offers:
        await q_exec("INSERT OR IGNORE INTO seen VALUES(?,?)", (sid, o["id"]))
    good = [o for o in offers if passes_filters(o, minus, s["price_max"])][:3]
    for o in reversed(good):
        await send_offer(m.bot, user_id, o, search_title(s))


@router.message(Command("list"))
@router.callback_query(F.data == "list")
async def list_searches(evt):
    m = evt.message if isinstance(evt, CallbackQuery) else evt
    uid = evt.from_user.id
    rows = await q_all("SELECT * FROM searches WHERE user_id=?", (uid,))
    if not rows:
        await m.answer("У вас пока нет поисков.", reply_markup=main_kb())
    for s in rows:
        b = InlineKeyboardBuilder()
        b.button(text="⏸ Пауза" if s["active"] else "▶️ Включить", callback_data=f"tog:{s['id']}")
        b.button(text="🗑 Удалить", callback_data=f"del:{s['id']}")
        await m.answer(f"{'🟢' if s['active'] else '⏸'} <b>#{s['id']}</b> "
                       f"{html.escape(search_title(s))}",
                       parse_mode="HTML", reply_markup=b.as_markup())
    if isinstance(evt, CallbackQuery):
        await evt.answer()


@router.callback_query(F.data.startswith("tog:"))
async def toggle(c: CallbackQuery):
    sid = int(c.data.split(":")[1])
    await q_exec("UPDATE searches SET active=1-active WHERE id=? AND user_id=?", (sid, c.from_user.id))
    s = await q_all("SELECT * FROM searches WHERE id=?", (sid,))
    state = "включён ▶️" if s and s[0]["active"] else "на паузе ⏸"
    await c.message.edit_text(f"Поиск #{sid} {state}")
    await c.answer()


@router.callback_query(F.data.startswith("del:"))
async def delete(c: CallbackQuery):
    sid = int(c.data.split(":")[1])
    await q_exec("DELETE FROM searches WHERE id=? AND user_id=?", (sid, c.from_user.id))
    await q_exec("DELETE FROM seen WHERE search_id=?", (sid,))
    await c.message.edit_text(f"🗑 Поиск #{sid} удалён")
    await c.answer()


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    global http
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")
    await db_init()
    http = aiohttp.ClientSession()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    task = asyncio.create_task(poller(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
