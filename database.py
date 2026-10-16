import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "deals.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 chat_id INTEGER NOT NULL,
 message_id INTEGER NOT NULL DEFAULT 0,
 creator_id INTEGER NOT NULL,
 creator_name TEXT NOT NULL,
 accepted_by INTEGER,
 rejected_by INTEGER,
 gift_url TEXT NOT NULL,
 gift_title TEXT NOT NULL,
 amount INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',
 expires_at TEXT NOT NULL,
 verification_note TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

async def create_deal(**k):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO deals(chat_id,message_id,creator_id,creator_name,gift_url,gift_title,amount,expires_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (k["chat_id"], k.get("message_id", 0), k["creator_id"], k["creator_name"],
             k["gift_url"], k["gift_title"], k["amount"], k["expires_at"])
        )
        await db.commit()
        return cur.lastrowid

async def get_deal(deal_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM deals WHERE id=?", (deal_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def update_deal(deal_id, **kwargs):
    allowed = {"message_id", "status", "accepted_by", "rejected_by", "verification_note"}
    pairs = [(k, v) for k, v in kwargs.items() if k in allowed]
    if not pairs:
        return
    sql = ", ".join(f"{k}=?" for k, _ in pairs)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE deals SET {sql} WHERE id=?", [v for _, v in pairs] + [deal_id])
        await db.commit()

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()

async def get_setting(key):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def add_admin(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
        await db.commit()

async def remove_admin(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        await db.commit()

async def list_admins(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,))
        return await cur.fetchone() is not None

async def get_active_deals():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM deals WHERE status IN ('PENDING','ACCEPTED')")
        return [dict(r) for r in await cur.fetchall()]

async def expire_pending_deals(now_iso):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deals SET status='EXPIRED' WHERE status='PENDING' AND expires_at <= ?", (now_iso,))
        await db.commit()
