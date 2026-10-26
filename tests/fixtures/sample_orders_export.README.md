# sample_orders_export.csv -- expected results

5 rows, modeled on a TCGPlayer "Export Shipping" orders CSV:

1. `ORD1001` -- complete row (has fee, postage, shipping charged).
2. `ORD1002` -- missing `Marketplace Fee` (blank cell) -> should land as `needs_review`.
3. Exact duplicate of row 1 (`ORD1001` again) -> should be skipped as a duplicate, not create a
   second order.
4. `ORD1003` -- another complete 2026 order.
5. `ORD1004` -- a 2025 order, to prove year filtering works.

## After `flask --app app seed-fixture`

- `rows_seen = 5`
- `orders_created = 4` (ORD1001, ORD1002, ORD1003, ORD1004 -- row 3 doesn't count)
- `orders_skipped_duplicate = 1`
- `rows_needing_review = 1` (ORD1002 only)

Each created order also gets one placeholder `sale_line_items` row with `sale_price_total` equal
to that order's `Value of Products` and `cogs_total = NULL` (nothing's been sold for a tracked
cost yet, since the CSV has no per-card breakdown -- see `importer.create_placeholder_line_item`).

## Dashboard P&L for year 2026 (ORD1001 + ORD1002 + ORD1003)

| Line              | Calculation                  | Amount  |
|-------------------|-------------------------------|---------|
| Revenue           | (25+10+5) + (4.99+3.99+2.99) | $51.97  |
| COGS              | 0 (no COGS entered yet)      | $0.00   |
| Marketplace Fees  | 1.50 + 0.75 (ORD1002's is NULL, ignored) | $2.25   |
| Postage           | 3.20 + 2.80 + 2.00           | $8.00   |
| Other Expenses    | 0                             | $0.00   |
| **Net Profit**    |                               | **$41.72** |

Year 2025 (ORD1004 only): revenue = 40 + 5.99 = $45.99, fee = $2.40, postage = $4.50, net = $39.09.
