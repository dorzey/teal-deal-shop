#!/usr/bin/env python3
"""
collector.py — Teal Deal's single-writer event collector.

Two single-writer DuckDB connections: one for oltp.duckdb, one for the
DuckLake catalog (see _oltp()/_lake()). The DuckLake connection is also
served over Quack (DuckDB's own HTTP client-server protocol): another
process can ATTACH to it over the network and read/write events while this
one keeps running, instead of needing the file directly. oltp.duckdb is
served the same way, on its own port (see the comment above _oltp()/_lake()).

  • serves the storefront from ./public  (so the tracker posts same-origin)
  • accepts POST /collect events and appends them to the DuckLake catalog,
    in the SAME schema 04_marts.sql reads — live events land in the funnel.
  • GET /dashboard-data serves revenue/totals for public/dashboard.html —
    its funnel/retention/daily come directly from the browser over quack.

Run from the repo root:   python3 collector.py    ->    http://localhost:8787/
Events batch on size or every few seconds, then INSERT as one transaction —
DuckLake gives every insert a snapshot, so a periodic background call to
ducklake_merge_adjacent_files() keeps that from turning into thousands of
tiny files (see _compactor()) instead of the app hand-rolling file layout.
"""
import json, os, sys, threading, time, datetime as dt
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import duckdb

ROOT       = os.path.dirname(os.path.abspath(__file__))
PUBLIC     = os.path.join(ROOT, "public")
PORT       = 8787
BATCH      = 20            # flush after this many buffered events
FLUSH_SECS = 5.0            # …or this many seconds, whichever comes first
COMPACT_SECS = 60.0         # how often to merge small lake files in the background

# Relative, matching 03_lake.sql/04_marts.sql/06_ducklake.sql exactly — DuckLake
# stores the DATA_PATH string given at catalog-creation time and requires later
# ATTACHes to match it verbatim (relative vs. absolute counts as a mismatch),
# so this can't be ROOT-absolute like oltp.duckdb's path below. Consistent with
# those scripts, and this file's own docstring: run collector.py from the repo root.
DUCKLAKE_CATALOG = "teal_deal_catalog.ducklake"
# DuckLake's own database name once opened directly (not ATTACHed under an
# alias) is the file stem. Needed by name for ducklake_merge_adjacent_files()
# below.
DUCKLAKE_DB      = os.path.splitext(DUCKLAKE_CATALOG)[0]

# Plain fixed token — this is a localhost-only demo (quack_serve defaults
# allow_other_hostname to false), no different from oltp.duckdb having no
# auth today. A real deployment would generate + guard this properly.
QUACK_TOKEN    = "teal-deal-demo-token"
QUACK_LAKE_PORT = 8789
QUACK_OLTP_PORT = 8790

# Public mode (TEAL_DEAL_PUBLIC=1): for running behind a TLS proxy on the open
# internet. The page ships the quack token, so it is public — the lake's quack
# endpoint is made read-only instead (see _lock_down_lake()), and oltp.duckdb is
# not served over quack at all. TEAL_DEAL_QUACK_URL is what the dashboard attaches
# to (the proxy's public address); /config hands it to the browser.
PUBLIC_MODE    = os.environ.get("TEAL_DEAL_PUBLIC") == "1"
QUACK_PUBLIC_URL = os.environ.get("TEAL_DEAL_QUACK_URL", f"quack:localhost:{QUACK_LAKE_PORT}")

# ---- the single writer, twice over: oltp.duckdb and the DuckLake catalog ---
# Each file only allows one connection to hold it — DuckDB's single-writer
# model. The DuckLake connection additionally calls quack_serve(): a second
# process can ATTACH to it over the network and read/write events while
# this process is running, instead of needing the file directly.
#
# oltp.duckdb is served too, on its own port: remote writes still funnel
# through this one process, so its single-writer limit stands (see README).
# DuckLake time travel (`AT (VERSION => n)`) works through the quack
# attachment; ducklake_snapshots() does not, so 06_ducklake.sql still needs
# the file directly.
#
# oltp and the lake are two separate connections because a quack server
# only exposes its own PRIMARY database to clients — the lake connection
# needs to be primary for quack_serve() to expose it at all. Both connect
# lazily so importing this module (as the collection-path tests do) never
# touches either file.
_oltp_con, _oltp_lock = None, threading.Lock()
_lake_con, _lake_lock = None, threading.Lock()
_has_hybrid = False

