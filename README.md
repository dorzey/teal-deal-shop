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
07_stress.py       concurrent-writer stress test over quack           (multiple writers, live)
marts.duckdb       downstream analytics: sessions, funnel, retention  (attaches oltp + the lake)
public/index.html  DuckDB-WASM catalog search (BM25 fts) + real cart/checkout + live recs
public/dashboard.html   funnel/revenue/retention, live from oltp.duckdb + the lake, no rebuild
```

## Run it

```bash
./run.sh                      # builds everything, then serves it — no extra steps
```

That's genuinely everything: seeds OLTP, lands the clickstream in a DuckLake
catalog, exports the browser catalog, builds the marts, runs the extensions
showcase and the snapshot/time-travel exploration (both best-effort — see
below), then starts `collector.py` itself as its last act:

- **http://localhost:8787/** — the storefront, DuckDB-WASM catalog search + real checkout
- **http://localhost:8787/dashboard.html** — Duck HQ, the live dashboard

Ctrl-C stops it (flushes cleanly on exit). Re-running `./run.sh` is always
safe — it stops whatever server a previous run left up before rebuilding, so
you never get a "port already in use" error.

Needs the `duckdb` CLI on your PATH (or `DUCKDB=/path/to/duckdb ./run.sh`) and
`pip install duckdb==1.5.4` for `collector.py` — pinned because the community
`duckpgq` extension (used for hybrid recommendations, see below) lags behind
`pip`'s latest DuckDB release; a newer pip version just means that one feature
degrades gracefully rather than breaking the app. Also needs network: the
DuckLake catalog (`03_lake.sql`) requires it on the first run to install the
`ducklake` extension (cached after that) — that's a core dependency now, not
an optional tier. `05_extras.sql` (fts/vss/spatial/DuckPGQ), `06_ducklake.sql`
(snapshot exploration), and `collector.py`'s own DuckPGQ setup all run
**best-effort**: if their extensions can't be reached (e.g. the community
`duckpgq` host is down, or its build hasn't caught up to your DuckDB version),
each logs it and keeps going rather than failing the whole build/app over an
optional demo tier.

Want to re-run one piece by hand instead — e.g. after editing `05_extras.sql`
— without rebuilding everything? Stop the collector first (only one process
can have the DuckLake catalog attached at a time), then:

```bash
duckdb oltp.duckdb -f 05_extras.sql             # fts / vss / spatial / DuckPGQ
duckdb -f 06_ducklake.sql                       # explore the live catalog: snapshots + time travel
duckdb marts.duckdb -f 04_marts.sql             # rebuild marts after live collector.py traffic
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
the receiving end and, like the OLTP tier, is a **single writer** — two of
them now, actually: one connection for `oltp.duckdb` (orders, recommendations,
the dashboard's revenue) and a separate one for the DuckLake catalog (event
flushes, the dashboard's funnel/retention). It serves the storefront (so
events post same-origin), batches events, and `INSERT`s each batch straight
into the DuckLake catalog in the *same schema the marts read* — live clicks
flow into the funnel and retention next time you build the marts. A
background thread calls `ducklake_merge_adjacent_files` every 60s so the
small-file fragmentation that batching used to hand-roll around is handled by
DuckLake's own documented maintenance operation instead.

The DuckLake connection also calls `quack_serve()` — DuckDB's own HTTP
client-server protocol, core since v1.5.3 — so a second process can `ATTACH
'quack:localhost:8789' AS remote (TOKEN 'puddle-demo-token')` and read/write
events live, *while the collector is running*, instead of needing the file
directly:

```bash
duckdb -c "
  INSTALL quack; LOAD quack;
  ATTACH 'quack:localhost:8789' AS remote (TOKEN 'puddle-demo-token');
  SELECT event_type, count(*) FROM remote.events GROUP BY event_type;
"
```

`oltp.duckdb` deliberately does **not** get the same treatment: confirmed
empirically that this beta release of quack makes any table with a `FOREIGN
KEY` invisible to remote clients (`PRIMARY KEY` and `CHECK` are fine,
`REFERENCES` is not) — and every real table in `oltp.duckdb` has one, since
real constraints are the entire point of that tier. Dropping them to work
around a beta bug in an external tool isn't a trade worth making, so
`oltp.duckdb`'s single-writer limitation is unchanged: still one process,
still funnel every write through it.

