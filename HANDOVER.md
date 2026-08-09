# Puddle — Handover for a Claude Code session

You're being handed a small toy project called **Puddle**: a one-store
e-commerce demo where *every tier runs on DuckDB* — transactions, a clickstream
lake, downstream analytics, extension-powered extras, a DuckLake catalog, and a
browser-side catalog search in DuckDB-WASM. The human reports **"it's not
working"** but hasn't pinned down which part. Your job: figure out which
command fails, fix it, and get the whole thing running.

**First move:** don't rebuild from scratch. Ask the human (or check their
scrollback) for the *exact command that failed and its error output*, then jump
to the matching section under "Troubleshooting by symptom." If you can't get
that, run the verification sequence below top-to-bottom and stop at the first
mismatch.

---

## What was and wasn't verified before handover

This project was assembled and tested in a sandbox that **could not download
DuckDB extensions** (no network to `extensions.duckdb.org` /
`community-extensions.duckdb.org`) and **could not run a backgrounded socket
server**. So:

| Surface | Status |
|---|---|
| `01_seed.sql` → `04_marts.sql` (core) | ✅ Fully run via `./run.sh` on DuckDB CLI **v1.5.5**. Outputs below are known-good. |
| `02_oltp.sql` transaction + oversell rollback | ✅ Verified. |
| `collector.py` **data path** (enqueue→flush→Parquet→read) | ✅ Verified by importing the module and calling `enqueue`/`flush` directly. |
| `collector.py` **live HTTP server** | ⚠️ **Not run.** The server is guarded under `__main__`; only the collection logic was exercised. |
| `public/` WASM search | ⚠️ Syntax-checked (`node --check`) and the catalog schema matches, but **not opened in a real browser**. |
| `05_extras.sql` — `fts` block | ✅ Confirmed working by the human. |
| `05_extras.sql` — `vss` block | ⚠️ Two known DuckDB limitations already patched (see below); **not executed**. |
| `05_extras.sql` — `spatial`, DuckPGQ blocks | ❌ **Unverified.** Syntax written against docs. |
| `06_ducklake.sql` | ❌ **Unverified end-to-end.** Written against DuckLake docs; API names may drift by version. |

**The unverified rows are the most likely source of "it's not working."** Weight
your suspicion accordingly.

**Update since this handover was written:** the clickstream lake (`03_lake.sql`,
`collector.py`) was migrated from a bare Parquet directory to a real DuckLake
catalog (`puddle_catalog.ducklake` + `lakehouse_data/`) — this is no longer an
optional/isolated tier, the extension is now required for the *core* pipeline
(`run.sh`), by explicit choice, trading away the original "no network needed"
guarantee below. `public/`, `collector.py`'s live server, real checkout,
recommendations, and `public/dashboard.html` were all built and verified in a
real browser during that later work — the rows above describing them as
unverified are stale.

---

## Environment requirements

- **DuckDB CLI** on `PATH` (built/tested on **v1.5.5**). `duckdb --version`.
  If missing: download the CLI from the DuckDB GitHub releases page for your OS,
  or `brew install duckdb`. Older CLIs (< 1.1) will choke on some syntax
  (`QUALIFY`, list literals, `FROM GRAPH_TABLE`); prefer ≥ 1.4.
- **Python 3.9+** with the `duckdb` module for `collector.py`:
  `pip install duckdb` (tested with duckdb **1.5.5**).
- **Network access to the extension hosts.** `run.sh` now installs the
  `ducklake` extension as part of the core pipeline (cached after the first
  run); `05_extras.sql` and `06_ducklake.sql` need network for their own
  extensions too.
- A browser + a static server for the WASM demo (details in its section).

---

## Run order + expected output (use as a verification checklist)

```bash
./run.sh          # DUCKDB=/path/to/duckdb ./run.sh  if duckdb isn't on PATH
```

`run.sh` deletes `oltp.duckdb`/`marts.duckdb`/`puddle_catalog.ducklake`, wipes
`lakehouse_data/`, then runs `01→03` against `oltp.duckdb`, exports
`public/catalog.parquet`, and runs `04` against `marts.duckdb`. Expected
checkpoints:

