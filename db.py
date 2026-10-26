from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing
from typing import Optional

from flask import g

from config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_log (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    import_type               TEXT NOT NULL,              -- 'orders_csv' today; 'api_sync' later
    filename                  TEXT,
    rows_seen                 INTEGER NOT NULL DEFAULT 0,
    orders_created            INTEGER NOT NULL DEFAULT 0,
    orders_updated            INTEGER NOT NULL DEFAULT 0,
    orders_skipped_duplicate  INTEGER NOT NULL DEFAULT 0,
    rows_needing_review       INTEGER NOT NULL DEFAULT 0,
    message                   TEXT,
    created_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expense_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_date  TEXT NOT NULL,                  -- ISO date, YYYY-MM-DD
    amount        REAL NOT NULL,
    category_id   INTEGER NOT NULL REFERENCES expense_categories(id),
    description   TEXT NOT NULL,
    vendor        TEXT,
    receipt_note  TEXT,
    receipt_file  TEXT,                            -- relative path under data/uploads/receipts/
    source        TEXT NOT NULL DEFAULT 'manual',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expenses_date     ON expenses(expense_date);
CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category_id);

CREATE TABLE IF NOT EXISTS orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number       TEXT,                        -- NULL allowed for manual entries
    order_date         TEXT NOT NULL,                -- YYYY-MM-DD
    channel            TEXT NOT NULL DEFAULT 'tcgplayer',
    buyer_location     TEXT,
    tracking_number    TEXT,
    product_weight_oz  REAL,
    value_of_products  REAL,                        -- buyer-paid product total excl. shipping
    shipping_charged   REAL,                        -- buyer-paid shipping (revenue)
    marketplace_fee    REAL,                        -- nullable; often absent from per-order CSV
    postage_cost       REAL,                        -- nullable; what the seller actually paid
    other_fees         REAL,
    status             TEXT NOT NULL DEFAULT 'manual'
                           CHECK (status IN ('manual', 'needs_review', 'reviewed')),
    source             TEXT NOT NULL DEFAULT 'manual'
                           CHECK (source IN ('manual', 'csv_import')),
    notes              TEXT,
    import_log_id      INTEGER REFERENCES import_log(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_order_number
    ON orders(order_number) WHERE order_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_date   ON orders(order_date);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS sale_line_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_name      TEXT NOT NULL,
    set_name          TEXT,
    card_number       TEXT,
    condition         TEXT,
    language          TEXT,
    quantity          INTEGER NOT NULL DEFAULT 1,
    sale_price_total  REAL NOT NULL DEFAULT 0,      -- this line's share of buyer-paid product price
    cogs_total        REAL,                          -- user-entered; NULL until filled in review
    needs_review      INTEGER NOT NULL DEFAULT 0,    -- 1 = auto placeholder from import, not yet split
    notes             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_line_items_order ON sale_line_items(order_id);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# Seeded via INSERT OR IGNORE keyed on name, so re-running is always safe and
# a user's own custom categories (added via /settings) are never touched.
DEFAULT_EXPENSE_CATEGORIES = [
    "Shipping Supplies",
    "Postage",
    "Marketplace Fees",
    "Software/Subscriptions",
    "Office/Home Office",
    "Equipment",
    "Professional Services",
    "Returns/Refunds",
    "Other",
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA journal_mode = WAL")
        g.db = db
    return g.db


def close_db(error: Optional[BaseException] = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def seed_default_categories(db: sqlite3.Connection) -> None:
    now = now_iso()
    for i, name in enumerate(DEFAULT_EXPENSE_CATEGORIES):
        db.execute(
            "INSERT OR IGNORE INTO expense_categories (name, sort_order, created_at) VALUES (?, ?, ?)",
            (name, i, now),
        )
    db.commit()


def init_db() -> None:
    """Creates the schema if missing. Safe to call repeatedly -- see
    collection-tracker/db.py's init_db() for why this opens its own
    connection rather than reusing flask.g."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        db.commit()
        seed_default_categories(db)
