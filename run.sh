#!/usr/bin/env bash
# Puddle — build the whole toy platform from scratch.
# Override the binary with:  DUCKDB=/path/to/duckdb ./run.sh
set -euo pipefail
DUCKDB="${DUCKDB:-duckdb}"
cd "$(dirname "$0")"

echo "==> reset"
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

cat <<EOF

Built: oltp.duckdb, marts.duckdb, puddle_catalog.ducklake (+ lakehouse_data/), public/catalog.parquet

Next:
  • Explore the lake's snapshots + time travel:
        $DUCKDB -f 06_ducklake.sql
  • Extensions showcase (fts / vss / spatial / DuckPGQ):
        $DUCKDB oltp.duckdb -f 05_extras.sql          # needs network for extensions
  • Browser catalog search + live tracking (DuckDB-WASM):
        python3 collector.py                          # then open http://localhost:8787
  • Duck HQ — live funnel/revenue/retention dashboard, no rebuild needed:
        (same collector.py)                           # then open http://localhost:8787/dashboard.html

NOTE: only one process can have the DuckLake catalog attached at a time —
stop collector.py before running 04_marts.sql / 06_ducklake.sql by hand.
EOF