- **01 seed:** `users 500`, `products 60`, `orders 400`, `order_items ~600`.
- **02 oltp:** a new order **id 401**; product 3 stock decremented by 2.
- **03 lake:** `events 5343`, `pageviews 3920`, `carts 1067`, `purchases 356`,
  **31** day partitions, landed as three DuckLake snapshots (one per
  chronological wave) instead of a Parquet directory.
- **04 marts — funnel:** `sessions 3200`, `view_to_cart_pct 33.3`,
  `cart_to_purchase_pct 33.4`.
- **04 marts — revenue_by_category:** **all four** of `bath / collectible /
  decor / outdoor` present with non-zero `revenue_eur`. (If `decor` is missing
  or zero, a fix regressed — see "Don't regress.")
- **04 marts — retention:** `weeks_since` 0..5 with `active_users`
  **200, 200, 180, 160, 139, 44** (a decay, not a flat line).

If all of that matches, the **core is fine** and "not working" is one of the
optional tiers below.

---

## The optional tiers

### Live event tracking — `collector.py` + `public/tracker.js`

```bash
pip install duckdb
python3 collector.py        # serves storefront + collects, http://localhost:8787/
```

Open the URL, search, click **Add to cart** (a toast says "event sent"). The
collector inserts events into the DuckLake catalog's `events` table, in the
**same schema** the marts read. Then (after stopping the collector — see
below):

```bash
duckdb marts.duckdb -f 04_marts.sql   # funnel/retention now include your clicks
```

Design notes for debugging:
- The collector **serves the storefront itself** so the tracker posts
  same-origin — this avoids CORS on `sendBeacon`. Don't "helpfully" split them
  onto two origins without adding CORS.
- It's a **single writer** for real now, not just by convention: `collector.py`
  holds one shared connection with **both** `oltp.duckdb` and the DuckLake
  catalog attached (orders, recommendations, the dashboard, and event flushes
  all go through it). A local-file DuckLake catalog is itself just a DuckDB
  file, so only one process can have it attached at a time — `04_marts.sql`,
  `06_ducklake.sql`, or an ad-hoc `duckdb` shell will all fail with a lock
  conflict while the collector is running. Stop it first. Paths are resolved
  relative to the script for `oltp.duckdb`, but the DuckLake catalog path must
  stay **relative** (`puddle_catalog.ducklake`) to match what `03_lake.sql`
  created it with — so **run it from the repo root**.
- Events buffer and flush on size (`BATCH=20`) or every `FLUSH_SECS=5`, then
  a background thread calls `ducklake_merge_adjacent_files` every
  `COMPACT_SECS=60` to keep that from fragmenting into lots of tiny files.
  For a quick test, drop the batch settings or hit Ctrl-C (it flushes on exit).

### `05_extras.sql` — fts / vss / spatial / DuckPGQ (needs network)

```bash
duckdb oltp.duckdb -f 05_extras.sql
```

