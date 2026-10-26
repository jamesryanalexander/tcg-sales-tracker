from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional


def available_years(db: sqlite3.Connection) -> List[int]:
    """Every calendar year that has at least one order or expense, newest
    first -- drives the year filter dropdown on the dashboard/sales/expenses
    pages."""
    rows = db.execute(
        "SELECT DISTINCT substr(order_date, 1, 4) AS y FROM orders "
        "UNION SELECT DISTINCT substr(expense_date, 1, 4) AS y FROM expenses"
    ).fetchall()
    years = sorted({int(r["y"]) for r in rows if r["y"]}, reverse=True)
    return years


def pnl_for_year(db: sqlite3.Connection, year: Optional[int]) -> Dict[str, Any]:
    """Profit/loss summary for one tax year (or all time if year is None).

    revenue        = card sale price (line items) + shipping charged to buyers
    cogs           = what you paid for the cards you sold
    marketplace_fee = TCGPlayer's per-order fee + any 'Marketplace Fees' expenses
    postage        = per-order postage you paid + any 'Postage' expenses
    other_expenses = every other expense category, plus per-order 'other_fees'
    net_profit     = revenue - cogs - marketplace_fee - postage - other_expenses
    """
    year_clause_orders = "substr(order_date, 1, 4) = ?" if year else "1=1"
    year_clause_expenses = "substr(expense_date, 1, 4) = ?" if year else "1=1"
    order_params = [str(year)] if year else []
    expense_params = [str(year)] if year else []

    order_totals = db.execute(
        f"SELECT COALESCE(SUM(shipping_charged), 0) AS shipping_charged, "
        f"COALESCE(SUM(marketplace_fee), 0) AS marketplace_fee, "
        f"COALESCE(SUM(postage_cost), 0) AS postage_cost, "
        f"COALESCE(SUM(other_fees), 0) AS other_fees, "
        f"COUNT(*) AS order_count "
        f"FROM orders WHERE {year_clause_orders}",
        order_params,
    ).fetchone()

    line_item_totals = db.execute(
        f"SELECT COALESCE(SUM(li.sale_price_total), 0) AS sale_price_total, "
        f"COALESCE(SUM(li.cogs_total), 0) AS cogs_total, "
        f"SUM(CASE WHEN li.needs_review = 1 THEN 1 ELSE 0 END) AS lines_needing_review "
        f"FROM sale_line_items li JOIN orders o ON o.id = li.order_id "
        f"WHERE {year_clause_orders.replace('order_date', 'o.order_date') if year else '1=1'}",
        order_params,
    ).fetchone()

    expense_rows = db.execute(
        f"SELECT ec.name AS category, COALESCE(SUM(e.amount), 0) AS total "
        f"FROM expenses e JOIN expense_categories ec ON ec.id = e.category_id "
        f"WHERE {year_clause_expenses} GROUP BY ec.name",
        expense_params,
    ).fetchall()
    expense_by_category = {row["category"]: row["total"] for row in expense_rows}

    marketplace_fee_expenses = expense_by_category.pop("Marketplace Fees", 0.0)
    postage_expenses = expense_by_category.pop("Postage", 0.0)
    other_expenses_total = sum(expense_by_category.values())

    revenue = line_item_totals["sale_price_total"] + order_totals["shipping_charged"]
    cogs = line_item_totals["cogs_total"]
    marketplace_fee = order_totals["marketplace_fee"] + marketplace_fee_expenses
    postage = order_totals["postage_cost"] + postage_expenses
    other_expenses = other_expenses_total + order_totals["other_fees"]
    net_profit = revenue - cogs - marketplace_fee - postage - other_expenses

    return {
        "year": year,
        "revenue": revenue,
        "cogs": cogs,
        "marketplace_fee": marketplace_fee,
        "postage": postage,
        "other_expenses": other_expenses,
        "net_profit": net_profit,
        "order_count": order_totals["order_count"],
        "lines_needing_review": line_item_totals["lines_needing_review"] or 0,
        "expense_by_category": dict(sorted(expense_by_category.items())),
    }


def category_breakdown(db: sqlite3.Connection, year: Optional[int]) -> List[Dict[str, Any]]:
    """Per-category expense totals for a year, including categories with no
    rows yet (so /settings and the expense list always show the full set)."""
    year_clause = "substr(e.expense_date, 1, 4) = ?" if year else "1=1"
    params = [str(year)] if year else []
    rows = db.execute(
        f"SELECT ec.id, ec.name, COALESCE(SUM(e.amount), 0) AS total, COUNT(e.id) AS count "
        f"FROM expense_categories ec "
        f"LEFT JOIN expenses e ON e.category_id = ec.id AND {year_clause} "
        f"WHERE ec.is_archived = 0 "
        f"GROUP BY ec.id, ec.name ORDER BY ec.sort_order, ec.name",
        params,
    ).fetchall()
    return [dict(row) for row in rows]