def _oltp():
    global _oltp_con, _has_hybrid
    if _oltp_con is None:
        con = duckdb.connect(os.path.join(ROOT, "oltp.duckdb"))
        _oltp_con = con
        if not PUBLIC_MODE:
            try:
                con.execute("INSTALL quack")
                con.execute("LOAD quack")
                con.execute(
                    f"SELECT * FROM quack_serve('quack://localhost:{QUACK_OLTP_PORT}', "
                    f"token := '{QUACK_TOKEN}', disable_ssl := true)"
                )
                print(f"[collector] oltp.duckdb reachable via "
                      f"quack:localhost:{QUACK_OLTP_PORT} (token {QUACK_TOKEN})", flush=True)
            except Exception as e:
                print(f"[collector] quack unavailable for oltp.duckdb: {e}", file=sys.stderr)
        try:
            _setup_hybrid(con)
            _has_hybrid = True
        except Exception as e:
            print(f"[collector] hybrid recommendations unavailable: {e}", file=sys.stderr)
    return _oltp_con

def _lake():
    """Opens the DuckLake catalog directly as this connection's own primary
    database and serves it over quack (see the module-level note above).
    Best-effort: quack is core in DuckDB v1.5.3+, but an older DuckDB just
    means no remote access to the lake, not a crash — same tolerance as
    the hybrid recommender's duckpgq dependency."""
    global _lake_con
    if _lake_con is None:
        con = duckdb.connect(f"ducklake:{DUCKLAKE_CATALOG}")
        try:
            con.execute("INSTALL quack")
            con.execute("LOAD quack")
            con.execute(
                f"SELECT * FROM quack_serve('quack://localhost:{QUACK_LAKE_PORT}', "
                f"token := '{QUACK_TOKEN}', disable_ssl := true)"
            )
            print(f"[collector] the DuckLake catalog reachable via "
                  f"quack:localhost:{QUACK_LAKE_PORT} (token {QUACK_TOKEN})", flush=True)
            if PUBLIC_MODE:
                _lock_down_lake(con)
        except Exception as e:
            print(f"[collector] quack unavailable for the DuckLake catalog: {e}", file=sys.stderr)
        _lake_con = con
    return _lake_con

def _lock_down_lake(con):
    """Make the lake's quack endpoint safe to publish: read queries only, and no
    filesystem access beyond the lake's own data files.

    quack calls `quack_authorization_function(session, query)` on every query,
    including the catalog queries ATTACH itself sends (WITH/SELECT/BEGIN/COMMIT…),
    so this is an allowlist of statement types plus a deny-list of write and
    file-reading keywords (which also catches `WITH x AS (DELETE …)`). Crude —
    a keyword inside a string literal is refused too — but fine for a demo.
    enable_external_access=false must come after quack_serve (quack needs its
    crypto module at start) and allowed_directories keeps the DuckLake data
    path readable for the collector's own inserts and compaction."""
    con.execute(r"""CREATE OR REPLACE MACRO quack_read_only(sid, q) AS
        regexp_matches(q, '^\s*(?i:select|with|from|begin|commit|rollback|abort|start|end|show|describe|summarize|explain)\b')
        AND NOT regexp_matches(q, '(?i)\b(insert|update|delete|drop|create|alter|copy|attach|detach|install|load|truncate|merge|export|import|vacuum|checkpoint|set|reset|call|pragma|read_csv|read_text|read_blob|read_parquet|read_json|glob)\b')""")
    con.execute("SET quack_authorization_function = 'quack_read_only'")
    con.execute("SET quack_server_max_connections = 64")   # one per open dashboard, roughly
    con.execute("SET allowed_directories = ['lakehouse_data/']")
    con.execute("SET enable_external_access = false")
    print("[collector] public mode: lake quack endpoint is read-only", flush=True)

