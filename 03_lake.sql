-- ============================================================================
-- 03_lake.sql  —  Web analytics / clickstream lake (DuckLake catalog)
--
-- A JS tracker fires pageview / add_to_cart / purchase events; a collector
-- appends them to the lake. This is the "append-only truth" — landed here as
-- a real DuckLake catalog (a transactional catalog over Parquet: every INSERT
-- is a snapshot, so 06_ducklake.sql can time-travel and diff it) instead of a
-- bare hive-partitioned Parquet directory. Needs network the first time, to
-- download the `ducklake` extension.
-- Payloads stay semi-structured in a JSON column so the tracker can evolve
-- without a schema migration.
-- ============================================================================

INSTALL ducklake;

-- Catalog metadata in a local file; data files land under lakehouse_data/.
ATTACH 'ducklake:puddle_catalog.ducklake' AS lakehouse (DATA_PATH 'lakehouse_data/');

CREATE TABLE IF NOT EXISTS lakehouse.events (
    event_id   BIGINT,
    user_id    BIGINT,
    event_type VARCHAR,
    product_id BIGINT,
    event_ts   TIMESTAMP,
    event_date DATE,
    props      JSON
);

-- Deterministic "interest" backbone: a (user, product, time) touch.
CREATE OR REPLACE TABLE _interest AS
SELECT id, user_id, product_id, ts
FROM (
    SELECT
        id,
        (id * 7)  % 200 + 1                                AS user_id,
        (id * 13) % 60  + 1                                AS product_id,
        TIMESTAMP '2025-03-01' + INTERVAL (id * 11) MINUTE AS ts,
        LEAST(id // 800, 8)                                AS week_idx  -- ~0..5
    FROM range(1, 4001) t(id)
)
-- Gentle deterministic churn: a shrinking slice of users stays active each
-- later week, so the retention triangle decays (~100% -> ~50%) instead of
-- every user being active in every week.
WHERE (user_id % 10) < (10 - week_idx);

-- The full clickstream = product pageviews + some non-product browsing
-- + a funnel of carts (1/3 of views) and purchases (1/9 of views), with a
-- semi-structured JSON props column so the tracker's schema can evolve
-- without a migration.
CREATE OR REPLACE TABLE events AS
WITH pageviews AS (          -- every interest touch is a product pageview
    SELECT id, user_id, 'pageview' AS event_type, product_id, ts FROM _interest
),
browsing AS (                -- homepage/category views with no product
    SELECT id + 100000, user_id, 'pageview', NULL,
           ts - INTERVAL 3 MINUTE
    FROM _interest WHERE id % 5 = 0
),
carts AS (                   -- 1 in 3 pageviews becomes an add_to_cart
    SELECT id + 200000, user_id, 'add_to_cart', product_id, ts + INTERVAL 2 MINUTE
    FROM _interest WHERE id % 3 = 0
),
purchases AS (               -- 1 in 3 carts becomes a purchase (=> 1/9 of views)
    SELECT id + 300000, user_id, 'purchase', product_id, ts + INTERVAL 7 MINUTE
    FROM _interest WHERE id % 9 = 0
),
_events(id, user_id, event_type, product_id, ts) AS (
    SELECT * FROM pageviews
    UNION ALL SELECT * FROM browsing
    UNION ALL SELECT * FROM carts
    UNION ALL SELECT * FROM purchases
)
SELECT
    row_number() OVER (ORDER BY ts, id)                   AS event_id,
    user_id,
    event_type,
    product_id,
    ts AS event_ts,
    CAST(ts AS DATE) AS event_date,
    to_json({
        'device':   ['mobile','desktop','tablet'][(user_id % 3) + 1],
        'referrer': ['google','direct','newsletter','instagram'][(id % 4) + 1],
        'session_hint': (user_id * 1000 + (epoch(ts) // 1800))   -- 30-min bucket
    }) AS props
FROM _events;

-- Land it in the lake in three chronological waves — each INSERT is its own
-- transaction, so this leaves three real snapshots for 06_ducklake.sql to
-- travel between, right out of the box.
INSERT INTO lakehouse.events
SELECT * FROM events WHERE event_date < DATE '2025-03-11';          -- snapshot: early March

INSERT INTO lakehouse.events
SELECT * FROM events
WHERE event_date >= DATE '2025-03-11' AND event_date < DATE '2025-03-21';  -- snapshot: mid March

INSERT INTO lakehouse.events
SELECT * FROM events WHERE event_date >= DATE '2025-03-21';         -- snapshot: late March

SELECT 'lake written' AS status,
       count(*)                                   AS events,
       count(DISTINCT event_date)                 AS day_partitions,
       count(*) FILTER (WHERE event_type='pageview')    AS pageviews,
       count(*) FILTER (WHERE event_type='add_to_cart') AS carts,
       count(*) FILTER (WHERE event_type='purchase')    AS purchases
FROM lakehouse.events;

-- Tidy the scratch tables; the events live in the DuckLake catalog now.
DROP TABLE _interest;
DROP TABLE events;
