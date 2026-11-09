"""Tests for collector.py: request validation, rate limiting, client-IP handling, the HTTP
layer (headers, CSP, HEAD, asset paths), the flush fallback, the inventory triggers and the
hybrid recommender (against an in-memory copy of 01_seed.sql), and the public quack lock-down
(a real quack server over a temporary DuckLake).

Importing collector never touches the databases on disk (they connect lazily), so
these need no build and no network. The DuckPGQ and quack lock-down tests need those
extensions installed (./run.sh does that once) and are skipped on a machine without them:   make test   (or: uv run python -m unittest)
"""
import datetime as dt
import unittest

import collector as c


def event(**over):
    ev = {
        "event_type": "pageview", "product_id": 3, "user_id": None,
        "event_ts": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "session_id": "s1", "props": {"path": "/"},
    }
    ev.update(over)
    return ev


class CleanEvent(unittest.TestCase):
    def test_accepts_what_the_tracker_sends(self):
        user_id, event_type, product_id, ts, day, props = c._clean_event(event())
        self.assertEqual((user_id, event_type, product_id), (None, "pageview", 3))
        self.assertEqual(day, ts.date())
        self.assertIn('"session_id": "s1"', props)

    def test_no_visitor_id_is_stored_even_if_a_client_sends_one(self):
        props = c._clean_event(event(visitor_id="v1"))[5]
        self.assertNotIn("visitor", props)

    def test_only_the_trackers_props_are_kept_and_long_ones_are_cut(self):
        import json
        props = json.loads(c._clean_event(event(props={
            "path": "/", "referrer": "r" * 5000, "device": "mobile",
            "blob": "x" * 5000, "nested": {"a": [1, 2]}, "n": float("nan")}))[5])
        self.assertEqual(set(props), {"path", "referrer", "device", "session_id"})
        self.assertEqual(len(props["referrer"]), c.PROP_MAX_CHARS)

    def test_non_ascii_text_is_fine(self):
        self.assertIn("🦆", c._clean_event(event(props={"path": "/🦆"}))[5])

    def test_missing_event_type_defaults_to_pageview(self):
        self.assertEqual(c._clean_event(event(event_type=None))[1], "pageview")

    def test_rejects_bad_input(self):
        for bad in [
            event(event_type="rm -rf"),
            event(product_id="3"), event(product_id=True), event(user_id=1.5),
            event(event_ts="yesterday"), event(event_ts="2001-01-01T00:00:00Z"),
            event(session_id="x" * 65), event(session_id=7),
            event(props=["a"]), event(props={str(i): i for i in range(21)}),
            # values that pass type checks but would make DuckDB fail a whole batch insert
            event(user_id=10**30), event(product_id=-10**30), event(product_id=2**40),
            event(props={"path": "\ud800"}), event(session_id="\ud800"),
            "not an object", None, [],
        ]:
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(c.HttpError) as cm:
                    c._clean_event(bad)
                self.assertEqual(cm.exception.status, 400)

    def test_enqueue_sheds_load_when_the_buffer_is_full(self):
        saved = list(c._buf)
        try:
            c._buf[:] = [None] * c.MAX_BUFFER
            with self.assertRaises(c.HttpError) as cm:
                c.enqueue(event())
            self.assertEqual(cm.exception.status, 503)
        finally:
            c._buf[:] = saved


class CleanOrder(unittest.TestCase):
    def test_accepts_a_normal_order(self):
        user_id, items = c._clean_order({"user_id": 7, "items": [{"product_id": 3, "qty": 2}]})
        self.assertEqual((user_id, items), (7, [{"product_id": 3, "qty": 2}]))

    def test_rejects_orders_that_would_add_stock_or_flood(self):
        ok = {"product_id": 3, "qty": 1}
        for bad in [
            {"user_id": 7, "items": [{"product_id": 3, "qty": -5}]},      # negative = restock
            {"user_id": 7, "items": [{"product_id": 3, "qty": 0}]},
            {"user_id": 7, "items": [{"product_id": 3, "qty": c.MAX_ORDER_QTY + 1}]},
            {"user_id": 7, "items": [ok] * (c.MAX_ORDER_ITEMS + 1)},
            {"user_id": 7, "items": []}, {"user_id": 7},
            {"user_id": 0, "items": [ok]}, {"user_id": "7", "items": [ok]}, {"items": [ok]},
            {"user_id": 7, "items": [{"product_id": "3", "qty": 1}]},
            {"user_id": 7, "items": ["3"]},
            [], None,
        ]:
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(c.HttpError) as cm:
                    c._clean_order(bad)
                self.assertEqual(cm.exception.status, 400)


