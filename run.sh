#!/usr/bin/env bash
# Puddle — build the whole toy platform from scratch, then serve it.
# Override the binary with:  DUCKDB=/path/to/duckdb ./run.sh
# When this finishes building, it starts the live server itself — no extra
# steps. Ctrl-C stops it (it flushes cleanly on exit). Re-running ./run.sh
# stops any server left over from a previous run first, so it's always safe
# to just run it again.
set -euo pipefail
DUCKDB="${DUCKDB:-duckdb}"
cd "$(dirname "$0")"

echo "==> uv sync (Python deps, pinned duckdb==2.0.0)"
uv sync

echo "==> reset"
pkill -INT -f "python3 collector.py" 2>/dev/null || true   # SIGINT so it flushes + closes cleanly, like Ctrl-C
rm -f oltp.duckdb marts.duckdb puddle_catalog.ducklake
rm -rf lakehouse_data && mkdir -p public

echo "==> 01  seed OLTP (schema, constraints, synthetic data)"
"$DUCKDB" oltp.duckdb -f 01_seed.sql

echo "==> 02  place an order (all-or-nothing transaction)"
"$DUCKDB" oltp.duckdb -f 02_oltp.sql

echo "==> 03  clickstream -> DuckLake catalog (needs network the first time)"
"$DUCKDB" oltp.duckdb -f 03_lake.sql

echo "==> --  export product catalog for the browser demo"
"$DUCKDB" oltp.duckdb -c "COPY (
  SELECT p.product_id, p.name, p.category,
         round(p.price_cents/100.0,2) AS price_eur, i.stock, (i.stock>0) AS in_stock,
         round(3.0+((p.product_id*37)%20)/10.0,1) AS rating,
         ((p.product_id*53)%500) AS popularity, p.description
  FROM products p JOIN inventory i USING (product_id) ORDER BY p.product_id
) TO 'public/catalog.parquet' (FORMAT PARQUET);"

echo "==> 04  build marts (attaches OLTP read-only + the DuckLake catalog)"
"$DUCKDB" marts.duckdb -f 04_marts.sql

echo "==> 05  extensions showcase: fts / vss / spatial / DuckPGQ (best effort — needs network)"
if ! "$DUCKDB" oltp.duckdb -f 05_extras.sql; then
  echo "    05_extras.sql failed (likely no network/extension access) — continuing without it"
fi

echo "==> 06  explore the lake's snapshots + time travel (best effort — needs network)"
if ! "$DUCKDB" -f 06_ducklake.sql; then
  echo "    06_ducklake.sql failed (likely no network/extension access) — continuing without it"
fi

cat <<EOF

Built: oltp.duckdb, marts.duckdb, puddle_catalog.ducklake (+ lakehouse_data/), public/catalog.parquet

==> starting the live storefront + tracking + Duck HQ dashboard
        http://localhost:8787/                  storefront
        http://localhost:8787/dashboard.html    live dashboard
    Ctrl-C to stop (flushes cleanly on exit)
EOF

exec uv run collector.py