def _setup_hybrid(con):
    """One-time build of the hybrid recommender's two signals: a real text
    embedding (hashed bag-of-words over name+description — no external model,
    same formula as public/index.html and 05_extras.sql) and a DuckPGQ
    co-purchase graph. `duckpgq` is a community extension, so this is
    best-effort — see the try/except in _oltp() — the same tolerance run.sh
    already gives 05_extras.sql for the identical reason."""
    con.execute("INSTALL vss")
    con.execute("INSTALL duckpgq FROM community")
    con.execute("LOAD vss")
    con.execute("LOAD duckpgq")
    con.execute("SET active_grammar_extensions = ['duckpgq']")  # DuckDB 2.0 opt-in
    con.execute("""
        CREATE OR REPLACE TABLE _tokens AS
        SELECT product_id, unnest(string_split(
            regexp_replace(lower(name || ' ' || name || ' ' || description), '[^a-z0-9 ]', ' ', 'g'), ' '
        )) AS token
        FROM products
    """)
    con.execute("""
        CREATE OR REPLACE TABLE _buckets AS
        SELECT product_id, (hash(token) % 16)::INTEGER AS bucket, count(*) AS n
        FROM _tokens WHERE token <> '' GROUP BY product_id, bucket
    """)
    con.execute("""
        CREATE OR REPLACE TABLE product_vectors AS
        SELECT p.product_id, list(coalesce(b.n, 0)::FLOAT ORDER BY g.bucket)::FLOAT[16] AS embedding
        FROM products p CROSS JOIN generate_series(0, 15) g(bucket)
        LEFT JOIN _buckets b ON b.product_id = p.product_id AND b.bucket = g.bucket
        GROUP BY p.product_id
    """)
    con.execute("DROP TABLE _tokens")
    con.execute("DROP TABLE _buckets")
    con.execute("""
        CREATE OR REPLACE TABLE hybrid_copurchase AS
        SELECT a.product_id AS p1, b.product_id AS p2, count(*) AS weight
        FROM order_items a JOIN order_items b ON a.order_id = b.order_id AND a.product_id <> b.product_id
        GROUP BY 1, 2
    """)
    con.execute("""
        CREATE OR REPLACE PROPERTY GRAPH hybrid_graph
            VERTEX TABLES (products LABEL product)
            EDGE TABLES (
                hybrid_copurchase SOURCE KEY (p1) REFERENCES products (product_id)
                                  DESTINATION KEY (p2) REFERENCES products (product_id)
                                  LABEL bought_with
            )
    """)

# ---- abuse limits ------------------------------------------------------------
# The storefront is meant to be reachable by anyone, so /collect and /order get
# rate limits and strict input validation. Not production grade: in-memory token
# buckets, per client IP and globally, plus size caps. Limits are (requests per
# minute, burst).
LIMITS = {                       # per client IP
    "/collect":                 (120, 30),   # tracker: one beacon per click
    "/order":                   (10, 5),
    "/recommendations":         (60, 20),
    "/hybrid-recommendations":  (60, 20),
    "/dashboard-data":          (60, 20),    # the dashboard polls every 4 s per tab
}
GLOBAL_LIMITS = {                # across all clients: bounds lake growth / stock drain
    "/collect": (1200, 200),
    "/order":   (60, 20),
}
MAX_BODY = 16 * 1024
MAX_EVENTS_PER_REQUEST = 20
MAX_BUFFER = 5000                # events waiting to flush; beyond this, shed load
MAX_ORDER_ITEMS, MAX_ORDER_QTY = 20, 10
MAX_PROPS_BYTES = 2048
EVENT_TYPES = {"pageview", "add_to_cart", "purchase"}   # what tracker.js sends

class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status

