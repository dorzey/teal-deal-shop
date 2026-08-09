#!/usr/bin/env python3
"""
collector.py — Puddle's single-writer event collector.

One process, one DuckDB connection to oltp.duckdb (with the DuckLake catalog
attached alongside it — see _oltp()): the same "single writer" discipline the
OLTP tier uses, now for the clickstream too. It:

  • serves the storefront from ./public  (so the tracker posts same-origin)
  • accepts POST /collect events and appends them to the DuckLake catalog,
    in the SAME schema 04_marts.sql reads — live events land in the funnel.
  • GET /dashboard-data computes that same funnel/retention/revenue live,
    straight from oltp.duckdb + the lake, for public/dashboard.html.

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
DUCKLAKE_CATALOG = "puddle_catalog.ducklake"
DUCKLAKE_DATA    = "lakehouse_data"

# ---- the single writer: both oltp.duckdb AND the DuckLake catalog ----------
# A local-file DuckLake catalog is itself just a DuckDB file, so — like
# oltp.duckdb — only one connection anywhere can have it attached at a time;
# a second attach attempt (even from a second connection in this SAME
# process) fails with a lock/handle conflict. So there's exactly one shared
# connection for orders, recommendations, the dashboard, AND event writes,
# guarded by one lock. Connects lazily so importing this module (as the
# collection-path tests do) never touches either file.
_oltp_con, _db_lock = None, threading.Lock()

def _oltp():
    global _oltp_con
    if _oltp_con is None:
        con = duckdb.connect(os.path.join(ROOT, "oltp.duckdb"))
        con.execute("INSTALL ducklake")
        con.execute(
            f"ATTACH 'ducklake:{DUCKLAKE_CATALOG}' AS lakehouse (DATA_PATH '{DUCKLAKE_DATA}/')"
        )
        _oltp_con = con
    return _oltp_con

# ---- event buffering ---------------------------------------------------------
_buf, _lock, _next_id = [], threading.Lock(), 1_000_000  # live ids start high

def _parse_ts(s):
    # tracker.js always sends a well-formed new Date().toISOString(); a
    # malformed one (e.g. a hand-crafted /collect call) surfaces as a 400
    # via do_POST's own try/except, same as any other bad payload.
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)

def enqueue(ev):
    """Normalise one tracker event into the lake schema and buffer it."""
    global _next_id
    with _lock:
        eid, _next_id = _next_id, _next_id + 1
    ts = _parse_ts(ev.get("event_ts"))
    props = dict(ev.get("props") or {})
    props["visitor_id"] = ev.get("visitor_id")
    props["session_id"] = ev.get("session_id")
    row = (
        eid,
        ev.get("user_id"),
        str(ev.get("event_type") or "pageview"),
        ev.get("product_id"),
        ts,
        ts.date(),
        json.dumps(props),
    )
    with _lock:
        _buf.append(row)
        due = len(_buf) >= BATCH
    if due:
        flush()

def flush():
    with _lock:
        if not _buf:
            return
        rows, _buf[:] = list(_buf), []
    con = _oltp()
    with _db_lock:
        con.executemany("INSERT INTO lakehouse.events VALUES (?,?,?,?,?,?,?)", rows)
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
            with _db_lock:
                _oltp().execute("CALL ducklake_merge_adjacent_files('lakehouse')")
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
    with _db_lock:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("INSERT INTO orders (user_id) VALUES (?)", [user_id])
            order_id = con.execute("SELECT currval('order_seq')").fetchone()[0]
            for it in items:
                pid, qty = int(it["product_id"]), int(it["qty"])
                con.execute("UPDATE inventory SET stock = stock - ? WHERE product_id = ?", [qty, pid])
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
    SQL, no extension needed, so it's safe to run on every request."""
    con = _oltp()
    with _db_lock:
        rows = con.execute("""
            WITH my_products AS (
                SELECT DISTINCT oi.product_id
                FROM orders o JOIN order_items oi USING (order_id)
                WHERE o.user_id = ?
            ),
            cohort_orders AS (
                SELECT DISTINCT oi.order_id
                FROM order_items oi JOIN my_products mp USING (product_id)
            )
            SELECT oi.product_id, count(DISTINCT oi.order_id) AS freq
            FROM order_items oi JOIN cohort_orders co USING (order_id)
            WHERE oi.product_id NOT IN (SELECT product_id FROM my_products)
            GROUP BY oi.product_id
            ORDER BY freq DESC, oi.product_id
            LIMIT ?
        """, [user_id, limit]).fetchall()
    return [r[0] for r in rows]