class VersionAssets(unittest.TestCase):
    def test_local_assets_get_a_content_hash_and_others_are_untouched(self):
        out = c.version_assets('<script src="xray.js" defer></script><use href="ducks.svg#x"/><script src="https://cdn/x.js">')
        self.assertRegex(out, r'src="/a/[0-9a-f]{10}/xray\.js"')
        self.assertRegex(out, r'href="/a/[0-9a-f]{10}/ducks\.svg#x"')
        self.assertIn('src="https://cdn/x.js"', out)
        self.assertNotIn("?v=", out)   # a CDN may ignore query strings

    def test_asset_paths_only_match_known_files(self):
        self.assertTrue(c.ASSET_PATH.match("/a/0123456789/tracker.js"))
        for bad in ("/a/xyz/tracker.js", "/a/0123456789/../collector.py", "/a/0123456789/collector.py"):
            self.assertIsNone(c.ASSET_PATH.match(bad))

class RateLimiter(unittest.TestCase):
    def test_allows_a_burst_then_refuses_with_a_retry_hint(self):
        rl = c.RateLimiter()
        self.assertEqual([rl.allow("k", 60, 3) for _ in range(3)], [0, 0, 0])
        wait = rl.allow("k", 60, 3)
        self.assertGreaterEqual(wait, 1)

    def test_clients_are_independent(self):
        rl = c.RateLimiter()
        for _ in range(3):
            rl.allow("a", 60, 3)
        self.assertGreater(rl.allow("a", 60, 3), 0)
        self.assertEqual(rl.allow("b", 60, 3), 0)

    def test_tokens_refill_over_time(self):
        rl = c.RateLimiter()
        for _ in range(2):
            rl.allow("k", 6000, 2)          # 100 tokens/second
        self.assertGreater(rl.allow("k", 6000, 2), 0)
        import time; time.sleep(0.05)       # ~5 tokens
        self.assertEqual(rl.allow("k", 6000, 2), 0)


class ClientIp(unittest.TestCase):
    """_client_ip feeds the rate limits, so spoofing it defeats them."""

    @staticmethod
    def request(peer, **headers):
        from http.client import HTTPMessage
        from types import SimpleNamespace
        msg = HTTPMessage()
        for k, v in headers.items():
            msg[k.replace("_", "-")] = v
        return SimpleNamespace(client_address=(peer, 40000), headers=msg)

    def ip(self, req, public=True, header="X-Forwarded-For"):
        from unittest import mock
        with mock.patch.object(c, "PUBLIC_MODE", public), mock.patch.object(c, "CLIENT_IP_HEADER", header):
            return c.Handler._client_ip(req)

    def test_behind_cloudflare_the_cf_header_wins_and_a_spoofed_xff_is_ignored(self):
        req = self.request("127.0.0.1", CF_Connecting_IP="203.0.113.9", X_Forwarded_For="6.6.6.6, 203.0.113.9")
        self.assertEqual(self.ip(req, header="CF-Connecting-IP"), "203.0.113.9")

    def test_behind_caddy_xff_is_used(self):
        req = self.request("::1", X_Forwarded_For="198.51.100.4")
        self.assertEqual(self.ip(req), "198.51.100.4")

    def test_a_missing_header_falls_back_to_the_peer(self):
        self.assertEqual(self.ip(self.request("127.0.0.1"), header="CF-Connecting-IP"), "127.0.0.1")

    def test_the_header_is_ignored_unless_the_peer_is_the_proxy_on_loopback(self):
        req = self.request("192.0.2.50", X_Forwarded_For="1.2.3.4")
        self.assertEqual(self.ip(req), "192.0.2.50")

    def test_outside_public_mode_the_header_is_ignored(self):
        req = self.request("127.0.0.1", X_Forwarded_For="1.2.3.4")
        self.assertEqual(self.ip(req, public=False), "127.0.0.1")