class RateLimiter:
    """Token buckets: `rate` tokens per minute, up to `burst` stored."""
    def __init__(self):
        self._buckets, self._lock, self._pruned = {}, threading.Lock(), time.monotonic()

    def allow(self, key, rate, burst):
        """Returns 0 if allowed, else the seconds to wait before retrying."""
        now = time.monotonic()
        with self._lock:
            tokens, then = self._buckets.get(key, (burst, now))
            tokens = min(burst, tokens + (now - then) * rate / 60)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return max(1, int((1 - tokens) * 60 / rate) + 1)
            self._buckets[key] = (tokens - 1, now)
            if now - self._pruned > 300:          # drop buckets that have refilled
                self._pruned = now
                self._buckets = {k: v for k, v in self._buckets.items()
                                 if v[0] < burst or now - v[1] < 600}
            return 0

_limiter = RateLimiter()

def _int_or_none(v, name):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise HttpError(400, f"{name} must be an integer")
    return v

def _short_str(v, name, limit=64):
    if v is not None and (not isinstance(v, str) or len(v) > limit):
        raise HttpError(400, f"{name} must be a string of at most {limit} characters")
    return v

# ---- event buffering ---------------------------------------------------------
_buf, _lock, _next_id = [], threading.Lock(), 1_000_000  # live ids start high

def _parse_ts(s):
    # tracker.js always sends a well-formed new Date().toISOString(); a
    # malformed one (e.g. a hand-crafted /collect call) surfaces as a 400.
    try:
        ts = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        raise HttpError(400, "event_ts must be an ISO timestamp")
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
    # visitors' clocks drift, but not by days
    if abs(ts - dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)) > dt.timedelta(days=1):
        raise HttpError(400, "event_ts is too far from the server's clock")
    return ts

def _clean_event(ev):
    """Validate one tracker event and shape it into a lake row (minus event_id).
    Everything is checked here, before anything is buffered, so a bad event can
    never poison a whole flush batch."""
    if not isinstance(ev, dict):
        raise HttpError(400, "event must be an object")
    event_type = ev.get("event_type") or "pageview"
    if event_type not in EVENT_TYPES:
        raise HttpError(400, f"event_type must be one of {sorted(EVENT_TYPES)}")
    ts = _parse_ts(ev.get("event_ts"))
    props = ev.get("props") or {}
    if not isinstance(props, dict) or len(props) > 20 or any(not isinstance(k, str) for k in props):
        raise HttpError(400, "props must be an object with at most 20 keys")
    props = dict(props)
    props["visitor_id"] = _short_str(ev.get("visitor_id"), "visitor_id")
    props["session_id"] = _short_str(ev.get("session_id"), "session_id")
    props_json = json.dumps(props)
    if len(props_json) > MAX_PROPS_BYTES:
        raise HttpError(400, "props too large")
    return (_int_or_none(ev.get("user_id"), "user_id"), event_type,
            _int_or_none(ev.get("product_id"), "product_id"), ts, ts.date(), props_json)

def enqueue(ev, _clean=None):
    """Normalise one tracker event into the lake schema and buffer it."""
    global _next_id
    fields = _clean or _clean_event(ev)
    with _lock:
        if len(_buf) >= MAX_BUFFER:
            raise HttpError(503, "busy, try again shortly")
        eid, _next_id = _next_id, _next_id + 1
        _buf.append((eid, *fields))
        due = len(_buf) >= BATCH
    if due:
        flush()

def _clean_order(payload):
    """Validate an /order body into (user_id, items). Without this a client could
    order a negative quantity and *add* stock."""
    if not isinstance(payload, dict):
        raise HttpError(400, "order must be an object")
    user_id = _int_or_none(payload.get("user_id"), "user_id")
    if user_id is None or user_id < 1:
        raise HttpError(400, "user_id must be a positive integer")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ORDER_ITEMS:
        raise HttpError(400, f"items must be a list of 1 to {MAX_ORDER_ITEMS}")
    clean = []
    for it in items:
        if not isinstance(it, dict):
            raise HttpError(400, "each item must be an object")
        pid, qty = _int_or_none(it.get("product_id"), "product_id"), _int_or_none(it.get("qty"), "qty")
        if pid is None or qty is None or not 1 <= qty <= MAX_ORDER_QTY:
            raise HttpError(400, f"each item needs a product_id and a qty from 1 to {MAX_ORDER_QTY}")
        clean.append({"product_id": pid, "qty": qty})
    return user_id, clean