def get_dashboard():
    """Live marts, computed on demand from oltp.duckdb + the DuckLake catalog —
    same shape as 04_marts.sql's funnel/retention/revenue, just expressed as
    CTEs instead of intermediate tables, since this runs on every dashboard
    poll and shouldn't write anything to disk on a path that shares a lock
    with checkout."""
    con = _oltp()
    with _db_lock:
        funnel = con.execute("""
            WITH gaps AS (
                SELECT user_id, event_ts, event_type,
                       event_ts - LAG(event_ts) OVER (PARTITION BY user_id ORDER BY event_ts) AS gap
                FROM lakehouse.events
            ), session_events AS (
                SELECT *, SUM(CASE WHEN gap IS NULL OR gap > INTERVAL 30 MINUTE THEN 1 ELSE 0 END)
                          OVER (PARTITION BY user_id ORDER BY event_ts) AS session_num
                FROM gaps
            ), sessions AS (
                SELECT bool_or(event_type = 'add_to_cart') AS reached_cart,
                       bool_or(event_type = 'purchase')    AS reached_purchase
                FROM session_events GROUP BY user_id, session_num
            )
            SELECT count(*) AS sessions,
                   count(*) FILTER (WHERE reached_cart) AS carted,
                   count(*) FILTER (WHERE reached_purchase) AS purchased,
                   round(100.0 * count(*) FILTER (WHERE reached_cart) / count(*), 1) AS view_to_cart_pct,
                   round(100.0 * count(*) FILTER (WHERE reached_purchase)
                         / nullif(count(*) FILTER (WHERE reached_cart), 0), 1) AS cart_to_purchase_pct
            FROM sessions
        """).fetchone()

        revenue = con.execute("""
            SELECT p.category, count(DISTINCT o.order_id) AS orders, sum(oi.qty) AS units,
                   round(sum(oi.qty * oi.price_cents) / 100.0, 2) AS revenue_eur
            FROM orders o JOIN order_items oi USING (order_id) JOIN products p USING (product_id)
            WHERE o.status IN ('confirmed', 'shipped')
            GROUP BY p.category ORDER BY revenue_eur DESC
        """).fetchall()

        retention = con.execute("""
            WITH activity AS (
                SELECT DISTINCT user_id, date_trunc('week', event_ts) AS active_week FROM lakehouse.events
            ), cohort AS (
                SELECT user_id, min(active_week) AS cohort_week FROM activity GROUP BY user_id
            )
            SELECT CAST(date_diff('week', c.cohort_week, a.active_week) AS INTEGER) AS weeks_since,
                   count(DISTINCT c.user_id) AS active_users
            FROM cohort c JOIN activity a USING (user_id)
            GROUP BY 1 ORDER BY 1
        """).fetchall()

        daily = con.execute("""
            SELECT event_date::VARCHAR AS d,
                   count(*) FILTER (WHERE event_type = 'pageview')    AS pageviews,
                   count(*) FILTER (WHERE event_type = 'add_to_cart') AS carts,
                   count(*) FILTER (WHERE event_type = 'purchase')    AS purchases
            FROM lakehouse.events GROUP BY event_date ORDER BY event_date
        """).fetchall()

        totals = con.execute("""
            SELECT count(DISTINCT o.order_id) AS orders,
                   coalesce(sum(oi.qty * oi.price_cents), 0) / 100.0 AS revenue
            FROM orders o JOIN order_items oi USING (order_id)
            WHERE o.status IN ('confirmed', 'shipped')
        """).fetchone()

    return {
        "funnel": {
            "sessions": funnel[0], "carted": funnel[1], "purchased": funnel[2],
            "view_to_cart_pct": funnel[3], "cart_to_purchase_pct": funnel[4],
        },
        "revenue_by_category": [
            {"category": r[0], "orders": r[1], "units": r[2], "revenue_eur": r[3]} for r in revenue
        ],
        "retention": [{"weeks_since": r[0], "active_users": r[1]} for r in retention],
        "daily": [{"date": r[0], "pageviews": r[1], "carts": r[2], "purchases": r[3]} for r in daily],
        "totals": {"orders": totals[0], "revenue_eur": round(totals[1], 2)},
    }

# ---- HTTP: static storefront + POST /collect + POST /order + GET /recommendations + GET /dashboard-data

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=PUBLIC, **k)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/recommendations":
            try:
                qs = parse_qs(urlparse(self.path).query)
                user_id = int(qs.get("user_id", [""])[0])
                self._json(200, {"product_ids": get_recommendations(user_id)})
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif path == "/dashboard-data":
            try:
                self._json(200, get_dashboard())
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            super().do_GET()

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/collect":
            try:
                payload = self._body()
                for ev in (payload if isinstance(payload, list) else [payload]):
                    enqueue(ev)
                self.send_response(204); self.end_headers()
            except Exception as e:
                self.send_response(400); self.end_headers()
                self.wfile.write(str(e).encode())
        elif path == "/order":
            try:
                payload = self._body()
                order_id = place_order(int(payload.get("user_id")), payload.get("items") or [])
                self._json(200, {"order_id": order_id})
            except Exception as e:
                self._json(409, {"error": str(e)})
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):  # keep the console quiet except for flushes
        pass

if __name__ == "__main__":
    _oltp()  # connect + attach the lake right away, so startup fails loudly, not on first request
    threading.Thread(target=_flusher, daemon=True).start()
    threading.Thread(target=_compactor, daemon=True).start()
    print(f"[collector] storefront + tracking on http://localhost:{PORT}/  (Ctrl-C to stop)")
    try:
        ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        flush()
        if _oltp_con: _oltp_con.close()
        print("\n[collector] flushed and stopped.")
