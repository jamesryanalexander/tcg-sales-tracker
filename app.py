from __future__ import annotations

import csv
import datetime as dt
import io
import os
import time
from pathlib import Path
from typing import Any, Optional

import click
from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import config
import db as db_module
import importer
import reports

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)

# Site-level gate -- same opt-in both-or-neither pattern as collection-tracker
# and basic-land-tracker. This app stores real sales/expense records used for
# tax filing, not just collectible values, so enabling it is strongly
# recommended once this is reachable from the internet (see README).
AUTH_USERNAME = os.environ.get("AUTH_USERNAME")
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH")
AUTH_ENABLED = bool(AUTH_USERNAME or AUTH_PASSWORD_HASH)
if AUTH_ENABLED and not (AUTH_USERNAME and AUTH_PASSWORD_HASH):
    raise RuntimeError(
        "Set both AUTH_USERNAME and AUTH_PASSWORD_HASH together (or neither, for local dev)."
    )

if AUTH_ENABLED:
    _secret_key = os.environ.get("SECRET_KEY")
    if not _secret_key:
        raise RuntimeError(
            "SECRET_KEY must be set explicitly when AUTH_USERNAME/AUTH_PASSWORD_HASH are configured."
        )
    app.config["SECRET_KEY"] = _secret_key
else:
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-dev-tcg-sales-tracker")

app.config["MAX_CONTENT_LENGTH"] = config.get_max_upload_mb() * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SESSION_COOKIE_SECURE", "1" if AUTH_ENABLED else "0"
) == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(days=30)

SITE_PUBLIC_ENDPOINTS = {"site_login", "static"}


# -----------------------------------------------------------------------------
# DB lifecycle
# -----------------------------------------------------------------------------

@app.teardown_appcontext
def _close_db(error: Optional[BaseException] = None) -> None:
    db_module.close_db(error)


def _bootstrap_db() -> None:
    db_module.init_db()


@app.cli.command("init-db")
def init_db_command() -> None:
    db_module.init_db()
    print(f"Initialized {config.DB_PATH}")


@app.cli.command("hash-password")
@click.argument("password")
def hash_password_command(password: str) -> None:
    """Print a password hash suitable for the AUTH_PASSWORD_HASH env var."""
    print(generate_password_hash(password))


@app.cli.command("seed-fixture")
@click.argument("path", default=str(BASE_DIR / "tests" / "fixtures" / "sample_orders_export.csv"))
def seed_fixture_command(path: str) -> None:
    """Loads a sample TCGPlayer-shaped orders CSV through the same
    importer.import_orders_csv() code path a real upload uses."""
    with app.app_context():
        db_module.init_db()
        db = db_module.get_db()
        counts = importer.import_orders_csv(db, Path(path), Path(path).name)
        print(
            f"Seeded {counts['rows_seen']} rows: {counts['orders_created']} orders created, "
            f"{counts['orders_skipped_duplicate']} duplicates skipped, "
            f"{counts['rows_needing_review']} needing review."
        )


# -----------------------------------------------------------------------------
# Site-level auth gate (opt-in, off by default)
# -----------------------------------------------------------------------------

@app.before_request
def _require_site_login():
    if not AUTH_ENABLED:
        return None
    if request.endpoint in SITE_PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if session.get("site_authed"):
        return None
    return redirect(url_for("site_login", next=request.full_path))


@app.route("/site-login", methods=["GET", "POST"])
def site_login():
    if not AUTH_ENABLED:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == AUTH_USERNAME and check_password_hash(AUTH_PASSWORD_HASH, password):
            session.clear()
            session["site_authed"] = True
            session.permanent = True
            next_url = request.form.get("next") or url_for("dashboard")
            return redirect(next_url)
        time.sleep(1)  # crude brute-force slowdown
        flash("Invalid username or password.", "error")
    return render_template("site_login.html", next=request.args.get("next", ""))


