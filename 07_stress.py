#!/usr/bin/env python3
"""
07_stress.py — concurrent-writer stress test against the live DuckLake
catalog, over Quack.

N threads, each with its own quack connection, INSERT into the same
`events` table collector.py's live traffic writes to, all at once —
a real exercise of DuckDB's "multiple concurrent writers" claim (see the
Quack announcement blog), not just a claim taken on faith.

Writes use real event_types (pageview/add_to_cart/purchase, weighted like
real traffic) and are ordinary rows in every other respect, so a run
visibly moves the dashboard's sessions/funnel/retention/daily numbers —
seed data is dated March 2025, so a burst of writes shows up as a
genuine spike on "today," the same way real traffic would. Each row is
tagged `props: {"stress": true, "worker": N}` so public/dashboard.html's
pollStressWrites() can count them separately from organic traffic.

    uv run 07_stress.py                      # 8 workers, 10s, defaults
    uv run 07_stress.py --workers 16 --duration 5

Needs collector.py running — it's what calls quack_serve() on the DuckLake
connection (see _lake() there).
"""
import argparse, json, threading, time, random, datetime as dt

import duckdb

from collector import QUACK_TOKEN, QUACK_LAKE_PORT  # same server, no duplicated constants


# Roughly matches the seeded traffic's mix (03_lake.sql) —
# mostly pageviews, fewer carts, fewer still purchases — so a stress run
# nudges the funnel the way a real traffic burst would, not randomly.
EVENT_TYPES = ["pageview"] * 7 + ["add_to_cart"] * 2 + ["purchase"] * 1


def worker(worker_id, stop_event, counter, lock, errors):
    con = duckdb.connect()
    con.execute("INSTALL quack")
    con.execute("LOAD quack")
    con.execute(f"ATTACH 'quack:localhost:{QUACK_LAKE_PORT}' AS remote (TOKEN '{QUACK_TOKEN}')")
    base_id = 900_000_000 + worker_id * 10_000_000  # cosmetically out of range of real event_ids
    n = 0
    while not stop_event.is_set():
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)   # UTC-naive, like the collector's timestamps
        try:
            # Inlined literals, not `?` parameters: DuckDB 2.0's quack pushdown
            # forwards the INSERT without the bound values ("Values were not
            # provided for the following parameters"). Every value is
            # generated here, so there is nothing to escape.
            props = json.dumps({"stress": True, "worker": worker_id})
            con.execute(
                f"INSERT INTO remote.events VALUES ({base_id + n}, {random.randint(1, 500)}, "
                f"'{random.choice(EVENT_TYPES)}', {random.randint(1, 60)}, "
                f"TIMESTAMP '{now.isoformat(sep=' ')}', DATE '{now.date()}', '{props}')"
            )
            n += 1
            with lock:
                counter[0] += 1
        except Exception as e:
            with lock:
                errors.append(str(e))
            break
    con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=8, help="concurrent writers (default: 8)")
    ap.add_argument("--duration", type=float, default=10.0, help="seconds to hammer writes for (default: 10)")
    args = ap.parse_args()

    counter, errors, lock, stop_event = [0], [], threading.Lock(), threading.Event()
    threads = [
        threading.Thread(target=worker, args=(i, stop_event, counter, lock, errors), daemon=True)
        for i in range(args.workers)
    ]

    print(f"[stress] {args.workers} workers writing concurrently for {args.duration:.0f}s "
          f"via quack:localhost:{QUACK_LAKE_PORT} ...")
    start = time.time()
    for t in threads:
        t.start()
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        print("\n[stress] stopping early...")
    stop_event.set()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    if errors:
        print(f"[stress] {len(errors)} worker(s) hit an error, e.g.: {errors[0]}")
    print(f"[stress] {counter[0]} inserts in {elapsed:.1f}s -> {counter[0] / elapsed:.0f} writes/sec")


if __name__ == "__main__":
    main()
