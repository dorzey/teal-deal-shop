-- ============================================================================
-- 05_extras.sql  —  The fun tier: everything is "just a DuckDB extension"
--
-- ⚠️  REQUIRES EXTENSION DOWNLOADS (extensions.duckdb.org + community repo).
--     Run against oltp.duckdb where the network can reach the extension hosts:
--       duckdb oltp.duckdb -f 05_extras.sql
--     Each block is independent — comment out any you don't want.
-- ============================================================================

-- ---- A. Full-text product search (fts / BM25) ------------------------------
INSTALL fts; LOAD fts;

PRAGMA create_fts_index('products', 'product_id', 'name', 'description', overwrite = 1);

-- "waterproof pond ducks", ranked by BM25
SELECT product_id, name,
       fts_main_products.match_bm25(product_id, 'waterproof pond') AS score
FROM products
WHERE score IS NOT NULL
ORDER BY score DESC
LIMIT 5;

-- ---- B. Vector similarity (vss / HNSW) -------------------------------------
INSTALL vss; LOAD vss;

-- HNSW indexes on a persistent (file) database are still experimental, so they
-- require this opt-in. In-memory databases don't need it. For a toy it's fine;
-- for production, know that the index is rebuilt on load and isn't crash-safe.
SET hnsw_enable_experimental_persistence = true;

-- NOTE: DuckDB won't let you UPDATE a table whose key is referenced by a
-- foreign key elsewhere — `products` is referenced by `inventory` and
-- `order_items`, so `UPDATE products SET embedding = ...` fails even though it
-- never touches product_id. Keep the vectors in their own table instead.
--
-- A simple real embedding: a 16-dim hashed bag-of-words over name +
-- description (name double-weighted), no external model needed — the same
-- formula public/index.html and collector.py use, so all three agree.
-- Products with overlapping words land near each other.
CREATE OR REPLACE TABLE _tokens AS
SELECT product_id, unnest(string_split(
    regexp_replace(lower(name || ' ' || name || ' ' || description), '[^a-z0-9 ]', ' ', 'g'), ' '
)) AS token
FROM products;

CREATE OR REPLACE TABLE _buckets AS
SELECT product_id, (hash(token) % 16)::INTEGER AS bucket, count(*) AS n
FROM _tokens WHERE token <> '' GROUP BY product_id, bucket;

CREATE OR REPLACE TABLE product_vectors AS
SELECT p.product_id, list(coalesce(b.n, 0)::FLOAT ORDER BY g.bucket)::FLOAT[16] AS embedding
FROM products p CROSS JOIN generate_series(0, 15) g(bucket)
LEFT JOIN _buckets b ON b.product_id = p.product_id AND b.bucket = g.bucket
GROUP BY p.product_id;

DROP TABLE _tokens;
DROP TABLE _buckets;

CREATE INDEX IF NOT EXISTS pv_hnsw ON product_vectors USING HNSW (embedding);

-- Nearest neighbours to product 3 (excludes itself).
-- Tip: to actually exercise the HNSW index, pass a literal FLOAT[16] into
-- array_distance with ORDER BY ... LIMIT; a subquery vector falls back to exact.
WITH q AS (SELECT embedding AS qe FROM product_vectors WHERE product_id = 3)
SELECT v.product_id, p.name, p.category,
       array_distance(v.embedding, q.qe) AS dist
FROM product_vectors v
CROSS JOIN q
JOIN products p USING (product_id)
WHERE v.product_id <> 3
ORDER BY dist
LIMIT 5;

-- ---- C. Co-purchase graph traversal (DuckPGQ) ------------------------------
INSTALL duckpgq FROM community; LOAD duckpgq;

-- Edges: products bought together in the same order, weighted by frequency.
CREATE OR REPLACE TABLE copurchase AS
SELECT a.product_id AS p1, b.product_id AS p2, count(*) AS weight
FROM order_items a
JOIN order_items b ON a.order_id = b.order_id AND a.product_id < b.product_id
GROUP BY 1, 2;

CREATE OR REPLACE PROPERTY GRAPH copurch
    VERTEX TABLES (products LABEL product)
    EDGE TABLES (
        copurchase SOURCE KEY (p1) REFERENCES products (product_id)
                   DESTINATION KEY (p2) REFERENCES products (product_id)
                   LABEL bought_with
    );

-- "Customers who bought product 3 also bought…" as a real graph MATCH
FROM GRAPH_TABLE (copurch
    MATCH (a:product)-[e:bought_with]->(b:product)
    WHERE a.product_id = 3
    COLUMNS (b.name AS also_bought, e.weight AS times)
)
ORDER BY times DESC
LIMIT 5;

-- ---- D. Nearest-warehouse fulfilment (spatial) -----------------------------
INSTALL spatial; LOAD spatial;

CREATE OR REPLACE TABLE warehouses AS
SELECT * FROM (VALUES
    ('Amsterdam', 4.9041, 52.3676),
    ('Berlin',   13.4050, 52.5200),
    ('Paris',     2.3522, 48.8566),
    ('London',   -0.1276, 51.5072)
) AS w(name, lon, lat);

-- Which warehouse is closest to a delivery point near Utrecht?
-- (planar distance on lon/lat — fine for a toy; use ST_Distance_Sphere for real)
SELECT w.name,
       ST_Distance(ST_Point(w.lon, w.lat), ST_Point(5.1214, 52.0907)) AS dist_deg
FROM warehouses w
ORDER BY dist_deg
LIMIT 1;

-- ---- E. Join a remote price feed live (httpfs) -----------------------------
-- INSTALL httpfs; LOAD httpfs;
-- SELECT p.product_id, p.name, p.price_cents, feed.competitor_cents
-- FROM products p
-- JOIN read_parquet('https://example.com/price_feed.parquet') feed
--   USING (product_id);

-- ---- F. Time travel over the lake (DuckLake) -------------------------------
-- Snapshots, AS-OF queries, and the change feed: see 06_ducklake.sql.