@app.post("/site-logout")
def site_logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("site_login"))


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _float_or_none(value: str) -> Optional[float]:
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _year_arg() -> Optional[int]:
    raw = request.args.get("year", "").strip()
    if raw.isdigit():
        return int(raw)
    return None


def _order_from_form(form) -> dict:
    return {
        "order_number": form.get("order_number", "").strip() or None,
        "order_date": form.get("order_date", "").strip() or dt.date.today().isoformat(),
        "buyer_location": form.get("buyer_location", "").strip() or None,
        "tracking_number": form.get("tracking_number", "").strip() or None,
        "product_weight_oz": _float_or_none(form.get("product_weight_oz", "")),
        "value_of_products": _float_or_none(form.get("value_of_products", "")),
        "shipping_charged": _float_or_none(form.get("shipping_charged", "")),
        "marketplace_fee": _float_or_none(form.get("marketplace_fee", "")),
        "postage_cost": _float_or_none(form.get("postage_cost", "")),
        "other_fees": _float_or_none(form.get("other_fees", "")),
        "notes": form.get("notes", "").strip() or None,
    }


def _sync_line_items(db, order_id: int) -> None:
    """Reconciles the repeatable line-item fieldset on sale_form.html against
    existing sale_line_items rows for this order: blank line_item_id ->
    insert, present -> update, an existing id not resubmitted -> delete.
    Any row that reaches here was explicitly looked at in the form, so it's
    no longer a review placeholder."""
    now = db_module.now_iso()
    ids = request.form.getlist("line_item_id")
    product_names = request.form.getlist("product_name")
    set_names = request.form.getlist("set_name")
    card_numbers = request.form.getlist("card_number")
    conditions = request.form.getlist("condition")
    languages = request.form.getlist("language")
    quantities = request.form.getlist("quantity")
    sale_prices = request.form.getlist("sale_price_total")
    cogs_totals = request.form.getlist("cogs_total")
    line_notes = request.form.getlist("line_notes")

    submitted_ids = set()
    for i, product_name in enumerate(product_names):
        product_name = product_name.strip()
        if not product_name:
            continue
        line_id = ids[i].strip() if i < len(ids) else ""
        line_id = line_id if line_id.isdigit() else ""
        quantity = int(quantities[i]) if i < len(quantities) and quantities[i].strip().isdigit() else 1
        row = {
            "order_id": order_id,
            "product_name": product_name,
            "set_name": (set_names[i].strip() or None) if i < len(set_names) else None,
            "card_number": (card_numbers[i].strip() or None) if i < len(card_numbers) else None,
            "condition": (conditions[i].strip() or None) if i < len(conditions) else None,
            "language": (languages[i].strip() or None) if i < len(languages) else None,
            "quantity": quantity,
            "sale_price_total": _float_or_none(sale_prices[i]) if i < len(sale_prices) else None or 0,
            "cogs_total": _float_or_none(cogs_totals[i]) if i < len(cogs_totals) else None,
            "notes": (line_notes[i].strip() or None) if i < len(line_notes) else None,
            "updated_at": now,
        }
        row["sale_price_total"] = row["sale_price_total"] or 0

        if line_id:
            submitted_ids.add(int(line_id))
            db.execute(
                "UPDATE sale_line_items SET product_name=:product_name, set_name=:set_name, "
                "card_number=:card_number, condition=:condition, language=:language, "
                "quantity=:quantity, sale_price_total=:sale_price_total, cogs_total=:cogs_total, "
                "notes=:notes, needs_review=0, updated_at=:updated_at "
                "WHERE id=:line_id AND order_id=:order_id",
                {**row, "line_id": int(line_id)},
            )
        else:
            row["created_at"] = now
            cur = db.execute(
                "INSERT INTO sale_line_items ("
                "order_id, product_name, set_name, card_number, condition, language, quantity, "
                "sale_price_total, cogs_total, needs_review, notes, created_at, updated_at"
                ") VALUES ("
                ":order_id, :product_name, :set_name, :card_number, :condition, :language, :quantity, "
                ":sale_price_total, :cogs_total, 0, :notes, :created_at, :updated_at)",
                row,
            )
            submitted_ids.add(cur.lastrowid)

    existing_ids = {
        r["id"] for r in db.execute("SELECT id FROM sale_line_items WHERE order_id = ?", (order_id,))
    }
    to_delete = existing_ids - submitted_ids
    if to_delete:
        db.executemany("DELETE FROM sale_line_items WHERE id = ?", [(i,) for i in to_delete])