def flush():
    with _lock:
        if not _buf:
            return
        rows, _buf[:] = list(_buf), []
    con = _lake()
    with _lake_lock:
        con.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?)", rows)
    print(f"[collector] flushed {len(rows)} event(s) to the lake", flush=True)

def _flusher():
    while True:
        time.sleep(FLUSH_SECS)
        try: flush()
        except Exception as e: print("[collector] flush error:", e, file=sys.stderr)

def _compactor():
    """Small-file cleanup: DuckLake gives every INSERT its own file(s), so a
    busy lake fragments the same way the old hand-rolled Parquet writer did.
    ducklake_merge_adjacent_files() is the documented fix — run it, don't
    reinvent it in Python."""
    while True:
        time.sleep(COMPACT_SECS)
        try:
            with _lake_lock:
                _lake().execute(f"CALL ducklake_merge_adjacent_files('{DUCKLAKE_DB}')")
            print("[collector] compacted the lake", flush=True)
        except Exception as e:
            print("[collector] compaction error:", e, file=sys.stderr)

def place_order(user_id, items):
    """All-or-nothing: allocate an order, decrement stock, write line items.
    Mirrors the transaction in 02_oltp.sql; the CHECK (stock >= 0) constraint
    is what turns an oversell into a rollback instead of negative inventory."""
    if not items:
        raise ValueError("cart is empty")
    con = _oltp()
    with _oltp_lock:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("INSERT INTO orders (user_id) VALUES (?)", [user_id])
            order_id = con.execute("SELECT currval('order_seq')").fetchone()[0]
            for it in items:
                pid, qty = int(it["product_id"]), int(it["qty"])
                if not con.execute("UPDATE inventory SET stock = stock - ? WHERE product_id = ? RETURNING product_id",
                                   [qty, pid]).fetchall():
                    raise ValueError(f"unknown product {pid}")
                con.execute("""
                    INSERT INTO order_items (order_id, product_id, qty, price_cents)
                    SELECT ?, product_id, ?, price_cents FROM products WHERE product_id = ?
                """, [order_id, qty, pid])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return order_id

def get_recommendations(user_id, limit=6):
    """"Shoppers like you also got" — real collaborative filtering over order
    history in oltp.duckdb: find other orders that share a product with this
    user's own orders, then rank what else showed up in those orders. Plain
    SQL, no extension needed, so it's safe to run on every request.

    Co-purchase overlap is real but sparse — plenty of users/products just
    don't share a basket with anything, even in the denser seed data — so
    this ranks genuine collaborative-filtering hits first (tier 0) and pads
    out to `limit` with best-sellers overall (tier 1) when there aren't
    enough. A user with zero order history still gets the best-seller list,
    since my_products is simply empty for them."""
    con = _oltp()
    with _oltp_lock:
        rows = con.execute("""
            WITH my_products AS (
                SELECT DISTINCT oi.product_id
                FROM orders o JOIN order_items oi USING (order_id)
                WHERE o.user_id = ?
            ),
            cohort_orders AS (
                SELECT DISTINCT oi.order_id
                FROM order_items oi JOIN my_products mp USING (product_id)
            ),
            collaborative AS (
                SELECT oi.product_id, count(DISTINCT oi.order_id) AS freq, 0 AS tier
                FROM order_items oi JOIN cohort_orders co USING (order_id)
                WHERE oi.product_id NOT IN (SELECT product_id FROM my_products)
                GROUP BY oi.product_id
            ),
            popular AS (
                SELECT oi.product_id, count(DISTINCT oi.order_id) AS freq, 1 AS tier
                FROM order_items oi
                WHERE oi.product_id NOT IN (SELECT product_id FROM my_products)
                  AND oi.product_id NOT IN (SELECT product_id FROM collaborative)
                GROUP BY oi.product_id
            ),
            ranked AS (
                SELECT * FROM collaborative
                UNION ALL
                SELECT * FROM popular
            )
            SELECT product_id FROM ranked
            ORDER BY tier, freq DESC, product_id
            LIMIT ?
        """, [user_id, limit]).fetchall()
    return [r[0] for r in rows]

