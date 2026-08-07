# 🦆 Puddle — one duck shop, every tier on DuckDB

A toy e-commerce platform where **DuckDB is the database for everything**: the
transaction that sells a duck, the clickstream that recorded the visit, the
analytics that chart it, and the catalog search running in the customer's
browser. One engine, wired together with `ATTACH` so a single query can span
tiers, plus Parquet as the append-only lake.

The point is partly a joke on DuckDB itself: it's an embedded *OLAP* engine, so
the transactional tier is the part that *shouldn't* work — which makes it the
most interesting corner to build, and the one seam left deliberately visible.

```
oltp.duckdb        transactions: users, products, inventory, orders  (single writer)
tracker.js         browser tracker: fires pageview/add_to_cart events
collector.py       single-writer collector: storefront + events + real checkout + live stats
lake/              clickstream events as day-partitioned Parquet      (append-only truth)
06_ducklake.sql    promotes the lake to a DuckLake catalog            (snapshots + time travel)
marts.duckdb       downstream analytics: sessions, funnel, retention  (attaches oltp + reads lake)
public/index.html  DuckDB-WASM catalog search + real cart/checkout    (recs computed live via vss)
public/dashboard.html   funnel/revenue/retention, live from oltp.duckdb + the lake, no rebuild
```

## Run it

```bash
./run.sh                      # builds oltp.duckdb, marts.duckdb, lake/, public/catalog.parquet
```

Needs the `duckdb` CLI on your PATH (or `DUCKDB=/path/to/duckdb ./run.sh`).
No extensions required for the core — it runs on a stock binary.

Then the optional layers:

```bash
# Live event tracking — serves the storefront AND collects real events:
python3 collector.py                           # http://localhost:8787/  (needs duckdb, pip install duckdb)
#   → open it, search, click "Add to cart"; each click POSTs an event that the
#     collector appends to lake/ as Parquet. Re-run 04_marts.sql and the live
#     events show up in the funnel and retention alongside the synthetic ones.

duckdb -f 06_ducklake.sql                       # promote the lake to DuckLake: snapshots + time travel (needs network)
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
`purchase` events land in `lake/` as day-partitioned Parquet, with a
semi-structured JSON `props` column so the schema can evolve without a
migration. `03_lake.sql` *synthesizes* a month of events so the marts have
history to chew on; the **live path** is `tracker.js` + `collector.py`.

**Event tracking (the live path).** `tracker.js` runs in the storefront: it
keeps a durable anonymous `visitor_id` and a 30-minute `session_id`, fires a
pageview on load, and sends an `add_to_cart` event when you click a card —
using `navigator.sendBeacon` so events survive page unloads. `collector.py` is
the receiving end and, like the OLTP tier, is a **single writer**: one process
holding one DuckDB connection. It serves the storefront (so events post
same-origin) and appends each batch to `lake/` as uniquely-named Parquet files
in the *same schema the marts read* — so live clicks flow straight into the
funnel and retention next time you build the marts.

**DuckLake (`06_ducklake.sql`).** The plain `lake/` is append-only Parquet with
no catalog — readable, but it can't answer "what did this look like last week?"
DuckLake adds a transactional catalog over Parquet: every commit is a snapshot,
giving you versioning, `AT (VERSION => n)` / `AT (TIMESTAMP => …)` time travel,
and a change feed. The script ingests the lake in a few chronological waves so
there are snapshots to travel between, then replays the funnel as of an earlier
one.

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
- Re-ingest the lake as a real DuckLake catalog for snapshots + `AS OF` queries.
- Replace the toy `vss` vectors with real embeddings for semantic recommendations,
  then fuse them with the DuckPGQ co-purchase graph for a hybrid recommender.
```