class RateLimiterMore(unittest.TestCase):
    def test_cost_spends_that_many_tokens(self):
        rl = c.RateLimiter()
        self.assertEqual(rl.allow("k", 60, 10, cost=10), 0)
        self.assertGreaterEqual(rl.allow("k", 60, 10, cost=1), 1)

    def test_a_request_bigger_than_the_burst_is_never_allowed(self):
        self.assertGreaterEqual(c.RateLimiter().allow("k", 60, 5, cost=6), 1)

    def test_buckets_that_have_refilled_are_forgotten(self):
        from unittest import mock
        rl = c.RateLimiter()
        now = [1000.0]
        with mock.patch.object(c.time, "monotonic", lambda: now[0]):
            rl = c.RateLimiter()
            for i in range(1000):
                rl.allow(("route", f"10.0.{i // 250}.{i % 250}"), 60, 3)
            self.assertEqual(len(rl._buckets), 1000)
            now[0] += 3600                       # everyone's bucket is full again
            rl.allow(("route", "someone-new"), 60, 3)   # triggers the periodic prune
            self.assertEqual(len(rl._buckets), 1)

    def test_a_bucket_still_draining_is_kept(self):
        from unittest import mock
        now = [1000.0]
        with mock.patch.object(c.time, "monotonic", lambda: now[0]):
            rl = c.RateLimiter()
            for _ in range(10):
                rl.allow("busy", 1, 10)          # empty, and refilling at 1 token a minute
            now[0] += 301                        # long enough to trigger the prune, not to refill (5 of 10)
            rl.allow("other", 1, 10)
            self.assertIn("busy", rl._buckets)


class IpKey(unittest.TestCase):
    def test_ipv4_is_unchanged(self):
        self.assertEqual(c._ip_key("203.0.113.9"), "203.0.113.9")

    def test_ipv6_addresses_in_one_slash_64_share_a_key(self):
        self.assertEqual(c._ip_key("2001:db8:1:2::1"), c._ip_key("2001:db8:1:2:ffff:ffff:ffff:ffff"))
        self.assertNotEqual(c._ip_key("2001:db8:1:2::1"), c._ip_key("2001:db8:1:3::1"))

    def test_ipv4_mapped_ipv6_is_the_ipv4_address(self):
        self.assertEqual(c._ip_key("::ffff:203.0.113.9"), "203.0.113.9")

    def test_garbage_is_passed_through_not_crashed_on(self):
        self.assertEqual(c._ip_key("not an ip"), "not an ip")


