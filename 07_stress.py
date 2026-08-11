#!/usr/bin/env python3
"""
07_stress.py — concurrent-writer stress test against the live DuckLake
catalog, over Quack.

Every other write path in Puddle funnels through collector.py's single
connection (see its own docstring) — that's the honest way to run DuckDB.
This script deliberately does the opposite: N independent processes-worth
of threads, each with their own quack connection, all INSERTing into the
same `events` table at once, to see what DuckDB's "multiple concurrent
writers" claim (see the Quack announcement blog) actually looks like in
practice rather than take it on faith.

Writes use real event_types (pageview/add_to_cart/purchase, roughly
matching real traffic's mix) and land in the exact same `events` table
collector.py's own live traffic does — on purpose: the whole point is to
watch the dashboard's actual sessions/funnel/retention/daily numbers move
live while this runs, not just a bespoke counter off to the side. (First
version used a separate 'stress_write' type kept out of every real metric —
technically clean, but pointless: readers couldn't see the writes land
anywhere. Real types it is; a stress run visibly nudges the funnel and
adds a retention blip for "today," and that's honest — it's exactly what
a real traffic burst against this seed data would do too.) Each row is
still tagged `props: {"stress": true, "worker": N}` so it can be counted
separately — see public/dashboard.html's pollStressWrites(), which filters
on that instead of event_type.

    python3 07_stress.py                      # 8 workers, 10s, defaults
    python3 07_stress.py --workers 16 --duration 5

Needs collector.py running (it's what calls quack_serve() on the DuckLake
connection — see _lake() there).
"""
import argparse, json, threading, time, random, datetime as dt

import duckdb

from collector import QUACK_TOKEN, QUACK_LAKE_PORT  # same server, no duplicated constants


# Roughly matches real traffic's mix (see README's funnel engineering note) —
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
        now = dt.datetime.now()
        try:
            con.execute(
                "INSERT INTO remote.events VALUES (?,?,?,?,?,?,?)",
                [
                    base_id + n,
                    random.randint(1, 500),  # real seeded user_id range, so oltp.users joins still work
                    random.choice(EVENT_TYPES),
                    random.randint(1, 60),   # real seeded product_id range
                    now,
                    now.date(),
                    json.dumps({"stress": True, "worker": worker_id}),
                ],
            )
            n += 1
            with lock:
                counter[0] += 1
        except Exception as e:
            with lock:
                errors.append(str(e))
            break


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