def get_hybrid_recommendations(purchased_ids, limit=4):
    """"Since you brought them home" — a hybrid recommender: DuckPGQ
    co-purchase graph traversal (who else bought these, tier 0) fused with
    text-embedding similarity (tier 1 fallback, same tiered pattern as
    get_recommendations() above). Runs server-side because DuckPGQ doesn't
    load in DuckDB-WASM, unlike "Ducks like this," which stays fully
    client-side. See _setup_hybrid()."""
    if not _has_hybrid or not purchased_ids:
        return []
    con = _oltp()
    ids = ",".join(str(int(i)) for i in purchased_ids)
    limit = int(limit)
    # No `?` placeholders here, even for LIMIT — a query containing
    # GRAPH_TABLE breaks DuckDB's parameter-count check even for an
    # unrelated `?` elsewhere ("Parameter argument/count mismatch"). ids
    # and limit are both server-controlled ints (int()-cast above), not
    # user text.
    with _oltp_lock:
        rows = con.execute(f"""
            WITH graph_hits AS (
                SELECT product_id, sum(weight) AS score, 0 AS tier
                FROM (
                    FROM GRAPH_TABLE (hybrid_graph
                        MATCH (a:product)-[e:bought_with]->(b:product)
                        WHERE a.product_id IN ({ids})
                        COLUMNS (b.product_id AS product_id, e.weight AS weight)
                    )
                )
                WHERE product_id NOT IN ({ids})
                GROUP BY product_id
            ),
            similarity_hits AS (
                SELECT v2.product_id, -min(array_distance(v1.embedding, v2.embedding)) AS score, 1 AS tier
                FROM product_vectors v1
                JOIN product_vectors v2 NEAREST 20 BY DISTANCE array_distance(v1.embedding, v2.embedding)
                WHERE v1.product_id IN ({ids})
                  AND v2.product_id NOT IN ({ids})
                  AND v2.product_id NOT IN (SELECT product_id FROM graph_hits)
                GROUP BY v2.product_id
            ),
            ranked AS (SELECT * FROM graph_hits UNION ALL SELECT * FROM similarity_hits)
            SELECT product_id FROM ranked ORDER BY tier, score DESC, product_id LIMIT {limit}
        """).fetchall()
    return [r[0] for r in rows]

def get_dashboard():
    """revenue_by_category + totals — the dashboard numbers that need
    oltp.duckdb (served over quack, but the dashboard doesn't attach to it
    yet — see _oltp()/_lake()). public/dashboard.html gets funnel/retention/daily
    directly via quack instead; see its queryLakeStats()."""
    con = _oltp()
    with _oltp_lock:
        revenue = con.execute("""
            SELECT p.category, count(DISTINCT o.order_id) AS orders, sum(oi.qty) AS units,
                   round(sum(oi.qty * oi.price_cents) / 100.0, 2) AS revenue_eur
            FROM orders o JOIN order_items oi USING (order_id) JOIN products p USING (product_id)
            WHERE o.status IN ('confirmed', 'shipped')
            GROUP BY p.category ORDER BY revenue_eur DESC
        """).fetchall()

        totals = con.execute("""
            SELECT count(DISTINCT o.order_id) AS orders,
                   coalesce(sum(oi.qty * oi.price_cents), 0) / 100.0 AS revenue
            FROM orders o JOIN order_items oi USING (order_id)
            WHERE o.status IN ('confirmed', 'shipped')
        """).fetchone()

    return {
        "revenue_by_category": [
            {"category": r[0], "orders": r[1], "units": r[2], "revenue_eur": r[3]} for r in revenue
        ],
        "totals": {"orders": totals[0], "revenue_eur": round(totals[1], 2)},
    }

# ---- HTTP: static storefront + POST /collect + POST /order + GET /recommendations
# + GET /hybrid-recommendations + GET /dashboard-data