class QuackReadOnly(unittest.TestCase):
    """The public lake endpoint's statement filter: reads pass, anything that writes,
    reads files or changes the lake is refused. Pure regex, so an in-memory DuckDB is enough."""
    @classmethod
    def setUpClass(cls):
        import duckdb
        cls.con = duckdb.connect()
        cls.con.execute(c.QUACK_READ_ONLY_SQL)

    def allowed(self, q):
        return self.con.execute("SELECT quack_read_only(NULL, ?)", [q]).fetchone()[0]

    def test_what_the_dashboard_and_attach_need_is_allowed(self):
        for q in ["SELECT count(*) FROM remote.events",
                  "  select * from events where event_type = 'pageview'",
                  "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
                  "FROM events SELECT event_type",
                  "BEGIN TRANSACTION", "COMMIT", "ROLLBACK",
                  "SELECT * FROM events AT (VERSION => 2)",
                  "SELECT * FROM events AT (TIMESTAMP => now())",
                  "SELECT * FROM lake.snapshots()", "SELECT * FROM lake.current_snapshot()",
                  "DESCRIBE events", "EXPLAIN SELECT 1"]:
            with self.subTest(q=q):
                self.assertTrue(self.allowed(q))

    def test_writes_and_ddl_are_refused(self):
        for q in ["INSERT INTO events SELECT * FROM events", "UPDATE events SET user_id = 1",
                  "DELETE FROM events", "DROP TABLE events", "CREATE TABLE x AS SELECT 1",
                  "ALTER TABLE events ADD COLUMN x INT", "COPY events TO '/tmp/x.csv'",
                  "ATTACH '/tmp/y.duckdb' AS y", "INSTALL httpfs", "LOAD httpfs", "SET memory_limit = '1TB'",
                  "PRAGMA database_list", "CALL ducklake_merge_adjacent_files('lake')",
                  "WITH d AS (DELETE FROM events RETURNING *) SELECT * FROM d",
                  "SELECT 1; DROP TABLE events"]:
            with self.subTest(q=q):
                self.assertFalse(self.allowed(q))

    def test_file_reading_functions_are_refused(self):
        for q in ["SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM read_json_auto('/etc/passwd')",
                  "SELECT * FROM read_text('/etc/passwd')", "SELECT * FROM parquet_scan('/x.parquet')",
                  "SELECT * FROM glob('/*')", "SELECT * FROM sniff_csv('/etc/passwd')",
                  "SELECT * FROM query('DELETE FROM events')", "SELECT * FROM query_table('events')"]:
            with self.subTest(q=q):
                self.assertFalse(self.allowed(q))

    def test_ducklake_maintenance_is_refused_by_either_name(self):
        for q in ["SELECT * FROM ducklake_expire_snapshots('lake', versions => [1])",
                  "SELECT * FROM ducklake_cleanup_old_files('lake', cleanup_all => true)",
                  "SELECT * FROM ducklake_set_option('lake', 'parquet_compression', 'zstd')",
                  "SELECT * FROM ducklake_delete_orphaned_files('lake')",
                  'SELECT * FROM "ducklake_expire_snapshots"(\'lake\')',
                  "SELECT * FROM lake.set_option('parquet_compression', 'zstd')",
                  "FROM lake.merge_adjacent_files()",
                  "SELECT * FROM lake.expire_snapshots(versions => [1])",
                  "SELECT * FROM lake.rewrite_data_files('events')",
                  "SELECT * FROM LAKE.SET_OPTION('a', 'b')"]:
            with self.subTest(q=q):
                self.assertFalse(self.allowed(q))


class Flush(unittest.TestCase):
    """flush() against an in-memory `events` table: one bad row must not cost its neighbours."""
    def setUp(self):
        import duckdb
        self.con = duckdb.connect()
        self.con.execute("""CREATE TABLE events (event_id BIGINT, user_id BIGINT, event_type VARCHAR,
                            product_id BIGINT, event_ts TIMESTAMP, event_date DATE, props JSON)""")
        self.saved = (c._lake_con, list(c._buf), c._lake_rows)
        c._lake_con, c._buf[:], c._lake_rows = self.con, [], 0
        import contextlib, io
        self._quiet = contextlib.redirect_stdout(io.StringIO())   # flush() prints a status line
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)
        c._lake_con, c._buf[:], c._lake_rows = self.saved[0], self.saved[1], self.saved[2]

    def row(self, eid, props='{"path": "/"}'):
        import datetime as dt
        ts = dt.datetime(2026, 1, 1, 12)
        return (eid, 1, "pageview", 3, ts, ts.date(), props)

    def test_a_good_batch_is_stored_and_counted(self):
        c._buf[:] = [self.row(1), self.row(2)]
        c.flush()
        self.assertEqual(self.con.execute("SELECT count(*) FROM events").fetchone()[0], 2)
        self.assertEqual(c._lake_rows, 2)

    def test_one_bad_row_costs_only_itself(self):
        import contextlib, io
        c._buf[:] = [self.row(1), self.row(2, props="{not json"), self.row(3)]
        with contextlib.redirect_stderr(io.StringIO()) as log:   # flush() reports what it dropped
            c.flush()                                      # must not raise
        self.assertIn("dropped a bad event", log.getvalue())
        ids = sorted(r[0] for r in self.con.execute("SELECT event_id FROM events").fetchall())
        self.assertEqual(ids, [1, 3])
        self.assertEqual(c._lake_rows, 2)
        self.assertEqual(c._buf, [])

    def test_a_full_lake_refuses_new_events(self):
        c._lake_rows = c.MAX_LAKE_ROWS
        with self.assertRaises(c.HttpError) as cm:
            c.enqueue(event())
        self.assertEqual(cm.exception.status, 503)


