# 🦆 Teal Deal — one duck shop, every tier on DuckDB

**A demo, not a real shop.** The ducks are made up and prices are in quackers (`Q`):
nothing is sold, charged or shipped. Live at https://tealdeal.dorzey.net/.

It is a toy e-commerce platform where **DuckDB is the database for everything**: the
transaction that sells a duck, the clickstream that recorded the visit, the
analytics that chart it, and the catalog search running in the browser. The joke:
DuckDB is an embedded *OLAP* engine, so the transactional tier is the part that
shouldn't work — and the one seam left visible.

## Words used below

- **DuckLake** — a table format: Parquet data files plus a catalog database, with a
  snapshot for every commit. Here the clickstream lives in one (the "lake").
- **Quack** — DuckDB's [client–server protocol over HTTP](https://duckdb.org/2026/05/12/quack-remote-protocol.html):
  one process serves a database, others `ATTACH` to it.
- **Haybarn** — Query Farm's build of DuckDB-Wasm (DuckDB running in the browser) on
  the DuckDB 2.0 engine; both pages use it.
- **Marts** — tables derived from the other tiers: sessions, funnel, retention, revenue.
- **Flock** is the cart, **Duck HQ** is the dashboard, **X-ray** is a mode that shows what
  DuckDB does behind each part of a page.
