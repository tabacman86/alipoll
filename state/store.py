import logging
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from scraper.models import Order

logger = logging.getLogger(__name__)

_COMPLETED_ARCHIVE_DAYS = 14

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,
    item_name         TEXT NOT NULL,
    status            TEXT NOT NULL,
    tracking_number   TEXT,
    estimated_delivery TEXT,
    seller            TEXT,
    recipient         TEXT,
    order_url         TEXT NOT NULL,
    order_date        TEXT,
    last_seen         TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    sub_status        TEXT
)
"""

_MIGRATE_COLS = [
    ("recipient",    "TEXT"),
    ("order_date",   "TEXT"),
    ("completed_at", "TEXT"),
    ("sub_status",   "TEXT"),
]

_TRANSLATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS translations (
    original  TEXT PRIMARY KEY,
    hebrew    TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class OrderStateStore:
    def __init__(self, db_path: str = "data/orders.db"):
        self._db_path = Path(db_path)

    async def init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_CREATE_TABLE)
            await db.execute(_TRANSLATIONS_TABLE)
            # Migrate existing DB: add new columns if missing
            async with db.execute("PRAGMA table_info(orders)") as cur:
                existing_cols = {row[1] async for row in cur}
            for col_name, col_type in _MIGRATE_COLS:
                if col_name not in existing_cols:
                    await db.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type}")
                    logger.info("Migrated DB: added column '%s'", col_name)
            await db.commit()
        logger.info("Database ready at %s", self._db_path)

    async def get_all(self) -> dict[str, dict]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM orders") as cursor:
                rows = await cursor.fetchall()
        return {row["order_id"]: dict(row) for row in rows}

    async def get_active(self) -> dict[str, dict]:
        """All orders excluding those completed more than 2 weeks ago."""
        cutoff = (datetime.utcnow() - timedelta(days=_COMPLETED_ARCHIVE_DAYS)).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM orders
                   WHERE lower(status) NOT IN ('cancelled', 'canceled', 'closed')
                     AND (completed_at IS NULL OR completed_at > ?)""",
                (cutoff,),
            ) as cursor:
                rows = await cursor.fetchall()
        return {row["order_id"]: dict(row) for row in rows}

    async def upsert(self, order: Order) -> str | None:
        """Insert or update. Returns previous status if changed, else None."""
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT status, completed_at FROM orders WHERE order_id = ?", (order.order_id,)
            ) as cursor:
                existing = await cursor.fetchone()

            old_status = existing[0] if existing else None
            old_completed_at = existing[1] if existing else None
            status_changed = old_status is not None and old_status != order.status

            # Record when an order first becomes Completed
            new_completed_at = old_completed_at
            if order.status.lower() == "completed" and not old_completed_at:
                new_completed_at = now
            elif order.status.lower() != "completed":
                new_completed_at = None  # reset if re-opened

            await db.execute(
                """
                INSERT INTO orders
                    (order_id, item_name, status, tracking_number, estimated_delivery,
                     seller, recipient, sub_status, order_url, order_date, last_seen, updated_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    item_name         = CASE WHEN excluded.item_name = 'Unknown item' THEN item_name ELSE excluded.item_name END,
                    status            = excluded.status,
                    tracking_number   = COALESCE(excluded.tracking_number, tracking_number),
                    estimated_delivery = COALESCE(excluded.estimated_delivery, estimated_delivery),
                    seller            = COALESCE(excluded.seller, seller),
                    recipient         = COALESCE(excluded.recipient, recipient),
                    sub_status        = COALESCE(excluded.sub_status, sub_status),
                    order_url         = excluded.order_url,
                    order_date        = COALESCE(excluded.order_date, order_date),
                    last_seen         = excluded.last_seen,
                    completed_at      = excluded.completed_at,
                    updated_at        = CASE WHEN status != excluded.status
                                             THEN excluded.updated_at
                                             ELSE updated_at END
                """,
                (
                    order.order_id,
                    order.item_name,
                    order.status,
                    order.tracking_number,
                    order.estimated_delivery,
                    order.seller,
                    order.recipient,
                    order.sub_status,
                    order.order_url,
                    order.order_date.isoformat() if order.order_date else None,
                    order.last_seen.isoformat(),
                    now,
                    new_completed_at,
                ),
            )
            await db.commit()

        return old_status if status_changed else None

    async def get_changed(self, new_orders: list[Order]) -> list[tuple[Order, str]]:
        existing = await self.get_active()
        changed: list[tuple[Order, str]] = []
        for order in new_orders:
            if order.order_id not in existing:
                changed.append((order, "NEW"))
            elif existing[order.order_id]["status"] != order.status:
                changed.append((order, existing[order.order_id]["status"]))
        return changed

    async def upsert_all(self, orders: list[Order]) -> None:
        for order in orders:
            await self.upsert(order)

    async def get_recipients(self) -> dict[str, str | None]:
        """Returns {order_id: recipient} for all stored orders."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT order_id, recipient FROM orders") as cursor:
                rows = await cursor.fetchall()
        return {row["order_id"]: row["recipient"] for row in rows}

    async def count(self) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM orders") as cursor:
                row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Translation cache ────────────────────────────────────────────────────

    async def get_translation(self, original: str) -> str | None:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT hebrew FROM translations WHERE original = ?", (original,)
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def save_translation(self, original: str, hebrew: str) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO translations (original, hebrew, created_at) VALUES (?, ?, ?)",
                (original, hebrew, now),
            )
            await db.commit()
