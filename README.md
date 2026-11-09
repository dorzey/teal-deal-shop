# 🦆 Teal Deal — one duck shop, every tier on DuckDB

Toy e-commerce platform where **DuckDB is the database for everything**: the
transaction that sells a duck, the clickstream that recorded the visit, the
analytics that chart it, and the catalog search running in the browser. Tiers
are joined with `ATTACH`, plus a DuckLake catalog (Parquet + snapshots) as the
append-only lake. The joke: DuckDB is an embedded *OLAP* engine, so the
transactional tier is the part that shouldn't work — and the one seam left visible.

## Run it

Needs [`uv`](https://docs.astral.sh/uv/) and the **DuckDB 2.0.x CLI** on PATH
(`curl https://install.duckdb.org | bash`, or `DUCKDB=/path ./run.sh`). 2.0 files
can't be read by older versions. Network needed on first run (extension installs).

```bash
./run.sh     # seed, build, serve — safe to re-run (stops the previous server)
```

- http://localhost:8787/ — storefront (in-browser search, cart, real checkout)
- http://localhost:8787/dashboard.html — Duck HQ, live dashboard

**🩻 X-ray** button (or `?xray`) labels which DuckDB feature powers what, and
turns the ducks into x-rays. Code: `public/xray.js` (all outbound links in one
table at the top; `make check-links` before deploying) + `xray.css`.

`make help` lists targets: `run`, `build` (no serve), `serve` (no rebuild),
`public` / `up` / `down` (local TLS rehearsal, see below), `check`, `browser`,
`stress`, `test`, `check-links`, `stop`.

## Layout

```
01_seed.sql  02_oltp.sql   oltp.duckdb: users, products, inventory, orders, triggers
03_lake.sql                clickstream into a DuckLake catalog (teal_deal_catalog.ducklake + lakehouse_data/)
04_marts.sql               marts.duckdb: sessions, funnel, retention, revenue
05_extras.sql              fts, vss, DuckPGQ, spatial, httpfs
06_ducklake.sql            snapshots, time travel, change feed
07_stress.py               concurrent-writer test over quack
collector.py               the single writer: serves site, events, checkout, stats, quack
public/                    index.html, dashboard.html, tracker.js, xray.{js,css}, ducks.svg
make_sprites.py            generates ducks.svg (one sprite per species + x-ray skeleton)
Dockerfile, compose*.yaml, deploy/    image, local TLS rehearsal, Cloudflare Tunnel
test_collector.py, test_pages.py      unit tests (`make test`)
```

Synthetic data is deterministic (`setseed`): 500 users, 60 products, 400
historical orders (+2 from the `02_oltp.sql` demo), 5,343 events over 31 days (March
2025). Funnel ≈ 33% view→cart, 33% cart→purchase.

## The tiers

**Transactional (`01`, `02`)** — real `PRIMARY KEY` / `REFERENCES` / `CHECK`
constraints and a `SEQUENCE`. Placing an order is one `BEGIN … COMMIT`:
allocate order, decrement inventory, write lines. `CHECK (stock >= 0)` replaces a
row lock — overselling rolls everything back. DuckDB is single-writer, so all
writes funnel through one process (`collector.py`). While it runs it holds the file
lock, so other processes can't open `oltp.duckdb` even read-only; they go through the
collector (in plain mode, its quack endpoint).

Two 2.0 **triggers** (in the DB, so every writer gets them): `trg_stock_audit`
logs inventory changes; `trg_restock` tops a product back up to 40 when an order
leaves it under 5 (so a public demo can't sell out). It hangs off `order_items`
because a trigger can't update its own table. Triggers can't reset the lake —
recreate the container for that (`make up RESET=1`).

**Web analytics (`03`)** — `pageview` / `add_to_cart` / `purchase` events land in
DuckLake with a JSON `props` column (schema evolves without migrations). Each
commit is a snapshot: `AT (VERSION => n)` time travel and a change feed. The seed
lands three chronological waves = three snapshots to explore immediately.

**Live path** — `tracker.js` keeps only a random per-tab `session_id` (`sessionStorage`: gone when
the tab closes, no cookies, no persistent visitor id) and `sendBeacon`s a pageview on load and `add_to_cart` when a duck is added to the flock.
`collector.py` batches them into the lake in the marts' schema, and merges small
files every 60 s (`ducklake_merge_adjacent_files`). It holds two connections:
`oltp.duckdb` (orders, recs, revenue) and the lake (events, funnel, retention).

**Quack** — DuckDB's [HTTP client-server protocol](https://duckdb.org/2026/05/12/quack-remote-protocol.html).
The lake is served on 8789 and, in plain mode only, `oltp.duckdb` on 8790 (token
`teal-deal-demo-token`; public mode serves just the read-only lake):

```bash
duckdb -c "INSTALL quack; LOAD quack;
  ATTACH 'quack:localhost:8789' AS remote (TOKEN 'teal-deal-demo-token');
  SELECT event_type, count(*) FROM remote.events GROUP BY 1;"
```

Remote writes still go through the collector's one connection.

**Marts (`04`)** — attaches OLTP read-only plus the lake: sessionization (30-min
gap), session funnel, retention cohorts (lake activity joined to OLTP signup +
country), revenue rollup. Rebuild after live traffic with the collector stopped (it
holds the locks): `duckdb marts.duckdb -f 04_marts.sql`.

**Extras (`05`)** — BM25 `fts`, HNSW `vss` over text embeddings, a DuckPGQ
co-purchase graph (`MATCH`), `spatial` nearest-warehouse (an `httpfs` remote-Parquet
join is there as a commented-out example).
DuckPGQ needs `SET active_grammar_extensions = ['duckpgq']` after `LOAD` on 2.0
(the scripts do this). `05`, `06` and the collector's DuckPGQ setup are
**best-effort**: if an extension can't load, that feature degrades; the build continues.

**Time travel (`06`)** — snapshots, `AT (VERSION | TIMESTAMP => …)`, and
`ducklake_table_changes`. Needs the catalog file directly (stop the collector).

**Storefront (`public/index.html`)** — ships the catalog as Parquet and runs
DuckDB-Wasm in the browser: every keystroke, facet and sort is a SQL query
(BM25 blended with `ILIKE` so mid-word typing never goes empty). "Ducks like
this" is a `NEAREST` join over small text embeddings (hashed bag-of-words,
`array_distance`); the page loads `vss` but nothing there needs it. The console strip shows the
generated SQL and timings. The cart ("flock") has per-line − / ✕; stock badges
poll `/stock` every 4 s. **Truth stays on the server**: checkout and inventory
are the OLTP tier. "Since you brought them home" fuses embeddings with a DuckPGQ
traversal over the order history, which lives in `oltp.duckdb`, so it's a round-trip
(`get_hybrid_recommendations()`). DuckPGQ itself does load in the Haybarn Wasm build
(checked: `INSTALL duckpgq` from Haybarn's community repo, the grammar opt-in and a
`GRAPH_TABLE` query all work), so the walk could run in the browser if the graph were shipped to it.

The collector serves scripts/styles/sprites at `/a/<content hash>/<name>` (cached
a year, immutable) so a CDN can't serve stale ones after a deploy. The hash is in
the path, not a `?v=` query, because a CDN rule may ignore query strings.

**Dashboard (`public/dashboard.html`)** — funnel, retention and daily activity are
computed by DuckDB-Wasm *in the browser*, attached over quack to the collector's
live lake connection. These three are quack-only (no fallback; cards show
"Waiting for the live Quack connection…"). Revenue/totals come from
`/dashboard-data` (needs `oltp.duckdb`, which the dashboard doesn't attach yet).

Both pages use Query Farm's Haybarn build `@haybarn/haybarn-wasm@2.0.0-rc12`
(jsDelivr), pinned the same on both (`test_pages.py` checks). Its quack extension
loads and can `ATTACH` to the 2.0 server. The official `@duckdb/duckdb-wasm` `next`
(1.33.1-dev114.0) is also on the 2.0 engine, but there `LOAD quack` failed ("could not be
loaded") when tried on 2026-11-08, so the dashboard can't attach with it (its `latest`,
1.33.1-dev57.0, was not tried). The dashboard needs the threaded build, so
the collector sends `COOP: same-origin` + `COEP: credentialless` for it; the
storefront uses the non-threaded build (no isolation). Caveats: no fallback
builds for browsers lacking Wasm exception handling, and extension files are
replaced in place. Move to the official package once it ships 2.0 with quack.

**Concurrent writers (`07_stress.py`)** — `uv run 07_stress.py --workers 8 --duration 10`
(or `make stress`; plain mode only). N threads, each with its own quack connection,
insert into the live lake. Rows are tagged `props: {"stress": true}` so the
dashboard's "⚡ writes" banner counts them apart from organic traffic. DuckLake
is optimistic: under heavy contention a transaction can exhaust its retries
(48 workers did: "Exceeded the maximum retry count of 10 set by `ducklake_max_retry_count`")
rather than queue.

## Public mode and abuse limits

`TEAL_DEAL_PUBLIC=1` makes the collector refuse every quack write and file read,
so the page's public quack token is harmless. Caddy (`deploy/Caddyfile.local`)
terminates TLS for `teal-deal.test:8443`; quack's client always POSTs `/quack`,
so the proxy routes that path to the lake and the rest to the collector — one origin.

- `make public` — native collector + Caddy container (Linux only: host networking).
- `make up` — both in containers, works anywhere with Docker. The proxy shares
  the `app` container's network namespace to reach quack on loopback.
- `make check` — confirms the endpoint refuses writes. `make browser` opens it.

**Always-on limits** (`LIMITS` table in `collector.py`; the limiter and the request
validation are unit-tested by `make test`):
per-IP token buckets (429 + `Retry-After`) on `/collect`, `/order` and the read
endpoints, plus a global bucket on `/collect` and `/order`; 16 KB body cap; max 20 events per request; orders of 1–20
lines with qty 1–10 (a negative qty would add stock); only the tracker's event
types. In public mode the client IP comes from a proxy-set header (`TEAL_DEAL_CLIENT_IP_HEADER`:
`X-Forwarded-For` for Caddy, `CF-Connecting-IP` behind Cloudflare), only trusted
from loopback. Not covered: queries sent straight to the read-only quack
endpoint (only `quack_server_max_connections` caps them), and a distributed flood
can use up the global order limit and lock out real shoppers.

The image bakes in the pinned 2.0.0 CLI, Python deps, every extension and the
seeded data, so containers need no runtime network and start from seed state.

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
break the app. One that did bite: a cache rule ignoring query strings served a stale
`tracker.js`, hence the hash in the asset *path*.
Wasm loads from jsDelivr and the extensions from `haybarn-extensions.query.farm`, not
through Cloudflare. First load, measured: the storefront moves ~9.5 MB (8.3 MB wasm,
brotli, plus extensions); the dashboard's threaded wasm is ~7.3 MB. The wasm is 45 MB
uncompressed.

## Manual re-runs

Stop the collector first (one process per DuckLake catalog), then e.g.
`duckdb oltp.duckdb -f 05_extras.sql` or `duckdb -f 06_ducklake.sql`.

## Known limitations

- `oltp.duckdb` has one writer; quack clients are still served by the collector's one connection.
- `ducklake_snapshots('remote')` errors over quack ("does not refer to a DuckLake database");
  `remote.snapshots()` and `AT (VERSION | TIMESTAMP => …)` work.
- A Postgres-backed DuckLake catalog would allow real multi-process reads/writes.
