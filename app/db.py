"""База заказов (SQLite — один файл data/shop.db, отдельный сервер не нужен).

Статусы заказа:
    new      — заказ создан, ждём оплату
    paid     — оплата подтверждена ЮKassa
    issued   — покупателю выдана личная ссылка в канал
    canceled — платёж отменён или истёк
    revoked  — доступ отозван администратором
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

HAS_ACCESS = ("paid", "issued")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    email       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token            TEXT NOT NULL UNIQUE,
    telegram_id      INTEGER,
    amount           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'new',
    source           TEXT NOT NULL DEFAULT 'yookassa',
    email            TEXT,
    payment_id       TEXT UNIQUE,
    confirmation_url TEXT,
    invite_link      TEXT,
    reminded         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    paid_at          TEXT,
    issued_at        TEXT
);
CREATE INDEX IF NOT EXISTS orders_tg ON orders (telegram_id);
CREATE INDEX IF NOT EXISTS orders_status ON orders (status);
CREATE TABLE IF NOT EXISTS join_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    approved    INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat(timespec="seconds")


@dataclass
class Order:
    id: int
    token: str
    telegram_id: int | None
    amount: str
    status: str
    source: str
    email: str | None
    payment_id: str | None
    confirmation_url: str | None
    invite_link: str | None
    reminded: int
    created_at: str
    paid_at: str | None
    issued_at: str | None


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, *args) -> Order | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return Order(**dict(row)) if row else None

    async def _many(self, sql: str, *args) -> list[Order]:
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [Order(**dict(r)) for r in rows]

    async def _update(self, sql: str, *args) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.rowcount

    # ---------- покупатели ----------

    async def save_user(self, telegram_id: int, username: str | None, first_name: str | None) -> None:
        await self.conn.execute(
            """INSERT INTO users (telegram_id, username, first_name, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   username = excluded.username, first_name = excluded.first_name""",
            (telegram_id, username, first_name, now()),
        )
        await self.conn.commit()

    async def get_email(self, telegram_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT email FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["email"] if row else None

    async def set_email(self, telegram_id: int, email: str) -> None:
        await self._update("UPDATE users SET email = ? WHERE telegram_id = ?", email, telegram_id)

    # ---------- заказы ----------

    async def create_order(
        self, telegram_id: int | None, amount: str, email: str | None = None,
        status: str = "new", source: str = "yookassa",
    ) -> Order:
        token = secrets.token_urlsafe(16)
        stamp = now()
        await self.conn.execute(
            """INSERT INTO orders (token, telegram_id, amount, status, source, email, created_at, paid_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (token, telegram_id, amount, status, source, email, stamp,
             stamp if status == "paid" else None),
        )
        await self.conn.commit()
        return await self.get_by_token(token)

    async def get(self, order_id: int) -> Order | None:
        return await self._one("SELECT * FROM orders WHERE id = ?", order_id)

    async def get_by_token(self, token: str) -> Order | None:
        return await self._one("SELECT * FROM orders WHERE token = ?", token)

    async def set_payment(self, order_id: int, payment_id: str, confirmation_url: str) -> None:
        await self._update(
            "UPDATE orders SET payment_id = ?, confirmation_url = ? WHERE id = ?",
            payment_id, confirmation_url, order_id,
        )

    async def open_order(self, telegram_id: int, max_age_minutes: int = 50) -> Order | None:
        """Неоплаченный заказ, ссылку на оплату которого ещё можно отправить повторно."""
        return await self._one(
            """SELECT * FROM orders WHERE telegram_id = ? AND status = 'new'
               AND confirmation_url IS NOT NULL AND created_at > ?
               ORDER BY id DESC LIMIT 1""",
            telegram_id, ago(minutes=max_age_minutes),
        )

    async def access_order(self, telegram_id: int) -> Order | None:
        """Оплаченный заказ покупателя — значит, ему можно в канал."""
        return await self._one(
            f"""SELECT * FROM orders WHERE telegram_id = ? AND status IN {HAS_ACCESS}
                ORDER BY id DESC LIMIT 1""",
            telegram_id,
        )

    async def bind(self, order_id: int, telegram_id: int) -> bool:
        """Одноразовая привязка заказа к Telegram-аккаунту. Сработает только у первого."""
        return await self._update(
            "UPDATE orders SET telegram_id = ? WHERE id = ? AND telegram_id IS NULL",
            telegram_id, order_id,
        ) == 1

    async def mark_paid(self, order_id: int) -> bool:
        """new → paid. True только у того, кто перевёл статус первым (защита от двойной выдачи)."""
        return await self._update(
            "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ? AND status = 'new'",
            now(), order_id,
        ) == 1

    async def mark_issued(self, order_id: int, invite_link: str | None = None) -> None:
        await self._update(
            """UPDATE orders SET status = 'issued',
                   invite_link = COALESCE(?, invite_link),
                   issued_at = COALESCE(issued_at, ?)
               WHERE id = ? AND status IN ('paid', 'issued')""",
            invite_link, now(), order_id,
        )

    async def mark_canceled(self, order_id: int) -> None:
        await self._update(
            "UPDATE orders SET status = 'canceled' WHERE id = ? AND status = 'new'", order_id
        )

    async def revoke(self, telegram_id: int) -> int:
        return await self._update(
            f"UPDATE orders SET status = 'revoked' WHERE telegram_id = ? AND status IN {HAS_ACCESS}",
            telegram_id,
        )

    async def pending_payments(self) -> list[Order]:
        """Неоплаченные заказы за последние сутки — их статус перепроверяем в ЮKassa."""
        return await self._many(
            """SELECT * FROM orders WHERE status = 'new' AND payment_id IS NOT NULL
               AND created_at > ?""",
            ago(days=1),
        )

    async def need_reminder(self, minutes: int) -> list[Order]:
        """Создали заказ, но не оплатили — кому пора напомнить (один раз)."""
        return await self._many(
            f"""SELECT * FROM orders o WHERE o.status = 'new' AND o.reminded = 0
                AND o.created_at < ? AND o.created_at > ?
                AND o.id = (SELECT MAX(id) FROM orders WHERE telegram_id = o.telegram_id)
                AND NOT EXISTS (SELECT 1 FROM orders p WHERE p.telegram_id = o.telegram_id
                                AND p.status IN {HAS_ACCESS})""",
            ago(minutes=minutes), ago(days=2),
        )

    async def set_reminded(self, order_id: int) -> None:
        await self._update("UPDATE orders SET reminded = 1 WHERE id = ?", order_id)

    # ---------- попытки вступить в канал ----------

    async def log_join(self, telegram_id: int, approved: bool) -> None:
        await self._update(
            "INSERT INTO join_attempts (telegram_id, approved, created_at) VALUES (?, ?, ?)",
            telegram_id, int(approved), now(),
        )

    async def stats(self) -> dict:
        result = {}
        async with self.conn.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status") as cur:
            for row in await cur.fetchall():
                result[row["status"]] = row["c"]
        async with self.conn.execute(
            f"SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) s FROM orders "
            f"WHERE status IN {HAS_ACCESS} AND source = 'yookassa'"
        ) as cur:
            result["revenue"] = (await cur.fetchone())["s"]
        async with self.conn.execute("SELECT COUNT(*) c FROM users") as cur:
            result["users"] = (await cur.fetchone())["c"]
        async with self.conn.execute(
            "SELECT COUNT(DISTINCT telegram_id) c FROM join_attempts WHERE approved = 0"
        ) as cur:
            result["rejected"] = (await cur.fetchone())["c"]
        return result
