-- ============================================================================
-- 04_marts.sql  —  Downstream analytics (marts.duckdb)
--
-- Where DuckDB earns its keep. Run with marts.duckdb as the current database;
-- it ATTACHes the OLTP store read-only and the DuckLake clickstream catalog,
-- so one query can span the transactional tier and the clickstream lake at
-- once.
--
--   duckdb marts.duckdb -f 04_marts.sql
--
-- NOTE: a local-file DuckLake catalog is itself just a DuckDB file, so — like
-- oltp.duckdb — only one process can have it attached at a time. If
-- collector.py is running (it holds both attached for the life of the
-- server), stop it first; otherwise this ATTACH fails with a lock conflict.
-- ============================================================================

INSTALL ducklake;
ATTACH IF NOT EXISTS 'oltp.duckdb' AS oltp (READ_ONLY);
ATTACH IF NOT EXISTS 'ducklake:teal_deal_catalog.ducklake' AS lakehouse (DATA_PATH 'lakehouse_data/');

-- Convenience view over the DuckLake clickstream catalog. Includes any
-- traffic from 07_stress.py's concurrent-writer test — those rows are
-- tagged in `props` but use real event_types, so they count here too.
CREATE OR REPLACE VIEW lake_events AS
SELECT * FROM lakehouse.events;

-- ---- 1. Sessionization: 30-min inactivity gap => new session ----------------
CREATE OR REPLACE TABLE session_events AS
WITH gaps AS (
    SELECT
        user_id, event_ts, event_type, product_id,
        event_ts - LAG(event_ts) OVER (PARTITION BY user_id ORDER BY event_ts) AS gap
    FROM lake_events
),
flagged AS (
    SELECT *,
        CASE WHEN gap IS NULL OR gap > INTERVAL 30 MINUTE THEN 1 ELSE 0 END AS is_new_session
    FROM gaps
)
SELECT *,
    SUM(is_new_session) OVER (PARTITION BY user_id ORDER BY event_ts) AS session_num
FROM flagged;

CREATE OR REPLACE TABLE sessions AS
SELECT
    hash(user_id, session_num)                              AS session_id,
    user_id,
    min(event_ts)                                           AS started_at,
    max(event_ts)                                           AS ended_at,
    max(event_ts) - min(event_ts)                           AS duration,
    count(*)                                                AS num_events,
    bool_or(event_type = 'add_to_cart')                     AS reached_cart,
    bool_or(event_type = 'purchase')                        AS reached_purchase
FROM session_events
GROUP BY user_id, session_num;

-- ---- 2. Conversion funnel (session-level) -----------------------------------
CREATE OR REPLACE TABLE funnel AS
SELECT
    count(*)                                                        AS sessions,
    count(*) FILTER (WHERE reached_cart)                            AS carted,
    count(*) FILTER (WHERE reached_purchase)                        AS purchased,
    round(100.0 * count(*) FILTER (WHERE reached_cart)     / count(*), 1) AS view_to_cart_pct,
    round(100.0 * count(*) FILTER (WHERE reached_purchase)
                 / nullif(count(*) FILTER (WHERE reached_cart), 0), 1)    AS cart_to_purchase_pct
FROM sessions;

-- ---- 3. Retention cohorts (first-activity week x weeks-since) ---------------
-- Cohort each user by their FIRST week seen in the lake, enrich the cohort with
-- country from the OLTP store (the cross-database join), then measure return in
-- subsequent weeks — the classic retention triangle.
CREATE OR REPLACE TABLE retention AS
WITH activity AS (
    SELECT DISTINCT user_id, date_trunc('week', event_ts) AS active_week
    FROM lake_events
),
cohort AS (
    SELECT user_id, min(active_week) AS cohort_week FROM activity GROUP BY user_id
)
SELECT
    c.cohort_week,
    u.country                                                        AS cohort_country,
    CAST(date_diff('week', c.cohort_week, a.active_week) AS INTEGER) AS weeks_since,
    count(DISTINCT c.user_id)                                        AS active_users
FROM cohort c
JOIN activity a USING (user_id)
JOIN oltp.users u USING (user_id)          -- cross-database enrichment
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- ---- 4. Revenue rollup (pure OLTP, confirmed + shipped only) ----------------
CREATE OR REPLACE TABLE revenue_by_category AS
SELECT
    p.category,
    count(DISTINCT o.order_id)                    AS orders,
    sum(oi.qty)                                   AS units,
    round(sum(oi.qty * oi.price_cents) / 100.0, 2) AS revenue_eur
FROM oltp.orders o
JOIN oltp.order_items oi USING (order_id)
JOIN oltp.products    p  USING (product_id)
WHERE o.status IN ('confirmed', 'shipped')
GROUP BY p.category
ORDER BY revenue_eur DESC;

-- ---- Show the results -------------------------------------------------------
SELECT '=== FUNNEL ===' AS section;
SELECT * FROM funnel;

SELECT '=== REVENUE BY CATEGORY ===' AS section;
SELECT * FROM revenue_by_category;

SELECT '=== RETENTION (all cohorts, by weeks since first seen) ===' AS section;
SELECT weeks_since, sum(active_users) AS active_users
FROM retention GROUP BY weeks_since ORDER BY weeks_since;