`public/dashboard.html` is the second client, and the more interesting one:
its funnel/retention/daily-activity charts are computed by DuckDB-WASM
running *in the browser*, attached straight to `collector.py`'s live
DuckLake connection over quack — same thesis as the storefront's in-browser
search, now against a live server instead of a static Parquet export. These
three are **quack-only, no `/dashboard-data` fallback**: `get_dashboard()`
doesn't compute them server-side at all anymore, only `revenue_by_category`/
`totals` (they need `oltp.duckdb`, which isn't quack-reachable, see above).
Until the in-browser connection finishes instantiating (a few seconds), or
if a live query ever fails, those three cards show a plain "waiting for the
live Quack connection…" placeholder rather than silently serving stale or
re-fetched JSON — the status line says "funnel/retention/daily via Quack,
client-side" once it's up, "waiting for Quack connection…" before that.
Needs `duckdb-wasm@1.33.1-dev57.0` specifically, not the storefront's
pinned `1.29.0` — confirmed that 1.29.0 can `INSTALL`/`LOAD` quack (its
functions work) but doesn't yet have quack's `ATTACH`/storage support
("Unrecognized storage type").

**Concurrent writers (`07_stress.py`).** DuckDB's own Quack announcement
claims "multiple concurrent writers" — this actually exercises that instead
of taking it on faith. N threads, each with its own quack connection,
`INSERT` into the live DuckLake catalog at once:

```bash
python3 07_stress.py --workers 8 --duration 10
```

Writes use real `event_type`s (weighted like real traffic — mostly
pageviews) and land in the exact same table `dashboard.html` reads, on
purpose: the point is watching the actual sessions/funnel/retention/daily
numbers move live while this runs, not a bespoke counter in a universe of
its own. First version used a separate `'stress_write'` type excluded from
every real query — technically clean, but pointless, since a reader
couldn't see the writes land anywhere. Real types it is; a stress run
visibly nudges the funnel and adds a genuine retention/daily-activity
spike for "today" (`weeks_since` jumps way out, since the seed data is
dated ~March 2025 and the stress run's timestamps are real "now") — that's
honest, not corruption, since it's exactly what a real traffic burst
against this seed data would do too. Each row is still tagged `props:
{"stress": true}` so the dashboard's own live "⚡ N writes · N/s" banner
(a separate, faster-polling query, filtering on that tag) can count them
distinctly from organic traffic. Also surfaced a real DuckLake behavior
worth knowing: under enough concurrent write pressure, a transaction can
exhaust its retry budget and fail outright (`Exceeded the maximum retry
count... Conflict on update!`) — optimistic concurrency, not a queue, so
contention shows up as retried-then-failed writes rather than serialized
waiting.

**DuckLake (`06_ducklake.sql`).** Since the lake *is* the catalog now, this
script no longer ingests anything — it just explores what's already there:
`ducklake_snapshots`, `AT (VERSION => n)` / `AT (TIMESTAMP => …)` time travel,
and a change feed via `ducklake_table_changes`, replaying the funnel as of an
earlier snapshot. This one still needs the file directly (stop `collector.py`
first) — confirmed empirically that DuckLake's time-travel/snapshot functions
don't work through a quack attachment yet, even though plain reads/writes do
(see above). That's real, on the DuckDB team's own roadmap (they've said
DuckLake-as-a-quack-catalog-server is coming), not a Puddle-specific gap.
Real concurrent multi-process access to `oltp.duckdb`, or to time-travel
queries against the lake, still needs either that upstream fix or Postgres
(or similar) as the catalog backend instead of a local file.

**`04_marts.sql` — downstream analytics (home turf).** `ATTACH`es the OLTP store
read-only and reads the lake in the same queries. Sessionization via window
functions (30-min inactivity gap ⇒ new session), a session-level view→cart→
purchase funnel, retention cohorts (a cross-database join of lake activity to
OLTP signup + country), and a revenue rollup.

**`05_extras.sql` — the fun tier, all just extensions.** BM25 full-text search
(`fts`), HNSW vector similarity over real text embeddings (`vss`), a co-purchase
graph you traverse with a real `MATCH` (DuckPGQ), nearest-warehouse fulfilment
(`spatial`), a live remote price-feed join (`httpfs`), and time-travel notes for
DuckLake.

**`public/` — client-side catalog search (DuckDB-WASM).** Ships the catalog as
Parquet and runs DuckDB *in the browser*: every keystroke, facet, and sort is a
real SQL query with zero round-trips, blending BM25 (`fts`) with an `ILIKE`
fallback so mid-word typing never flickers to zero results. "Ducks like this"
is a real text embedding (`vss`), computed live in-browser too. The console
strip at the bottom shows the generated SQL and its sub-millisecond execution
time — the whole thesis, made visible. Reads and personalization can live in
the browser; **truth stays on the server** (anything on the write path —
checkout, inventory — is the OLTP tier). One exception: "since you brought
them home" fuses that same text embedding with a DuckPGQ co-purchase graph
traversal, and DuckPGQ doesn't load in DuckDB-WASM (confirmed) — so that one
recommendation is a real round-trip to `collector.py` (`get_hybrid_recommendations()`),
the same tiered real-signal-first/fallback pattern `get_recommendations()`
already uses for "shoppers like you also got".

## Notes on the synthetic data

Deterministic (`setseed`) so every run is identical. ~500 users, 60 products,
400 seeded historical orders + 1 live transaction, ~6,500 clickstream events
across 31 daily partitions. The funnel is engineered to ~33% view→cart and
~33% cart→purchase; retention has gentle built-in churn so the cohort triangle
actually decays instead of sitting flat.

## Upgrade paths

- ~~Swap the browser's `ILIKE` search for `fts` loaded in WASM (BM25 in-browser).~~
  Done — see the `public/` tier above.
- ~~Replace the toy `vss` vectors with real embeddings for semantic recommendations,
  then fuse them with the DuckPGQ co-purchase graph for a hybrid recommender.~~
  Done — a dependency-free hashed bag-of-words text embedding (no external model),
  fused server-side with a DuckPGQ co-purchase graph. See the `public/` tier above.
- Back the DuckLake catalog with Postgres instead of a local file, for real
  concurrent multi-process reads/writes. **Partly done** — investigated
  Quack (DuckDB's own HTTP client-server protocol, core since v1.5.3)
  instead of reaching for Postgres, and it genuinely works for the
  DuckLake catalog: `collector.py` now serves it live, and a second
  process can read/write events without stopping the collector (see
  "Event tracking" above). It does *not* solve `oltp.duckdb`, though —
  confirmed a beta bug where any table with a `FOREIGN KEY` is invisible
  to quack clients, which is every real table there — nor DuckLake time
  travel, which doesn't work through quack yet at all (upstream roadmap
  item). Postgres remains the answer for those two until quack matures.
  `public/dashboard.html` is the concrete payoff: its funnel/retention/daily
  charts now run as real SQL in DuckDB-WASM, attached live over quack,
  instead of a `/dashboard-data` round-trip — see the `public/` tier above.
