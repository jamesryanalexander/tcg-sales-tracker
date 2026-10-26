from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

import db as db_module

# -----------------------------------------------------------------------------
# Header/row normalization -- ported from basic-land-tracker/app.py's
# normalize_header()/first_present()/normalized_csv_rows(), since TCGPlayer's
# exact export column names aren't guaranteed to stay fixed and we want the
# same tolerant matching that app already proved out for CSV imports.
# -----------------------------------------------------------------------------

def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def first_present(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in row and row[key] is not None and str(row[key]).strip() != "":
            return str(row[key]).strip()
    return ""


def normalized_csv_rows(path: Path) -> Iterator[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return
        normalized = {field: normalize_header(field) for field in reader.fieldnames}
        for raw_row in reader:
            yield {normalized[key]: (value or "").strip() for key, value in raw_row.items() if key is not None}


def _money_or_none(value: str) -> Optional[float]:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", value)
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y")


def _date_or_none(value: str) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    import datetime as dt

    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    # Already ISO-ish (e.g. has a time component)? Take the date portion.
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else None


# Canonical field -> candidate normalized headers. TCGPlayer's "Export
# Shipping" CSV columns aren't fully confirmed, so this list is deliberately
# generous; any field that doesn't match stays None rather than failing the
# import, and the order is flagged needs_review instead (see upsert_order()).
ORDER_COLUMN_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "order_number":       ("order_number", "order__", "order_id", "order_no"),
    "order_date":         ("order_date", "date", "order_placed_date", "date_of_purchase"),
    "tracking_number":    ("tracking_number", "tracking__", "tracking"),
    "product_weight_oz":  ("product_weight_oz", "product_weight", "weight_oz", "weight"),
    "value_of_products":  (
        "value_of_products", "value_of_product", "product_value", "subtotal",
        "total_price_of_products", "total_product_value",
    ),
    "shipping_charged":   (
        "shipping_charged", "shipping_fee_charged", "buyer_shipping",
        "shipping", "shipping_price_charged",
    ),
    "marketplace_fee":    ("marketplace_fee", "fees", "tcgplayer_fee", "commission", "selling_fees"),
    "postage_cost":       ("postage_cost", "postage", "shipping_cost", "shipping_label_cost"),
    "buyer_location":     ("ship_to_address_state", "buyer_location", "ship_to_state", "state"),
}


def map_order_row(row: Dict[str, str]) -> Dict[str, Any]:
    """CSV row (already passed through normalized_csv_rows) -> canonical
    order dict. Any field whose column isn't present in this export stays
    None; upsert_order() decides what that means for review status."""

    def field(name: str) -> str:
        return first_present(row, *ORDER_COLUMN_CANDIDATES[name])

    return {
        "order_number": field("order_number") or None,
        "order_date": _date_or_none(field("order_date")) or db_module.now_iso()[:10],
        "tracking_number": field("tracking_number") or None,
        "product_weight_oz": _money_or_none(field("product_weight_oz")),
        "value_of_products": _money_or_none(field("value_of_products")),
        "shipping_charged": _money_or_none(field("shipping_charged")),
        "marketplace_fee": _money_or_none(field("marketplace_fee")),
        "postage_cost": _money_or_none(field("postage_cost")),
        "buyer_location": field("buyer_location") or None,
    }


def upsert_order(
    db: sqlite3.Connection, order: Dict[str, Any], import_log_id: Optional[int]
) -> Tuple[int, bool]:
    """Canonical ingestion core -- this app's equivalent of
    collection-tracker/sync.py's upsert_items(). Fed by the CSV importer
    today; a future tcgplayer.py API sync should call this exact function
    with its own mapped order dict so no ingestion logic needs to change.

    Dedup is by order_number via a partial UNIQUE index (NULLs, i.e. manual
    entries with no order number, never collide). A duplicate order_number
    is left untouched -- it does not overwrite an already-reviewed order."""
    now = db_module.now_iso()
    needs_review = any(
        order.get(f) is None for f in ("marketplace_fee", "postage_cost", "shipping_charged")
    )
    status = "needs_review" if needs_review else "manual"

    if order.get("order_number"):
        existing = db.execute(
            "SELECT id FROM orders WHERE order_number = ?", (order["order_number"],)
        ).fetchone()
        if existing is not None:
            return existing["id"], False

    cur = db.execute(
        "INSERT INTO orders ("
        "order_number, order_date, buyer_location, tracking_number, product_weight_oz, "
        "value_of_products, shipping_charged, marketplace_fee, postage_cost, status, "
        "source, import_log_id, created_at, updated_at"
        ") VALUES ("
        ":order_number, :order_date, :buyer_location, :tracking_number, :product_weight_oz, "
        ":value_of_products, :shipping_charged, :marketplace_fee, :postage_cost, :status, "
        "'csv_import', :import_log_id, :created_at, :updated_at"
        ")",
        {**order, "status": status, "import_log_id": import_log_id, "created_at": now, "updated_at": now},
    )
    return cur.lastrowid, True


def create_placeholder_line_item(db: sqlite3.Connection, order_id: int, order: Dict[str, Any]) -> None:
    """TCGPlayer's order-level export has no per-card breakdown, so each
    imported order gets one placeholder line item the user splits into real
    per-card rows (with COGS filled in) via the normal sale edit form."""
    now = db_module.now_iso()
    db.execute(
        "INSERT INTO sale_line_items "
        "(order_id, product_name, quantity, sale_price_total, cogs_total, needs_review, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, NULL, 1, ?, ?)",
        (
            order_id,
            "(review needed — split into per-card line items)",
            order.get("value_of_products") or 0,
            now,
            now,
        ),
    )


def start_import_log(db: sqlite3.Connection, import_type: str, filename: Optional[str]) -> int:
    cur = db.execute(
        "INSERT INTO import_log (import_type, filename, created_at) VALUES (?, ?, ?)",
        (import_type, filename, db_module.now_iso()),
    )
    db.commit()
    return cur.lastrowid


def finish_import_log(db: sqlite3.Connection, import_log_id: int, counts: Dict[str, int], message: str = "") -> None:
    db.execute(
        "UPDATE import_log SET rows_seen=?, orders_created=?, orders_updated=?, "
        "orders_skipped_duplicate=?, rows_needing_review=?, message=? WHERE id=?",
        (
            counts.get("rows_seen", 0),
            counts.get("orders_created", 0),
            counts.get("orders_updated", 0),
            counts.get("orders_skipped_duplicate", 0),
            counts.get("rows_needing_review", 0),
            message,
            import_log_id,
        ),
    )
    db.commit()


def import_orders_csv(db: sqlite3.Connection, path: Path, filename: str) -> Dict[str, Any]:
    """Main entry point for /import. Returns a counts dict used for both the
    flash message and the import_log row."""
    import_log_id = start_import_log(db, "orders_csv", filename)

    rows_seen = 0
    orders_created = 0
    orders_skipped_duplicate = 0
    rows_needing_review = 0

    for raw_row in normalized_csv_rows(path):
        rows_seen += 1
        order = map_order_row(raw_row)
        order_id, created = upsert_order(db, order, import_log_id)
        if not created:
            orders_skipped_duplicate += 1
            continue
        orders_created += 1
        create_placeholder_line_item(db, order_id, order)
        if order["marketplace_fee"] is None or order["postage_cost"] is None or order["shipping_charged"] is None:
            rows_needing_review += 1

    db.commit()
    counts = {
        "rows_seen": rows_seen,
        "orders_created": orders_created,
        "orders_updated": 0,
        "orders_skipped_duplicate": orders_skipped_duplicate,
        "rows_needing_review": rows_needing_review,
    }
    finish_import_log(db, import_log_id, counts, "Orders CSV import completed")
    return counts