- **`fts`**: confirmed working (BM25 over name+description).
- **`vss`**: already patched for two real DuckDB limitations (see "Don't
  regress"). Uses toy 4-dim vectors in a **separate `product_vectors` table**.
- **`spatial`**: nearest-warehouse query — `ST_Distance` on lon/lat is planar
  (fine for a toy). If a function name errors, check the installed `spatial`
  version's function list.
- **DuckPGQ** (community extension, `INSTALL duckpgq FROM community`): builds a
  co-purchase `PROPERTY GRAPH` and traverses it with `FROM GRAPH_TABLE (... MATCH
  ...)`. **This is the least-verified SQL block.** If it errors, the likely
  culprits are the `CREATE PROPERTY GRAPH` label/key syntax or the `GRAPH_TABLE`
  `MATCH`/`COLUMNS` clause for the installed DuckPGQ version — reconcile against
  that version's README.

### `06_ducklake.sql` — explore the live DuckLake catalog: snapshots + time travel

```bash
duckdb -f 06_ducklake.sql   # stop collector.py first if it's running
```

**Verified working**, including against a catalog with real live-traffic
snapshots on top of the seed data (not just fresh out of `run.sh`). Since
`03_lake.sql` now lands the seed directly into the DuckLake catalog (three
chronological waves = three real snapshots, snapshot versions **2, 3, 4** —
version 1 is the empty table right after creation), this script no longer
ingests anything itself — it just explores what's already there via
`ducklake_snapshots('lakehouse')`, `events AT (VERSION => n)`, and
`ducklake_table_changes('lakehouse', 'main', 'events', 1, 4)`. If you've run
`collector.py` since seeding, expect a higher max snapshot than the script's
comments assume — that's expected, not a bug.

### `public/` — DuckDB-WASM catalog search

**Must be served over HTTP** — opening `index.html` as `file://` will fail
because the browser blocks `fetch('./catalog.parquet')` and `localStorage`.

```bash
python3 collector.py                 # http://localhost:8787/ — serves storefront + tracking
```

Serve it via `collector.py`, not a plain static server: `data-collector="/collect"` in
`index.html` is a relative path, and `python3 -m http.server` has no `POST` handler
(returns `501 Unsupported method`), so "Add to cart" events silently drop.

- Loads `@duckdb/duckdb-wasm@1.29.0` from the jsDelivr CDN (needs internet). If
  that exact version 404s, bump the pin in `index.html`.
- Registers `catalog.parquet` as a buffer and `CREATE TABLE catalog AS SELECT *
  FROM read_parquet(...)`. Every keystroke/facet/sort runs a real SQL query; the
  bottom console shows the SQL + ms latency.
- No COOP/COEP headers are set, so it runs the **non-threaded** WASM bundle —
  that's expected and fine.

---

## Don't regress these (bugs already fixed)

Each of these was a real failure that's now patched. If you rewrite a file,
preserve the fix:

1. **`01_seed.sql` — status decorrelation.** Order `status` is assigned via
   `hash(id) % 5`, **not** `id % 4`. Using `id % 4` aligns status with the
   `product_id % 4` category formula so **every `decor` order becomes
   `cancelled`** and the `decor` revenue row vanishes. Keep the hash.
2. **`01_seed.sql` — sequence.** `CREATE SEQUENCE order_seq START 401;` (there is
   no `setval` in DuckDB). Seeded orders use explicit ids 1..400; live orders
   take 401+.
3. **`01_seed.sql` re-run.** `CREATE OR REPLACE TABLE users` **fails** if
   `orders`/`order_items` already exist (FK dependency). `run.sh` deletes the DB
   first. If you must re-seed manually, delete `oltp.duckdb` or drop tables in
   dependency order (order_items → orders → inventory → products → users).
4. **`03_lake.sql` — retention churn.** `_interest` has a `WHERE (user_id % 10) <
   (10 - week_idx)` clause so the retention cohort **decays** instead of being
   flat/empty. Retention is cohorted on **first-activity week**, not signup week
   (signups are in Jan, activity in March — signup cohorts would be empty for
   weeks 0–3).
5. **`05_extras.sql` — vss.** (a) Vectors live in a **separate
   `product_vectors` table**, never a column on `products`: DuckDB refuses to
   `UPDATE` a table whose key is referenced by a foreign key (`inventory`,
   `order_items` reference `products`). (b) `SET
   hnsw_enable_experimental_persistence = true;` is required before `CREATE INDEX
   ... USING HNSW` on a **file** database.

---

## How to report back

When you've localized it, tell the human: which command failed, the exact
error, the one-line fix, and whether any "Don't regress" invariant was
involved. If the failure is in `05`/`06` due to an extension version, note the
installed extension version and the specific syntax that changed.

## File map

```
run.sh            orchestrator (resets + builds core, exports catalog)
01_seed.sql       OLTP schema, constraints, synthetic data
02_oltp.sql       place-order transaction + oversell/rollback demo
03_lake.sql       synthesizes a month of clickstream -> DuckLake catalog (needs network)
04_marts.sql      sessions, funnel, retention, revenue (ATTACH oltp + DuckLake catalog)
05_extras.sql     fts / vss / spatial / DuckPGQ            (needs network)
06_ducklake.sql   explore the live catalog: snapshots + time travel (needs network)
collector.py      single-writer collector: storefront + events + real checkout + live stats
public/index.html DuckDB-WASM catalog search UI
public/tracker.js browser tracker (pageview / add_to_cart)
public/catalog.parquet  pre-exported catalog for the WASM demo
README.md         narrative overview
```
