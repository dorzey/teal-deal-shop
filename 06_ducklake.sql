-- ============================================================================
-- 06_ducklake.sql  —  Explore the live DuckLake catalog: snapshots + time travel
--
-- ⚠️  REQUIRES THE `ducklake` EXTENSION (needs network to download, once —
--     it's cached after that).
--     Run where extensions can install:
--         duckdb -f 06_ducklake.sql
--
-- 03_lake.sql lands the clickstream directly into this DuckLake catalog in
-- three chronological waves, so there are real snapshots to travel between
-- right out of the box. collector.py adds more on top as live events flow
-- in and it periodically flushes, so a server that's been running will
-- show a higher max snapshot than the versions this script assumes below.
-- ============================================================================

INSTALL ducklake;

ATTACH 'ducklake:teal_deal_catalog.ducklake' AS lakehouse (DATA_PATH 'lakehouse_data/');
USE lakehouse;

-- ---- The snapshot log ------------------------------------------------------
SELECT snapshot_id, snapshot_time, schema_version
FROM ducklake_snapshots('lakehouse')
ORDER BY snapshot_id;

-- ---- Time travel: row counts as of each seed snapshot -----------------------
-- Snapshot 1 is the empty table (right after CREATE TABLE); 2/3/4 are the
-- three chronological seed waves from 03_lake.sql (early / mid / late March).
SELECT 1 AS version, count(*) AS events FROM events AT (VERSION => 1)
UNION ALL SELECT 2, count(*) FROM events AT (VERSION => 2)
UNION ALL SELECT 3, count(*) FROM events AT (VERSION => 3)
UNION ALL SELECT 4, count(*) FROM events AT (VERSION => 4)
ORDER BY version;

-- ---- "Replay last week's dashboard": the funnel as of snapshot 3 (mid-March) --
SELECT
    count(*) FILTER (WHERE event_type = 'pageview')    AS pageviews,
    count(*) FILTER (WHERE event_type = 'add_to_cart') AS carts,
    count(*) FILTER (WHERE event_type = 'purchase')    AS purchases
FROM events AT (VERSION => 3);

-- ---- Change feed between two snapshots (DuckLake CDC) -----------------------
-- What landed between the empty table (1) and the fully-seeded lake (4)?
SELECT event_type, count(*) AS added
FROM ducklake_table_changes('lakehouse', 'main', 'events', 1, 4)
WHERE change_type = 'insert'
GROUP BY event_type
ORDER BY added DESC;

-- Time travel by wall-clock also works, e.g.:
--   SELECT count(*) FROM events AT (TIMESTAMP => now() - INTERVAL 1 HOUR);

-- ---- Maintenance: merge the small files a busy lake accumulates -------------
-- Every INSERT (each seed wave, each collector.py flush) is its own file(s).
-- This is the documented fix — a periodic scheduled call, not something the
-- app should hand-roll. collector.py already does this in the background
-- every 60s while it's running; here it is standalone:
--   CALL ducklake_merge_adjacent_files('lakehouse');