- **Plain mode** is `./run.sh` on your machine: quack serves the lake (8789) and
  `oltp.duckdb` (8790), and anyone with the token can write. **Public mode**
  (`TEAL_DEAL_PUBLIC=1`, used by the containers) serves only the lake, read-only; see
  [Public mode](#public-mode-and-abuse-limits).

## Run it

Needs [`uv`](https://docs.astral.sh/uv/) and the **DuckDB 2.0.x CLI** on PATH
(`curl https://install.duckdb.org | bash`, or `DUCKDB=/path ./run.sh`). 2.0 files
can't be read by older versions. The first run needs network (extension installs).

```bash
./run.sh     # seed, build, serve — safe to re-run (stops this checkout's previous server)
```

- http://localhost:8787/ — storefront (search, filters, cart, pretend checkout)
- http://localhost:8787/dashboard.html — Duck HQ, the live dashboard

The 🩻 **X-ray** button (or `?xray` in the URL) overlays the DuckDB feature behind each
part of the page, with links to its docs, and turns the ducks into x-rays. The code is
`public/xray.js` (every outbound link is in the table at its top; run `make check-links`
before deploying) and `public/xray.css`.

`make help` lists the targets: `run`, `build` (no serve), `serve` (no rebuild),
`public` / `up` / `down` (local TLS rehearsal, see [Public mode](#public-mode-and-abuse-limits)),
`check`, `browser`, `stress`, `test`, `sprites`, `ranges` (regenerate `ducks.svg` /
`ranges.json`), `check-links`, `stop`.

## Layout

```
01_seed.sql  02_oltp.sql   oltp.duckdb: users, products, inventory, orders, triggers
03_lake.sql                clickstream into a DuckLake catalog (teal_deal_catalog.ducklake + lakehouse_data/)
04_marts.sql               marts.duckdb: sessions, funnel, retention, revenue
05_extras.sql              fts, vss (HNSW), DuckPGQ, spatial (httpfs example commented out)
06_ducklake.sql            snapshots, time travel, change feed
07_stress.py               concurrent-writer test over quack
collector.py               the single writer: serves the site, events, checkout, stats, quack
public/                    index.html (storefront), dashboard.html (Duck HQ), tracker.js,
                           xray.{js,css}, ducks.svg, ranges.json; catalog.parquet is generated
make_sprites.py            generates ducks.svg (one sprite per species + x-ray skeleton)
make_ranges.py             generates ranges.json (rough species ranges for "Ducks near …")
run.sh, Makefile           build + serve, and a wrapper for the common tasks
Dockerfile, compose*.yaml, deploy/, .env.example   image, local TLS rehearsal, Cloudflare Tunnel
test_collector.py, test_pages.py                   unit tests (`make test`, no network)
pyproject.toml, uv.lock, .python-version           Python deps (duckdb pinned to 2.0.0)
```

`oltp.duckdb`, `marts.duckdb`, `teal_deal_catalog.ducklake`, `lakehouse_data/` and
`public/catalog.parquet` are build outputs of `./run.sh` (git-ignored).

Synthetic data is deterministic (`setseed`): 500 users, 60 products, 400
historical orders (+2 from the `02_oltp.sql` demo), about 5,300 events over 31 days
(March 2025). The funnel is roughly 33% view→cart and 33% cart→purchase.

## The tiers

**Transactional (`01`, `02`)** — real `PRIMARY KEY` / `REFERENCES` / `CHECK`
constraints and a `SEQUENCE`. Placing an order is one `BEGIN … COMMIT`:
allocate order, decrement inventory, write lines. `CHECK (stock >= 0)` replaces a
row lock — overselling rolls everything back. DuckDB is single-writer, so all
writes funnel through one process (`collector.py`). While it runs it holds the file
lock, so other processes can't open `oltp.duckdb` even read-only; they go through the
collector (in plain mode, its quack endpoint).

Two 2.0 **triggers** (in the database, so every writer gets them): `trg_stock_audit`
logs inventory changes; `trg_restock` tops a product back up to 40 when an order
leaves it under 5 (so a public demo can't sell out). It hangs off `order_items`
because a trigger can't update its own table. Triggers can't reset the lake —
recreate the container for that (`make up RESET=1`).

**Web analytics (`03`)** — `pageview` / `add_to_cart` / `purchase` events land in
DuckLake with a JSON `props` column (the schema can evolve without migrations). Each
commit is a snapshot, which gives `AT (VERSION => n)` time travel and a change feed.
The seed lands three chronological waves, so there are three snapshots to explore
straight away.

**Live path** — `tracker.js` keeps only a random per-tab `session_id` in
`sessionStorage` (gone when the tab closes; no cookies, no persistent visitor id). It
`sendBeacon`s a pageview on load and an `add_to_cart` when a duck is added to the
flock. `collector.py` batches the events into the lake in the marts' schema and merges
small files every 60 s (`ducklake_merge_adjacent_files`). It holds two connections:
`oltp.duckdb` (orders, recommendations, revenue) and the lake (events, funnel, retention).

**Quack** — the lake is served on 8789 and, in plain mode only, `oltp.duckdb` on 8790
(token `teal-deal-demo-token`):

```bash
duckdb -c "INSTALL quack; LOAD quack;
  ATTACH 'quack:localhost:8789' AS remote (TOKEN 'teal-deal-demo-token');
  SELECT event_type, count(*) FROM remote.events GROUP BY 1;"
```

Remote writes still go through the collector's one connection.

**Marts (`04`)** — attaches OLTP read-only plus the lake: sessionization (30-minute
gap), the session funnel, retention cohorts (lake activity joined to OLTP signup and
country) and a revenue rollup. Rebuild after live traffic with the collector stopped
(it holds the locks): `duckdb marts.duckdb -f 04_marts.sql`.

**Extras (`05`)** — BM25 `fts`; an HNSW `vss` index over text embeddings (the
collector rebuilds that table at startup, which drops the index); a DuckPGQ
co-purchase graph (`MATCH`); `spatial` nearest-warehouse (an `httpfs` remote-Parquet
join is there as a commented-out example). On 2.0, DuckPGQ needs
`SET active_grammar_extensions = ['duckpgq']` after `LOAD` (the scripts do this).
`05`, `06` and the collector's DuckPGQ setup are **best-effort**: if an extension
can't load, that feature degrades and the build continues.

**Time travel (`06`)** — snapshots, `AT (VERSION | TIMESTAMP => …)` and
`ducklake_table_changes`. It needs the catalog file directly, so stop the collector first.

**Concurrent writers (`07_stress.py`)** — `uv run 07_stress.py --workers 8 --duration 10`
(or `make stress`; plain mode only). N threads, each with its own quack connection,
insert into the live lake. Rows are tagged `props: {"stress": true}` so the dashboard's
"⚡ writes" banner counts them apart from organic traffic. DuckLake is optimistic:
under heavy contention a transaction can exhaust its retries (48 workers did: "Exceeded
the maximum retry count of 10 set by `ducklake_max_retry_count`") rather than queue.

## In the browser

**Storefront (`public/index.html`)** ships the catalog as Parquet and runs DuckDB-Wasm
on it: every keystroke, facet and sort is a SQL query (BM25 blended with `ILIKE` so
mid-word typing never goes empty). The console strip at the bottom shows the generated
SQL and timings. The cart has per-line − / ✕, and stock badges poll `/stock` every 4 s.
Checkout and inventory stay on the server (the OLTP tier). Three features worth knowing:

- **Ducks like this** is a `NEAREST` join over small text embeddings (hashed
  bag-of-words as `FLOAT[16]`, compared with `array_distance`). The join and the distance
  function are core DuckDB; `vss` adds an in-memory HNSW index over the embeddings, which
  the join then uses (checked with `EXPLAIN`). At 60 ducks that is no faster than a scan
  and gives the same lists.
- **Ducks near …** filters by a place you pick. Each duck has rough, hand-drawn range
  polygons (`public/ranges.json`, from `make_ranges.py`; for fun, not a field guide) and
  the filter is `ST_Contains(range, ST_Point(lon, lat))` in the `spatial` extension.
  `spatial` is about 7.9 MB over the wire, so it is fetched only when someone picks a place.
- **Since you brought them home** (after checkout) fuses embeddings with a DuckPGQ
  traversal over the order history. That history lives in `oltp.duckdb`, so it is a
  round-trip to the collector (`get_hybrid_recommendations()`).

**Dashboard (`public/dashboard.html`)** — funnel, retention and daily activity are
computed by DuckDB-Wasm *in the browser*, attached over quack to the collector's live
lake connection. These three are quack-only: there is no fallback, and the cards show
"Waiting for the live Quack connection…". Revenue and totals come from the collector's
`/dashboard-data` (the dashboard attaches only the lake, not `oltp.duckdb`).

**Browser engine** — both pages use Query Farm's Haybarn build,
`@haybarn/haybarn-wasm@2.0.0-rc12` (from jsDelivr), pinned to the same version on both
(`test_pages.py` checks). The dashboard needs the threaded build for quack, so the
collector sends `COOP: same-origin` and `COEP: credentialless` for it; the storefront
uses the non-threaded build and needs no isolation. Caveats: Haybarn has no fallback
builds for browsers without Wasm exception handling, and its extension files are
replaced in place at a fixed path, so a pin can stop matching them.

**Asset caching** — the collector serves scripts, styles and sprites at
`/a/<content hash>/<name>` (cached for a year, immutable), so a CDN can't serve a stale
file after a deploy. The hash is in the path, not a `?v=` query, because a CDN rule may
ignore query strings (Cloudflare's did).

## Public mode and abuse limits

`TEAL_DEAL_PUBLIC=1` locks down the lake's quack endpoint, because the page ships its
token to every visitor. Only read statements are accepted: an allowlist of statement
types plus a deny-list of write keywords, file-reading functions and DuckLake's
maintenance functions (`QUACK_READ_ONLY_SQL` in `collector.py`, tested in
`test_collector.py`). It is a keyword filter, so crude: it can refuse a harmless query,
and it was written for this demo, not as a general sandbox. Queries are also capped at
512 MB of memory, 512 MB of temp disk and 2 threads, and the process exits if the
lock-down fails rather than serve a writable lake. `oltp.duckdb` is not served over
quack at all.

Quack's client always POSTs to `/quack`, so a TLS proxy routes that path to the lake and
everything else to the collector, which keeps the site and the lake on one origin.
Locally, Caddy (`deploy/Caddyfile.local`) does this for `teal-deal.test:8443`; that
name only resolves through the browser flag in `make browser` (or an `/etc/hosts` entry).

- `make public` — native collector + Caddy container (Linux only: host networking).
- `make up` — both in containers, works anywhere with Docker. The proxy shares
  the `app` container's network namespace to reach quack on loopback.
- `make check` — confirms the endpoint refuses writes; `make browser` opens it.

**Always-on limits** (`LIMITS` table in `collector.py`; the limiter and the request
validation are unit-tested by `make test`):

- per-IP token buckets (429 + `Retry-After`) on `/collect`, `/order` and the read
  endpoints, plus a global bucket on `/collect` and `/order`; events are also charged one
  token each, per IP and globally, so a request carrying 20 costs 20;
- a 16 KB body cap, at most 20 events per request, orders of 1–20 lines with qty 1–10 (a
  negative qty would add stock), and only the tracker's event types;
- only the tracker's props (`path`, `referrer`, `device`, cut to 200 characters), since
  the lake is publicly readable; the lake stops accepting events at 200,000 rows (503)
  until it is reset;
- in public mode the client IP comes from a proxy-set header
  (`TEAL_DEAL_CLIENT_IP_HEADER`: `X-Forwarded-For` for Caddy, `CF-Connecting-IP` behind
  Cloudflare), trusted only from loopback; an IPv6 client is keyed by its /64;
- responses carry `nosniff` and `X-Frame-Options: DENY`, and the containers are limited
  to 1 GB and 1.5 CPUs.

**Not covered:** a read query is capped on memory, temp disk and threads but not on time
(only `quack_server_max_connections` bounds how many run at once); a distributed flood
can use up the global order limit and lock out real shoppers; and the lake's contents,
including anything a visitor forges through `/collect`, are readable by anyone.

The image bakes in the pinned 2.0.0 CLI, the Python deps, every extension and the
seeded data, so containers need no network at runtime and start from the seed state.

## Deploying: Hetzner + Cloudflare Tunnel

`compose.prod.yaml` runs the app plus `cloudflared`. No ports are published;
`cloudflared` dials out, so the firewall needs only SSH (which also makes
`CF-Connecting-IP` trustworthy). Meant for x86 and ARM (only x86 has been built here).

1. **Tunnel**: Zero Trust → Networks → Tunnels → create, copy the token. Add two
   Public Hostnames for `tealdeal.dorzey.net`, in this order
   (see `deploy/cloudflared.example.yml`):
   1. path `^/quack` → HTTP `127.0.0.1:8789`
   2. no path → HTTP `127.0.0.1:8787`
2. **Node**: Docker + compose plugin; clone the repo.
3. **Start**: `cp .env.example .env` (set `TUNNEL_TOKEN`, and
   `TEAL_DEAL_QUACK_URL` if not `tealdeal.dorzey.net`), then
   `docker compose -f compose.prod.yaml up -d --build`.
4. **Check**: the shop loads, `/dashboard.html` fills in, and
   `curl https://tealdeal.dorzey.net/config` shows `"quack_url": "quack:tealdeal.dorzey.net:443"`.
5. **Reset** (fresh seed, empty lake): `docker compose -f compose.prod.yaml up -d --force-recreate`
   (both containers; `cloudflared` shares the app's namespace). Nightly:
   `0 4 * * * cd /opt/teal-deal && docker compose -f compose.prod.yaml up -d --force-recreate`.

Cloudflare gotchas (not hit here, so untested): features that rewrite scripts
(*Rocket Loader*, *Auto Minify*) or challenge requests (*Bot Fight Mode*, WAF rules on
`POST /collect` or `POST /quack`, which browsers can't solve in the background) could
break the app. The one that did bite was a cache rule ignoring query strings, which
served a stale `tracker.js`; hence the hash in the asset *path*.

The Wasm loads from jsDelivr and the extensions from `haybarn-extensions.query.farm`,
not through Cloudflare. First-load transfer, measured: the storefront moves about
9.5 MB (8.3 MB of Wasm, brotli, plus extensions), and about 7.9 MB more if someone uses
"Ducks near …" (`spatial`); the dashboard's threaded Wasm is about 7.3 MB. The Wasm is
45 MB uncompressed.

## Manual re-runs

Stop the collector first (one process per DuckLake catalog), then e.g.
`duckdb oltp.duckdb -f 05_extras.sql` or `duckdb -f 06_ducklake.sql`.

## Known limitations

- `oltp.duckdb` has one writer; quack clients are still served by the collector's one connection.
- `ducklake_snapshots('remote')` errors over quack ("does not refer to a DuckLake database");
  `remote.snapshots()` and `AT (VERSION | TIMESTAMP => …)` work.
- A Postgres-backed DuckLake catalog would allow real multi-process reads and writes.
- The official `@duckdb/duckdb-wasm` `next` build (1.33.1-dev114.0) is on the 2.0 engine,
  but `LOAD quack` failed there when checked on 2026-11-08 (its `latest`, 1.33.1-dev57.0,
  was not tried), so both pages stay on Haybarn until that works.
