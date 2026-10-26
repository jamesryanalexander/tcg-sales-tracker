> **Disclosure:** This is a personal app for tracking my own TCGPlayer card sales for tax
> record-keeping. It was built with the help of Claude Code; all AI-generated components have
> been manually reviewed and tested to ensure stability and security. All human created and
> guided code is released under a Creative Commons CC-0 Dedication to the extent necessary.
> **This app does not provide tax advice** -- verify totals with your accountant before filing.

# TCG Sales Tracker

A local-first Flask app that tracks sales of cards sold on TCGPlayer (primarily) for tax
purposes: Cost of Goods Sold (COGS), sale price, shipping charged to buyers, marketplace fees,
postage, and general selling expenses (supplies, subscriptions, etc.), rolling up into a
profit/loss report by year.

Sibling apps: [`collection-tracker`](../collection-tracker) and
[`basic-land-tracker`](../basic-land-tracker). Same stack and visual language, different accent
color to tell them apart.

## v1 scope

- **CSV import only.** No TCGPlayer credentials are stored and nothing logs into TCGPlayer on
  your behalf. You export your order history from the
  [TCGPlayer Seller Portal](https://seller.tcgplayer.com/) (Orders -> select orders -> Export
  Shipping) and upload the CSV on the **Import** page.
- **COGS is entered manually** per sale -- this app is standalone and does not read from
  `collection-tracker`'s database.
- **Sales + expense ledger only.** Tracking *current* TCGPlayer listings/inventory (what's for
  sale right now, as opposed to what's already sold) is an intentional phase 2 -- see "Future
  work" below.

TCGPlayer's exact export columns vary and per-order marketplace fees often aren't included at
all (they usually show up only in a separate seller payout/statement report). The importer is
tolerant of this: whatever column it can't find is left blank and that order is flagged
**needs review** so you can fill in the gap by hand on the sale's edit page. Re-uploading the
same export is always safe -- orders are matched and deduplicated by order number.

## Local dev

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug
```

Open http://127.0.0.1:5000. The SQLite DB auto-creates at `data/tcg_sales_tracker.sqlite3`.

### Try it without a real TCGPlayer export

```bash
flask --app app seed-fixture
```

Loads `tests/fixtures/sample_orders_export.csv` through the same importer a real upload uses.
See `tests/fixtures/sample_orders_export.README.md` for the expected counts and a
hand-calculated profit/loss total to sanity-check the dashboard against.

## CLI commands

- `flask --app app init-db` -- create the SQLite schema and seed default expense categories.
- `flask --app app seed-fixture [path]` -- import a sample (or real) orders CSV.
- `flask --app app hash-password '<password>'` -- generate a hash for `AUTH_PASSWORD_HASH`.

## Config

- `.env` (gitignored, copy from `.env.example`) -- used by the Docker deployment.
- `MAX_UPLOAD_MB` caps CSV/receipt upload size (default 20 MB -- orders CSVs are tiny; this
  mostly matters for receipt image uploads).

## Deployment

Runs behind the same shared reverse proxy as the sibling apps (see
[`edge-caddy`](../edge-caddy)) -- this app's own `docker-compose.yml` publishes no host ports,
only `expose`s port 5000 internally on the shared `edge` Docker network.

```bash
cp .env.example .env
# fill in real AUTH_USERNAME/AUTH_PASSWORD_HASH/SECRET_KEY -- strongly recommended here, since
# this app stores real sales and expense records used for tax filing, not just collectible
# values like the sibling apps
docker network inspect edge >/dev/null 2>&1 || docker network create edge
docker compose up -d --build
```

Then add a site block in `edge-caddy/Caddyfile` pointing at `tcg-sales-tracker-app:5000` and
`docker compose up -d` there to pick it up -- see `edge-caddy/README.md` for the full runbook.

`--workers 1` in the Dockerfile matches the sibling apps' convention. There's no background
scheduler in v1 (nothing runs automatically -- see below), so it isn't load-bearing yet, but is
kept now to avoid a prod config change if/when phase 2 adds one.

## Future work (not built yet)

- **Automatic TCGPlayer sync**: if/when official TCGPlayer Seller API access is available, a
  `tcgplayer.py` client (modeled on `collection-tracker/echomtg.py`) and a `scheduler.py`
  (modeled on `collection-tracker/scheduler.py`) could feed the exact same
  `importer.upsert_order()` function the CSV path uses today -- no schema changes needed.
  `import_log.import_type` already has room for an `'api_sync'` value alongside `'orders_csv'`.
- **Current inventory/listings tracking**: a `listings` table (what's for sale right now,
  analogous to `collection-tracker`'s `inventory_items`), with a nullable FK added to
  `sale_line_items` later via the same `PRAGMA table_info` + `ALTER TABLE` pattern
  `basic-land-tracker` uses for its `origin`/`group_key` columns.
