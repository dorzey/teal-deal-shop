-- ============================================================================
-- 06_ducklake.sql  —  Promote the Parquet lake to a real DuckLake catalog
--
-- ⚠️  REQUIRES THE `ducklake` EXTENSION (needs network to download).
--     Run where extensions can install:
--         duckdb -f 06_ducklake.sql
--
-- The plain lake/ directory is append-only Parquet with no catalog: you can
-- read it, but you can't ask "what did this look like last Tuesday?" DuckLake
-- adds a transactional catalog over Parquet — every commit is a snapshot, so
-- you get versioning, time travel, and change feeds. Here we ingest the
-- existing events in a few chronological batches so there are snapshots to
-- travel between.
-- ============================================================================

INSTALL ducklake;
LOAD ducklake;

-- Catalog metadata in a local file; data files land under lakehouse_data/.
ATTACH 'ducklake:puddle_catalog.ducklake' AS lakehouse (DATA_PATH 'lakehouse_data/');
USE lakehouse;

CREATE TABLE IF NOT EXISTS events (
    event_id   BIGINT,
    user_id    BIGINT,
    event_type VARCHAR,
    product_id BIGINT,
    event_ts   TIMESTAMP,
    event_date DATE,
    props      JSON
);

-- Each INSERT is its own transaction => its own snapshot. Ingest the existing
-- Parquet lake in three chronological waves so we have versions to compare.
INSERT INTO events
SELECT event_id, user_id, event_type, product_id, event_ts, event_date, props
FROM read_parquet('lake/events/**/*.parquet', hive_partitioning = true)
WHERE event_date < DATE '2025-03-11';                 -- snapshot 1: early March

INSERT INTO events
SELECT event_id, user_id, event_type, product_id, event_ts, event_date, props
FROM read_parquet('lake/events/**/*.parquet', hive_partitioning = true)
WHERE event_date >= DATE '2025-03-11'
  AND event_date <  DATE '2025-03-21';                -- snapshot 2: mid March

INSERT INTO events
SELECT event_id, user_id, event_type, product_id, event_ts, event_date, props
FROM read_parquet('lake/events/**/*.parquet', hive_partitioning = true)
WHERE event_date >= DATE '2025-03-21';                -- snapshot 3: late March

-- ---- The snapshot log ------------------------------------------------------
SELECT snapshot_id, snapshot_time, schema_version
FROM ducklake_snapshots('lakehouse')
ORDER BY snapshot_id;

-- ---- Time travel: row counts as of each version ----------------------------
SELECT 1 AS version, count(*) AS events FROM events AT (VERSION => 1)
UNION ALL SELECT 2, count(*) FROM events AT (VERSION => 2)
UNION ALL SELECT 3, count(*) FROM events AT (VERSION => 3)
ORDER BY version;

-- ---- "Replay last week's dashboard": the funnel as of snapshot 2 -----------
SELECT
    count(*) FILTER (WHERE event_type = 'pageview')    AS pageviews,
    count(*) FILTER (WHERE event_type = 'add_to_cart') AS carts,
    count(*) FILTER (WHERE event_type = 'purchase')    AS purchases
FROM events AT (VERSION => 2);

-- ---- Change feed between two snapshots (DuckLake CDC) -----------------------
-- What landed between version 1 and version 3?
SELECT event_type, count(*) AS added
FROM ducklake_table_changes('lakehouse', 'main', 'events', 1, 3)
WHERE change_type = 'insert'
GROUP BY event_type
ORDER BY added DESC;

-- Time travel by wall-clock also works, e.g.:
--   SELECT count(*) FROM events AT (TIMESTAMP => now() - INTERVAL 1 HOUR);
