# 🦆 Puddle — one duck shop, every tier on DuckDB

A toy e-commerce platform where **DuckDB is the database for everything**: the
transaction that sells a duck, the clickstream that recorded the visit, the
analytics that chart it, and the catalog search running in the customer's
browser. One engine, wired together with `ATTACH` so a single query can span
tiers, plus a DuckLake catalog (Parquet + real snapshots) as the append-only lake.

The point is partly a joke on DuckDB itself: it's an embedded *OLAP* engine, so
the transactional tier is the part that *shouldn't* work — which makes it the
most interesting corner to build, and the one seam left deliberately visible.

```
oltp.duckdb        transactions: users, products, inventory, orders  (single writer)
tracker.js         browser tracker: fires pageview/add_to_cart events
collector.py       single-writer collector: storefront + events + real checkout + live stats
puddle_catalog.ducklake + lakehouse_data/   clickstream lake (DuckLake catalog, append-only truth)
06_ducklake.sql    explores the live catalog                         (snapshots + time travel)
marts.duckdb       downstream analytics: sessions, funnel, retention  (attaches oltp + the lake)
public/index.html  DuckDB-WASM catalog search + real cart/checkout    (recs computed live via vss)
public/dashboard.html   funnel/revenue/retention, live from oltp.duckdb + the lake, no rebuild
```

## Run it

```bash
./run.sh                      # builds oltp.duckdb, marts.duckdb, the DuckLake catalog, public/catalog.parquet
```

Needs the `duckdb` CLI on your PATH (or `DUCKDB=/path/to/duckdb ./run.sh`).
The clickstream lake is a real DuckLake catalog, so `run.sh` needs network the
first time to install the `ducklake` extension (cached after that) — that's
now a core dependency, not just an optional tier.

Then the optional layers:

```bash
# Live event tracking — serves the storefront AND collects real events:
python3 collector.py                           # http://localhost:8787/  (needs duckdb, pip install duckdb)
#   → open it, search, click "Add to cart"; each click POSTs an event that the
#     collector inserts into the DuckLake catalog. Stop the collector, then
#     re-run 04_marts.sql and the live events show up in the funnel and
#     retention alongside the synthetic ones (only one process can have the
#     catalog attached at a time).

duckdb -f 06_ducklake.sql                       # explore the live catalog: snapshots + time travel (needs network)
duckdb oltp.duckdb -f 05_extras.sql             # fts / vss / spatial / DuckPGQ   (needs network)
```

## The tiers

**`01_seed.sql` · `02_oltp.sql` — transactional (the "wrong tool" corner).**
Real `PRIMARY KEY` / `REFERENCES` / `CHECK` constraints and a `SEQUENCE`. The
place-order flow is a genuine `BEGIN … COMMIT`: allocate an order, decrement
inventory, write line items, all-or-nothing. `CHECK (stock >= 0)` does the job a
pessimistic row lock would do elsewhere — try to oversell and the whole
transaction rolls back. Because DuckDB is single-writer, the honest way to run
an app on it is to funnel every write through **one serializing process** holding
the sole write connection; readers use separate read-only connections.

**`03_lake.sql` — web analytics.** A tracker's `pageview` / `add_to_cart` /
`purchase` events land directly in a **DuckLake catalog** (not a bare Parquet
directory), with a semi-structured JSON `props` column so the schema can
evolve without a migration. Every commit is a snapshot, giving versioning,
`AT (VERSION => n)` time travel, and a change feed for free — `03_lake.sql`
*synthesizes* a month of events in three chronological waves, so there are
three real snapshots to explore right away; the **live path** is `tracker.js`
+ `collector.py`.

**Event tracking (the live path).** `tracker.js` runs in the storefront: it
keeps a durable anonymous `visitor_id` and a 30-minute `session_id`, fires a
pageview on load, and sends an `add_to_cart` event when you click a card —
using `navigator.sendBeacon` so events survive page unloads. `collector.py` is
the receiving end and, like the OLTP tier, is a **single writer**: one shared
connection holding both `oltp.duckdb` *and* the DuckLake catalog attached
(orders, recommendations, the dashboard, and event flushes all funnel through
it — a local-file DuckLake catalog is itself just a DuckDB file, so only one
connection anywhere can have it attached). It serves the storefront (so
events post same-origin), batches events, and `INSERT`s each batch straight
into the DuckLake catalog in the *same schema the marts read* — live clicks
flow into the funnel and retention next time you build the marts. A
background thread calls `ducklake_merge_adjacent_files` every 60s so the
small-file fragmentation that batching used to hand-roll around is handled by
DuckLake's own documented maintenance operation instead.

**DuckLake (`06_ducklake.sql`).** Since the lake *is* the catalog now, this
script no longer ingests anything — it just explores what's already there:
`ducklake_snapshots`, `AT (VERSION => n)` / `AT (TIMESTAMP => …)` time travel,
and a change feed via `ducklake_table_changes`, replaying the funnel as of an
earlier snapshot. Trade-off worth knowing: this only works because everything
shares one local-file catalog — a second process (this script, an ad-hoc
query, `collector.py`) can't attach concurrently while another holds it open,
same limitation as `oltp.duckdb`. Real concurrent multi-process access would
need Postgres (or similar) as the catalog backend instead of a local file.

**`04_marts.sql` — downstream analytics (home turf).** `ATTACH`es the OLTP store
read-only and reads the lake in the same queries. Sessionization via window
functions (30-min inactivity gap ⇒ new session), a session-level view→cart→
purchase funnel, retention cohorts (a cross-database join of lake activity to
OLTP signup + country), and a revenue rollup.

**`05_extras.sql` — the fun tier, all just extensions.** BM25 full-text search
(`fts`), HNSW vector similarity (`vss`), a co-purchase graph you traverse with a
real `MATCH` (DuckPGQ), nearest-warehouse fulfilment (`spatial`), a live remote
price-feed join (`httpfs`), and time-travel notes for DuckLake.

**`public/` — client-side catalog search (DuckDB-WASM).** Ships the catalog as
Parquet and runs DuckDB *in the browser*: every keystroke, facet, and sort is a
real SQL query with zero round-trips. The console strip at the bottom shows the
generated SQL and its sub-millisecond execution time — the whole thesis, made
visible. Reads and personalization can live in the browser; **truth stays on the
server** (anything on the write path — checkout, inventory — is the OLTP tier).

## Notes on the synthetic data

Deterministic (`setseed`) so every run is identical. ~500 users, 60 products,
400 seeded historical orders + 1 live transaction, ~6,500 clickstream events
across 31 daily partitions. The funnel is engineered to ~33% view→cart and
~33% cart→purchase; retention has gentle built-in churn so the cohort triangle
actually decays instead of sitting flat.

## Upgrade paths

- Swap the browser's `ILIKE` search for `fts` loaded in WASM (BM25 in-browser).
- Back the DuckLake catalog with Postgres instead of a local file, for real
  concurrent multi-process reads/writes (today, only one process can have it
  attached at a time — see the DuckLake tradeoff above).
- Replace the toy `vss` vectors with real embeddings for semantic recommendations,
  then fuse them with the DuckPGQ co-purchase graph for a hybrid recommender.