class Handler(SimpleHTTPRequestHandler):
    timeout = 15   # drop connections that stall mid-request (slowloris)

    def __init__(self, *a, **k):
        super().__init__(*a, directory=PUBLIC, **k)

    def _client_ip(self):
        """The visitor's address. Behind the TLS proxy every connection arrives
        from loopback, so in public mode trust X-Forwarded-For from a loopback
        peer — Caddy overwrites any value a client sends with the real one."""
        peer = self.client_address[0]
        if PUBLIC_MODE and peer in ("127.0.0.1", "::1"):
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return peer

    def _limited(self, route):
        """Apply the per-client and global limits for `route`. If one is hit,
        answers 429 with Retry-After and returns True."""
        waits = [_limiter.allow((route, self._client_ip()), *LIMITS[route])]
        if route in GLOBAL_LIMITS:
            waits.append(_limiter.allow((route, "*"), *GLOBAL_LIMITS[route]))
        wait = max(waits)
        if wait:
            self._json(429, {"error": "slow down — too many requests"}, {"Retry-After": str(wait)})
            return True
        return False

    def end_headers(self):
        # The dashboard's in-browser DuckDB needs threads (for quack), which
        # needs a cross-origin-isolated page. `credentialless` rather than
        # `require-corp` so its CDN scripts still load without CORP headers.
        if urlparse(self.path).path == "/dashboard.html":
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
        super().end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in LIMITS and self._limited(path):
            return
        if path == "/recommendations":
            try:
                qs = parse_qs(urlparse(self.path).query)
                user_id = int(qs.get("user_id", [""])[0])
                self._json(200, {"product_ids": get_recommendations(user_id)})
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif path == "/hybrid-recommendations":
            try:
                qs = parse_qs(urlparse(self.path).query)
                ids = [int(x) for x in qs.get("product_ids", [""])[0].split(",") if x]
                if len(ids) > MAX_ORDER_ITEMS:
                    raise ValueError(f"at most {MAX_ORDER_ITEMS} product_ids")
                self._json(200, {"product_ids": get_hybrid_recommendations(ids)})
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif path == "/config":
            self._json(200, {"quack_url": QUACK_PUBLIC_URL, "quack_token": QUACK_TOKEN})
        elif path == "/dashboard-data":
            try:
                self._json(200, get_dashboard())
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            super().do_GET()

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise HttpError(400, "bad Content-Length")
        if length < 0:
            raise HttpError(400, "bad Content-Length")
        if length > MAX_BODY:
            raise HttpError(413, f"body too large (max {MAX_BODY} bytes)")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise HttpError(400, "body is not valid JSON")

    def _json(self, status, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.rstrip("/")
        if path not in ("/collect", "/order"):
            self.send_response(404); self.end_headers()
            return
        if self._limited(path):
            return
        try:
            payload = self._body()
            if path == "/collect":
                events = payload if isinstance(payload, list) else [payload]
                if len(events) > MAX_EVENTS_PER_REQUEST:
                    raise HttpError(400, f"at most {MAX_EVENTS_PER_REQUEST} events per request")
                cleaned = [_clean_event(ev) for ev in events]   # all-or-nothing
                for fields in cleaned:
                    enqueue(None, _clean=fields)
                self.send_response(204); self.end_headers()
            else:
                user_id, items = _clean_order(payload)
                try:
                    self._json(200, {"order_id": place_order(user_id, items)})
                except Exception as e:      # out of stock, unknown product/user: the DB says no
                    print(f"[collector] order rejected: {e}", file=sys.stderr, flush=True)
                    # the storefront shows this in a toast; don't hand database internals to visitors
                    self._json(409, {"error": "not enough stock" if "stock" in str(e) else "that order couldn't be placed"})
        except HttpError as e:
            self._json(e.status, {"error": str(e)})

    def log_message(self, *a):  # keep the console quiet except for flushes
        pass

if __name__ == "__main__":
    _oltp()  # connect (+ serve over quack) right away, so startup fails loudly, not on first request
    _lake()
    threading.Thread(target=_flusher, daemon=True).start()
    threading.Thread(target=_compactor, daemon=True).start()
    print(f"[collector] storefront + tracking on http://localhost:{PORT}/  (Ctrl-C to stop)")
    try:
        ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        flush()
        if _oltp_con: _oltp_con.close()
        if _lake_con: _lake_con.close()
        print("\n[collector] flushed and stopped.")