def _save_receipt_upload() -> Optional[str]:
    uploaded = request.files.get("receipt_file")
    if not uploaded or uploaded.filename == "":
        return None
    receipt_dir = config.UPLOAD_DIR / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{db_module.now_iso().replace(':', '-')}_{secure_filename(uploaded.filename)}"
    uploaded.save(receipt_dir / filename)
    return f"receipts/{filename}"


# -----------------------------------------------------------------------------
# Dashboard / P&L
# -----------------------------------------------------------------------------

@app.route("/")
def dashboard():
    db = db_module.get_db()
    year = _year_arg()
    pnl = reports.pnl_for_year(db, year)
    recent_orders = db.execute(
        "SELECT * FROM orders ORDER BY order_date DESC, id DESC LIMIT 10"
    ).fetchall()
    return render_template(
        "dashboard.html",
        pnl=pnl,
        years=reports.available_years(db),
        selected_year=year,
        recent_orders=recent_orders,
    )


# -----------------------------------------------------------------------------
# Sales (orders + line items)
# -----------------------------------------------------------------------------

@app.route("/sales")
def sales_list():
    db = db_module.get_db()
    year = _year_arg()
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    clauses = []
    params: list[Any] = []
    if year:
        clauses.append("substr(order_date, 1, 4) = ?")
        params.append(str(year))
    if status in ("manual", "needs_review", "reviewed"):
        clauses.append("status = ?")
        params.append(status)
    if q:
        clauses.append(
            "(order_number LIKE ? OR id IN (SELECT order_id FROM sale_line_items WHERE product_name LIKE ?))"
        )
        params.extend([f"%{q}%", f"%{q}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    orders = db.execute(
        f"SELECT * FROM orders {where} ORDER BY order_date DESC, id DESC LIMIT 500", params
    ).fetchall()
    return render_template(
        "sales_list.html",
        orders=orders,
        years=reports.available_years(db),
        selected_year=year,
        status=status,
        q=q,
    )


@app.route("/sales/new", methods=["GET", "POST"])
def sale_new_form():
    if request.method == "POST":
        db = db_module.get_db()
        now = db_module.now_iso()
        order = _order_from_form(request.form)
        if order["order_number"]:
            dup = db.execute(
                "SELECT id FROM orders WHERE order_number = ?", (order["order_number"],)
            ).fetchone()
            if dup:
                flash(f"An order with number {order['order_number']} already exists.", "error")
                return redirect(url_for("sale_edit_form", order_id=dup["id"]))
        cur = db.execute(
            "INSERT INTO orders ("
            "order_number, order_date, buyer_location, tracking_number, product_weight_oz, "
            "value_of_products, shipping_charged, marketplace_fee, postage_cost, other_fees, "
            "status, source, notes, created_at, updated_at"
            ") VALUES ("
            ":order_number, :order_date, :buyer_location, :tracking_number, :product_weight_oz, "
            ":value_of_products, :shipping_charged, :marketplace_fee, :postage_cost, :other_fees, "
            "'manual', 'manual', :notes, :created_at, :updated_at)",
            {**order, "created_at": now, "updated_at": now},
        )
        order_id = cur.lastrowid
        _sync_line_items(db, order_id)
        db.commit()
        flash("Sale saved.", "success")
        return redirect(url_for("sale_edit_form", order_id=order_id))

    return render_template("sale_form.html", order=None, line_items=[])


@app.route("/sales/<int:order_id>", methods=["GET", "POST"])
def sale_edit_form(order_id: int):
    db = db_module.get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        flash("Sale not found.", "error")
        return redirect(url_for("sales_list"))

    if request.method == "POST":
        now = db_module.now_iso()
        updated = _order_from_form(request.form)
        db.execute(
            "UPDATE orders SET order_number=:order_number, order_date=:order_date, "
            "buyer_location=:buyer_location, tracking_number=:tracking_number, "
            "product_weight_oz=:product_weight_oz, value_of_products=:value_of_products, "
            "shipping_charged=:shipping_charged, marketplace_fee=:marketplace_fee, "
            "postage_cost=:postage_cost, other_fees=:other_fees, notes=:notes, "
            "updated_at=:updated_at WHERE id=:id",
            {**updated, "updated_at": now, "id": order_id},
        )
        _sync_line_items(db, order_id)
        db.commit()
        flash("Sale updated.", "success")
        return redirect(url_for("sale_edit_form", order_id=order_id))

    line_items = db.execute(
        "SELECT * FROM sale_line_items WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()
    return render_template("sale_form.html", order=order, line_items=line_items)


@app.post("/sales/<int:order_id>/delete")
def sale_delete(order_id: int):
    db = db_module.get_db()
    db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    db.commit()
    flash("Sale deleted.", "success")
    return redirect(url_for("sales_list"))


@app.post("/sales/<int:order_id>/mark-reviewed")
def sale_mark_reviewed(order_id: int):
    db = db_module.get_db()
    db.execute(
        "UPDATE orders SET status = 'reviewed', updated_at = ? WHERE id = ?",
        (db_module.now_iso(), order_id),
    )
    db.execute("UPDATE sale_line_items SET needs_review = 0 WHERE order_id = ?", (order_id,))
    db.commit()
    flash("Marked reviewed.", "success")
    return redirect(request.referrer or url_for("sales_list"))


# -----------------------------------------------------------------------------
# Expenses
# -----------------------------------------------------------------------------

@app.route("/expenses")
def expenses_list():
    db = db_module.get_db()
    year = _year_arg()
    category_id = request.args.get("category_id", "")

    clauses = []
    params: list[Any] = []
    if year:
        clauses.append("substr(e.expense_date, 1, 4) = ?")
        params.append(str(year))
    if category_id.isdigit():
        clauses.append("e.category_id = ?")
        params.append(int(category_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = db.execute(
        f"SELECT e.*, ec.name AS category_name FROM expenses e "
        f"JOIN expense_categories ec ON ec.id = e.category_id "
        f"{where} ORDER BY e.expense_date DESC, e.id DESC LIMIT 500",
        params,
    ).fetchall()
    categories = db.execute(
        "SELECT * FROM expense_categories WHERE is_archived = 0 ORDER BY sort_order, name"
    ).fetchall()
    return render_template(
        "expenses_list.html",
        expenses=rows,
        categories=categories,
        years=reports.available_years(db),
        selected_year=year,
        category_id=category_id,
        breakdown=reports.category_breakdown(db, year),
    )


def _expense_from_form(form) -> dict:
    return {
        "expense_date": form.get("expense_date", "").strip() or dt.date.today().isoformat(),
        "amount": _float_or_none(form.get("amount", "")) or 0,
        "category_id": int(form.get("category_id")) if form.get("category_id", "").isdigit() else None,
        "description": form.get("description", "").strip(),
        "vendor": form.get("vendor", "").strip() or None,
        "receipt_note": form.get("receipt_note", "").strip() or None,
    }


@app.route("/expenses/new", methods=["GET", "POST"])
def expense_new_form():
    db = db_module.get_db()
    if request.method == "POST":
        data = _expense_from_form(request.form)
        if not data["category_id"] or not data["description"]:
            flash("Category and description are required.", "error")
        else:
            now = db_module.now_iso()
            receipt_file = _save_receipt_upload()
            db.execute(
                "INSERT INTO expenses (expense_date, amount, category_id, description, vendor, "
                "receipt_note, receipt_file, source, created_at, updated_at) VALUES "
                "(:expense_date, :amount, :category_id, :description, :vendor, :receipt_note, "
                ":receipt_file, 'manual', :created_at, :updated_at)",
                {**data, "receipt_file": receipt_file, "created_at": now, "updated_at": now},
            )
            db.commit()
            flash("Expense saved.", "success")
            return redirect(url_for("expenses_list"))

    categories = db.execute(
        "SELECT * FROM expense_categories WHERE is_archived = 0 ORDER BY sort_order, name"
    ).fetchall()
    return render_template("expense_form.html", expense=None, categories=categories)


@app.route("/expenses/<int:expense_id>", methods=["GET", "POST"])
def expense_edit_form(expense_id: int):
    db = db_module.get_db()
    expense = db.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if expense is None:
        flash("Expense not found.", "error")
        return redirect(url_for("expenses_list"))

    if request.method == "POST":
        data = _expense_from_form(request.form)
        if not data["category_id"] or not data["description"]:
            flash("Category and description are required.", "error")
        else:
            receipt_file = _save_receipt_upload() or expense["receipt_file"]
            db.execute(
                "UPDATE expenses SET expense_date=:expense_date, amount=:amount, "
                "category_id=:category_id, description=:description, vendor=:vendor, "
                "receipt_note=:receipt_note, receipt_file=:receipt_file, updated_at=:updated_at "
                "WHERE id=:id",
                {**data, "receipt_file": receipt_file, "updated_at": db_module.now_iso(), "id": expense_id},
            )
            db.commit()
            flash("Expense updated.", "success")
            return redirect(url_for("expenses_list"))

    categories = db.execute(
        "SELECT * FROM expense_categories WHERE is_archived = 0 ORDER BY sort_order, name"
    ).fetchall()
    return render_template("expense_form.html", expense=expense, categories=categories)


@app.post("/expenses/<int:expense_id>/delete")
def expense_delete(expense_id: int):
    db = db_module.get_db()
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("expenses_list"))


# -----------------------------------------------------------------------------
# Import
# -----------------------------------------------------------------------------

@app.route("/import", methods=["GET", "POST"])
def import_form():
    db = db_module.get_db()
    if request.method == "POST":
        uploaded = request.files.get("orders_file")
        if not uploaded or uploaded.filename == "":
            flash("Choose a CSV file to upload.", "error")
            return redirect(url_for("import_form"))

        upload_dir = config.UPLOAD_DIR
        upload_dir.mkdir(parents=True, exist_ok=True)
        filename = secure_filename(uploaded.filename)
        path = upload_dir / filename
        uploaded.save(path)

        try:
            counts = importer.import_orders_csv(db, path, filename)
        except Exception as exc:  # noqa: BLE001 -- surface to the user, never 500 on a bad export
            flash(f"Import failed: {exc}", "error")
            return redirect(url_for("import_form"))

        flash(
            f"Imported {counts['rows_seen']} row(s): {counts['orders_created']} order(s) created, "
            f"{counts['orders_skipped_duplicate']} duplicate(s) skipped, "
            f"{counts['rows_needing_review']} needing review.",
            "success",
        )
        return redirect(url_for("sales_list", status="needs_review"))

    import_logs = db.execute(
        "SELECT * FROM import_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    return render_template("import.html", import_logs=import_logs)


# -----------------------------------------------------------------------------
# CSV export
# -----------------------------------------------------------------------------

def _csv_response(filename: str, header: list[str], rows: list[list[Any]]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/export/sales.csv")
def export_sales_csv():
    db = db_module.get_db()
    year = _year_arg()
    year_clause = "WHERE substr(o.order_date, 1, 4) = ?" if year else ""
    params = [str(year)] if year else []
    rows = db.execute(
        f"SELECT o.order_date, o.order_number, li.product_name, li.set_name, li.card_number, "
        f"li.quantity, li.sale_price_total, li.cogs_total, o.shipping_charged, o.marketplace_fee, "
        f"o.postage_cost, o.other_fees, o.status "
        f"FROM sale_line_items li JOIN orders o ON o.id = li.order_id "
        f"{year_clause} ORDER BY o.order_date, o.id",
        params,
    ).fetchall()
    header = [
        "order_date", "order_number", "product_name", "set_name", "card_number", "quantity",
        "sale_price_total", "cogs_total", "shipping_charged", "marketplace_fee", "postage_cost",
        "other_fees", "status",
    ]
    return _csv_response("sales.csv", header, [list(r) for r in rows])


@app.get("/export/expenses.csv")
def export_expenses_csv():
    db = db_module.get_db()
    year = _year_arg()
    year_clause = "WHERE substr(e.expense_date, 1, 4) = ?" if year else ""
    params = [str(year)] if year else []
    rows = db.execute(
        f"SELECT e.expense_date, ec.name AS category, e.amount, e.description, e.vendor, e.receipt_note "
        f"FROM expenses e JOIN expense_categories ec ON ec.id = e.category_id "
        f"{year_clause} ORDER BY e.expense_date, e.id",
        params,
    ).fetchall()
    header = ["expense_date", "category", "amount", "description", "vendor", "receipt_note"]
    return _csv_response("expenses.csv", header, [list(r) for r in rows])


@app.get("/export/summary.csv")
def export_summary_csv():
    db = db_module.get_db()
    year = _year_arg()
    pnl = reports.pnl_for_year(db, year)
    rows = [
        ["Revenue (sales + shipping charged)", round(pnl["revenue"], 2)],
        ["Cost of Goods Sold", round(pnl["cogs"], 2)],
        ["Marketplace Fees", round(pnl["marketplace_fee"], 2)],
        ["Postage", round(pnl["postage"], 2)],
        ["Other Expenses", round(pnl["other_expenses"], 2)],
        ["Net Profit", round(pnl["net_profit"], 2)],
    ]
    return _csv_response(f"summary_{year or 'all'}.csv", ["category", "amount"], rows)


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = db_module.get_db()
    if request.method == "POST":
        name = request.form.get("category_name", "").strip()
        if name:
            db.execute(
                "INSERT OR IGNORE INTO expense_categories (name, sort_order, created_at) "
                "VALUES (?, 999, ?)",
                (name, db_module.now_iso()),
            )
            db.commit()
            flash(f"Added category '{name}'.", "success")
        return redirect(url_for("settings"))

    categories = db.execute(
        "SELECT * FROM expense_categories ORDER BY is_archived, sort_order, name"
    ).fetchall()
    import_logs = db.execute("SELECT * FROM import_log ORDER BY id DESC LIMIT 20").fetchall()
    return render_template(
        "settings.html", categories=categories, import_logs=import_logs, auth_enabled=AUTH_ENABLED
    )


@app.post("/settings/categories/<int:category_id>/archive")
def archive_category(category_id: int):
    db = db_module.get_db()
    db.execute("UPDATE expense_categories SET is_archived = 1 WHERE id = ?", (category_id,))
    db.commit()
    flash("Category archived.", "success")
    return redirect(url_for("settings"))


@app.post("/settings/categories/<int:category_id>/unarchive")
def unarchive_category(category_id: int):
    db = db_module.get_db()
    db.execute("UPDATE expense_categories SET is_archived = 0 WHERE id = ?", (category_id,))
    db.commit()
    flash("Category restored.", "success")
    return redirect(url_for("settings"))


_bootstrap_db()


if __name__ == "__main__":
    app.run(debug=True)
