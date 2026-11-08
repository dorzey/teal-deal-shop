"""Tests for collector.py's request validation and rate limiting.

Importing collector never touches the databases (they connect lazily), so these
need no build and no network:   make test   (or: uv run python -m unittest)
"""
import datetime as dt
import unittest

import collector as c


def event(**over):
    ev = {
        "event_type": "pageview", "product_id": 3, "user_id": None,
        "event_ts": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "visitor_id": "v1", "session_id": "s1", "props": {"path": "/"},
    }
    ev.update(over)
    return ev


class CleanEvent(unittest.TestCase):
    def test_accepts_what_the_tracker_sends(self):
        user_id, event_type, product_id, ts, day, props = c._clean_event(event())
        self.assertEqual((user_id, event_type, product_id), (None, "pageview", 3))
        self.assertEqual(day, ts.date())
        self.assertIn('"visitor_id": "v1"', props)

    def test_missing_event_type_defaults_to_pageview(self):
        self.assertEqual(c._clean_event(event(event_type=None))[1], "pageview")

    def test_rejects_bad_input(self):
        for bad in [
            event(event_type="rm -rf"),
            event(product_id="3"), event(product_id=True), event(user_id=1.5),
            event(event_ts="yesterday"), event(event_ts="2001-01-01T00:00:00Z"),
            event(visitor_id="x" * 65), event(visitor_id=7),
            event(props=["a"]), event(props={str(i): i for i in range(21)}),
            event(props={"blob": "x" * 5000}),
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


if __name__ == "__main__":
    unittest.main()