class HttpSmoke(unittest.TestCase):
    """The HTTP layer, against a real server on a free port. Only routes that need no
    database: pages, assets, /config."""
    @classmethod
    def setUpClass(cls):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        cls.http = http.client
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), c.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path):
        conn = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r, body

    def test_pages_carry_a_content_security_policy_and_the_security_headers(self):
        for path in ("/", "/dashboard.html"):
            with self.subTest(path=path):
                r, body = self.request("GET", path)
                self.assertEqual(r.status, 200)
                csp = r.getheader("Content-Security-Policy")
                self.assertIn("default-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertIn("object-src 'none'", csp)
                self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
                self.assertIn(b"<html", body.lower())

    def test_only_the_dashboard_is_cross_origin_isolated(self):
        self.assertEqual(self.request("GET", "/dashboard.html")[0].getheader("Cross-Origin-Opener-Policy"), "same-origin")
        self.assertIsNone(self.request("GET", "/")[0].getheader("Cross-Origin-Opener-Policy"))

    def test_head_gets_the_same_headers_as_get_and_no_body(self):
        for path in ("/", "/dashboard.html", "/config"):
            with self.subTest(path=path):
                got, body = self.request("GET", path)
                head, head_body = self.request("HEAD", path)
                self.assertEqual(head.status, 200)
                self.assertEqual(head_body, b"")
                self.assertEqual(head.getheader("Content-Length"), str(len(body)))
                self.assertEqual(head.getheader("Content-Type"), got.getheader("Content-Type"))

    def test_config_hands_out_the_quack_address(self):
        import json
        r, body = self.request("GET", "/config")
        self.assertEqual(set(json.loads(body)), {"quack_url", "quack_token"})

    def test_hashed_asset_paths_are_cached_forever_and_only_for_known_files(self):
        import re
        html = self.request("GET", "/")[1].decode()
        path = re.search(r'src="(/a/[0-9a-f]{10}/tracker\.js)"', html).group(1)
        r, body = self.request("GET", path)
        self.assertEqual(r.status, 200)
        self.assertIn("immutable", r.getheader("Cache-Control"))
        self.assertIn(b"sendBeacon", body)
        for bad in ("/a/0123456789/collector.py", "/a/0123456789/../../collector.py", "/a/xyz/tracker.js"):
            with self.subTest(path=bad):
                self.assertEqual(self.request("GET", bad)[0].status, 404)

    def test_the_server_does_not_expose_the_source_or_databases(self):
        for path in ("/collector.py", "/oltp.duckdb", "/../collector.py", "/.env"):
            with self.subTest(path=path):
                self.assertNotEqual(self.request("GET", path)[0].status, 200)

    def test_quack_origin_follows_the_configured_address(self):
        saved = c.QUACK_PUBLIC_URL
        try:
            for url, origin in [("quack:localhost:8789", "http://localhost:8789"),
                                ("quack:tealdeal.dorzey.net:443", "https://tealdeal.dorzey.net"),
                                ("quack:teal-deal.test:8443", "https://teal-deal.test:8443"),
                                ("quack:[::1]:8789", "http://[::1]:8789"), ("nonsense", None)]:
                c.QUACK_PUBLIC_URL = url
                self.assertEqual(c._quack_origin(), origin, url)
                if origin:
                    self.assertIn(origin, c._csp())
        finally:
            c.QUACK_PUBLIC_URL = saved


class OrderErrorMessage(unittest.TestCase):
    def test_an_oversell_says_not_enough_stock_and_other_failures_stay_generic(self):
        import duckdb
        con = duckdb.connect()
        con.execute("CREATE TABLE inventory (product_id INT PRIMARY KEY, stock INT CHECK (stock >= 0))")
        con.execute("INSERT INTO inventory VALUES (1, 5)")
        with self.assertRaises(duckdb.ConstraintException) as oversell:
            con.execute("UPDATE inventory SET stock = stock - 9 WHERE product_id = 1")
        self.assertEqual(c._order_error_message(oversell.exception), "not enough stock")
        with self.assertRaises(duckdb.ConstraintException) as dup:
            con.execute("INSERT INTO inventory VALUES (1, 1)")
        self.assertEqual(c._order_error_message(dup.exception), "that order couldn't be placed")
        self.assertEqual(c._order_error_message(ValueError("unknown product 9999")), "that order couldn't be placed")


def _extension_available(*names):
    """True if these DuckDB extensions can be LOADed here (installed already). The tests that
    need them are skipped, not failed, on a machine that has never run ./run.sh."""
    import duckdb
    con = duckdb.connect()
    try:
        for n in names:
            con.execute(f"LOAD {n}")
        return True
    except Exception:
        return False
    finally:
        con.close()


@unittest.skipUnless(_extension_available("duckpgq"), "duckpgq is not installed (run ./run.sh once)")
class HybridRecommendations(unittest.TestCase):
    """The "Since you brought them home" query: DuckPGQ graph walk + NEAREST similarity."""
    @classmethod
    def setUpClass(cls):
        import duckdb
        cls.con = duckdb.connect()
        with open("01_seed.sql") as f:
            cls.con.execute(f.read())
        cls.saved = (c._oltp_con, c._has_hybrid)
        c._oltp_con = cls.con
        c._setup_hybrid(cls.con)
        c._has_hybrid = True

    @classmethod
    def tearDownClass(cls):
        c._oltp_con, c._has_hybrid = cls.saved
        cls.con.close()

    def test_returns_other_products_never_the_ones_just_bought(self):
        got = c.get_hybrid_recommendations([3, 5, 10])
        self.assertEqual(len(got), 4)
        self.assertFalse({3, 5, 10} & set(got))
        self.assertEqual(len(set(got)), 4)
        known = {r[0] for r in self.con.execute("SELECT product_id FROM products").fetchall()}
        self.assertTrue(set(got) <= known)

    def test_is_deterministic(self):
        self.assertEqual(c.get_hybrid_recommendations([40]), c.get_hybrid_recommendations([40]))

    def test_nothing_bought_means_nothing_suggested(self):
        self.assertEqual(c.get_hybrid_recommendations([]), [])

    def test_the_limit_is_respected(self):
        self.assertEqual(len(c.get_hybrid_recommendations([3], limit=2)), 2)


@unittest.skipUnless(_extension_available("ducklake", "quack"), "ducklake/quack are not installed (run ./run.sh once)")
class PublicLakeLockdown(unittest.TestCase):
    """_lock_down_lake end to end: a real quack server over a real (temporary) DuckLake, and
    a client attacking it the way the audit did."""
    PORT = 18789

    @classmethod
    def setUpClass(cls):
        import duckdb
        import os
        import tempfile
        cls.cwd = os.getcwd()
        cls.tmp = tempfile.TemporaryDirectory()
        os.chdir(cls.tmp.name)            # DuckLake's data path is relative to the working directory
        seed = duckdb.connect()
        seed.execute("LOAD ducklake")
        seed.execute("ATTACH 'ducklake:lock.ducklake' AS lk (DATA_PATH 'lakehouse_data/')")
        seed.execute("CREATE TABLE lk.events (event_id BIGINT, event_type VARCHAR)")
        seed.execute("INSERT INTO lk.events VALUES (1, 'pageview'), (2, 'add_to_cart')")
        seed.close()
        cls.server = duckdb.connect("ducklake:lock.ducklake")
        cls.server.execute("LOAD quack")
        c._lock_down_lake(cls.server)     # the real function, before serving as in _lake()
        cls.server.execute(f"SELECT * FROM quack_serve('quack://127.0.0.1:{cls.PORT}', token := 'testtoken', disable_ssl := true)")
        cls.server.execute("SET enable_external_access = false")
        cls.client = duckdb.connect()
        cls.client.execute("LOAD quack")
        cls.client.execute(f"ATTACH 'quack:127.0.0.1:{cls.PORT}' AS r (TOKEN 'testtoken')")

    @classmethod
    def tearDownClass(cls):
        import os
        cls.client.close()
        cls.server.close()
        os.chdir(cls.cwd)
        cls.tmp.cleanup()

    def remote(self, sql):
        return self.client.execute("SELECT * FROM quack_query_by_name('r', ?)", [sql]).fetchall()

    def test_reads_work(self):
        self.assertEqual(self.remote("SELECT count(*) FROM events"), [(2,)])
        self.assertEqual(self.client.execute("SELECT count(*) FROM r.events").fetchone(), (2,))

    def test_writes_and_maintenance_are_refused_and_nothing_changes(self):
        attacks = ["INSERT INTO events VALUES (9, 'x')", "DELETE FROM events", "DROP TABLE events",
                   "SELECT * FROM ducklake_expire_snapshots('lock', versions => [1])",
                   "SELECT * FROM ducklake_set_option('lock', 'parquet_compression', 'zstd')",
                   "SELECT * FROM lock.set_option('parquet_compression', 'zstd')",
                   "SELECT * FROM lock.merge_adjacent_files()",
                   "SELECT * FROM read_json_auto('/etc/passwd')", "COPY events TO 'x.csv'"]
        snapshots = lambda: self.server.execute("SELECT count(*) FROM ducklake_snapshots('lock')").fetchone()
        before = snapshots()
        for sql in attacks:
            with self.subTest(sql=sql):
                with self.assertRaises(Exception) as cm:
                    self.remote(sql)
                self.assertIn("Authorization failed", str(cm.exception))
        self.assertEqual(self.remote("SELECT count(*) FROM events"), [(2,)])
        self.assertEqual(snapshots(), before)        # no snapshot was expired, none was added

    def test_a_query_over_the_memory_limit_fails_instead_of_taking_the_process_down(self):
        with self.assertRaises(Exception) as cm:
            self.remote("SELECT list(i) FROM range(1000000000) t(i)")
        self.assertIn("Out of Memory", str(cm.exception))
        self.assertEqual(self.remote("SELECT count(*) FROM events"), [(2,)])   # still answering


class Restock(unittest.TestCase):
    """The inventory triggers from 01_seed.sql, exercised through place_order."""

    @classmethod
    def setUpClass(cls):
        import duckdb, os
        cls.con = duckdb.connect()
        with open(os.path.join(os.path.dirname(__file__), "01_seed.sql")) as f:
            cls.con.execute(f.read())
        c._oltp_con = cls.con            # place_order() uses whatever _oltp() holds

    @classmethod
    def tearDownClass(cls):
        c._oltp_con = None
        cls.con.close()

    def stock(self, pid):
        return self.con.execute("SELECT stock FROM inventory WHERE product_id = ?", [pid]).fetchone()[0]

    def pick(self, order_by):
        return self.con.execute(f"SELECT product_id, stock FROM inventory ORDER BY {order_by} LIMIT 1").fetchone()

    def test_an_order_that_runs_a_product_low_restocks_it_and_audits_both_steps(self):
        pid, stock = self.pick("stock, product_id")
        self.assertGreaterEqual(stock, 3)
        c.place_order(7, [{"product_id": pid, "qty": stock - 1}])     # leaves 1 on the shelf
        self.assertEqual(self.stock(pid), 40)
        audit = self.con.execute(
            "SELECT old_stock, new_stock FROM stock_audit WHERE product_id = ? ORDER BY changed_at", [pid]).fetchall()
        self.assertEqual(audit, [(stock, 1), (1, 40)])

    def test_a_product_with_plenty_of_stock_is_just_decremented(self):
        pid, stock = self.pick("stock DESC, product_id")
        c.place_order(7, [{"product_id": pid, "qty": 2}])
        self.assertEqual(self.stock(pid), stock - 2)

    def test_only_the_products_in_the_order_are_restocked(self):
        low, other = [r[0] for r in self.con.execute(
            "SELECT product_id FROM inventory ORDER BY stock, product_id LIMIT 2").fetchall()]
        before = self.stock(other)
        c.place_order(7, [{"product_id": low, "qty": self.stock(low) - 1}])
        self.assertEqual(self.stock(other), before)

    def test_overselling_still_fails_and_changes_nothing(self):
        pid, stock = self.pick("stock DESC, product_id")
        orders = self.con.execute("SELECT count(*) FROM orders").fetchone()[0]
        with self.assertRaises(Exception):
            c.place_order(7, [{"product_id": pid, "qty": stock + 1}])
        self.assertEqual(self.stock(pid), stock)
        self.assertEqual(self.con.execute("SELECT count(*) FROM orders").fetchone()[0], orders)

    def test_unknown_product_is_rejected_without_leaving_an_empty_order(self):
        orders = self.con.execute("SELECT count(*) FROM orders").fetchone()[0]
        with self.assertRaises(ValueError):
            c.place_order(7, [{"product_id": 99999, "qty": 1}])
        self.assertEqual(self.con.execute("SELECT count(*) FROM orders").fetchone()[0], orders)


if __name__ == "__main__":
    unittest.main()
