-- ============================================================================
-- 01_seed.sql  —  The transactional tier (oltp.duckdb)
-- Real constraints, sequences, all-or-nothing writes. The "wrong tool" corner:
-- DuckDB is a single-writer OLAP engine, so in a real app EVERY write here
-- funnels through ONE serializing process holding the sole write connection.
-- ============================================================================

-- Reproducible synthetic data
SELECT setseed(0.42);

-- ---- Users -----------------------------------------------------------------
CREATE OR REPLACE TABLE users (
    user_id     BIGINT PRIMARY KEY,
    email       VARCHAR NOT NULL UNIQUE,
    country     VARCHAR NOT NULL,
    signed_up   TIMESTAMP NOT NULL
);

INSERT INTO users
SELECT
    id                                             AS user_id,
    'user' || id || '@tealdeal.shop'                 AS email,
    ['NL','DE','FR','GB','US'][(id % 5) + 1]       AS country,
    TIMESTAMP '2025-01-01' + INTERVAL (id * 37) MINUTE AS signed_up
FROM range(1, 501) t(id);

-- ---- Products + inventory --------------------------------------------------
CREATE OR REPLACE TABLE products (
    product_id  BIGINT PRIMARY KEY,
    name        VARCHAR NOT NULL,
    category    VARCHAR NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    description VARCHAR NOT NULL
);

INSERT INTO products
SELECT
    id AS product_id,
    ['Rubber','Mallard','Teal','Muscovy','Wood','Pintail','Eider','Merganser'][(id % 8) + 1]
        || ' ' ||
    ['Duck','Decoy','Float','Toy','Charm','Figurine'][(id % 6) + 1]     AS name,
    ['bath','decor','collectible','outdoor'][(id % 4) + 1]              AS category,
    ((id * 137) % 4500) + 500                                          AS price_cents,
    'A delightful '
        || ['classic','handcrafted','waterproof','limited-edition','vintage'][(id % 5) + 1]
        || ' duck for '
        || ['the bath','your shelf','the pond','collectors'][(id % 4) + 1]  AS description
FROM range(1, 61) t(id);

CREATE OR REPLACE TABLE inventory (
    product_id BIGINT PRIMARY KEY REFERENCES products(product_id),
    -- The star of the OLTP demo: this CHECK does the job a pessimistic lock
    -- would do elsewhere. Oversell => constraint violation => ROLLBACK.
    stock      INTEGER NOT NULL CHECK (stock >= 0)
);

INSERT INTO inventory
SELECT product_id, ((product_id * 7) % 40) + 5 AS stock FROM products;

-- ---- Orders (written one-at-a-time by the single writer) --------------------
-- Live orders (via the transaction in 02_oltp.sql) start at 401, so the 400
-- seeded rows below never collide with sequence-allocated ids.
CREATE SEQUENCE order_seq START 401;

CREATE OR REPLACE TABLE orders (
    order_id   BIGINT PRIMARY KEY DEFAULT nextval('order_seq'),
    user_id    BIGINT NOT NULL REFERENCES users(user_id),
    placed_at  TIMESTAMP NOT NULL DEFAULT now(),
    status     VARCHAR NOT NULL DEFAULT 'confirmed'
                 CHECK (status IN ('confirmed','shipped','cancelled'))
);

CREATE OR REPLACE TABLE order_items (
    order_id    BIGINT NOT NULL REFERENCES orders(order_id),
    product_id  BIGINT NOT NULL REFERENCES products(product_id),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    price_cents INTEGER NOT NULL,          -- price captured at purchase time
    PRIMARY KEY (order_id, product_id)
);

-- Some historical orders so downstream analytics has revenue to chew on.
-- (Generated directly here; the live path is the transaction in 02_oltp.sql.)
INSERT INTO orders (order_id, user_id, placed_at, status)
SELECT
    id,
    (id * 13) % 500 + 1,
    TIMESTAMP '2025-02-01' + INTERVAL (id * 17) MINUTE,
    -- status via hash() so it doesn't correlate with the product/category
    -- modular formulas below (~60% confirmed, 20% shipped, 20% cancelled)
    ['confirmed','confirmed','confirmed','shipped','cancelled'][(hash(id) % 5)::BIGINT + 1]
FROM range(1, 401) t(id);

-- 1-3 deterministic line items per order. Three different product formulas
-- so category and order status don't correlate (otherwise a whole category
-- can end up only ever in cancelled orders), and dense enough that most
-- products share a basket with several others — collector.py's "shoppers
-- like you also got" is real collaborative filtering over this co-purchase
-- history, and it needs actual overlap to have anything to say.
INSERT INTO order_items
SELECT o.order_id, p.product_id,
       ((o.order_id + p.product_id) % 3) + 1 AS qty,
       p.price_cents
FROM orders o
JOIN products p ON p.product_id = ((o.order_id * 3 + 7) % 60) + 1;

INSERT INTO order_items
SELECT o.order_id, p.product_id,
       ((o.order_id * 2 + p.product_id) % 2) + 1 AS qty,
       p.price_cents
FROM orders o
JOIN products p ON p.product_id = ((o.order_id * 7 + 29) % 60) + 1
WHERE ((o.order_id * 7 + 29) % 60) + 1
      <> ((o.order_id * 3 + 7) % 60) + 1;             -- avoid PK clash with item 1

INSERT INTO order_items
SELECT o.order_id, p.product_id,
       ((o.order_id * 5 + p.product_id) % 2) + 1 AS qty,
       p.price_cents
FROM orders o
JOIN products p ON p.product_id = ((o.order_id * 11 + 41) % 60) + 1
WHERE o.order_id % 3 <> 0                             -- ~2/3 get a third item
  AND ((o.order_id * 11 + 41) % 60) + 1 <> ((o.order_id * 3 + 7) % 60) + 1   -- avoid clash with item 1
  AND ((o.order_id * 11 + 41) % 60) + 1 <> ((o.order_id * 7 + 29) % 60) + 1; -- avoid clash with item 2

-- ---- Stock audit trail (DuckDB 2.0 trigger) ---------------------------------
-- Every UPDATE on inventory writes one audit row per changed product. Created
-- after the seed so the bulk load above isn't audited.
CREATE OR REPLACE TABLE stock_audit (
    product_id BIGINT,
    old_stock  INTEGER,
    new_stock  INTEGER,
    changed_at TIMESTAMP DEFAULT now()
);
CREATE TRIGGER trg_stock_audit AFTER UPDATE ON inventory
    REFERENCING OLD TABLE AS o NEW TABLE AS n
    FOR EACH STATEMENT
        INSERT INTO stock_audit (product_id, old_stock, new_stock)
        SELECT n.product_id, o.stock, n.stock FROM o JOIN n USING (product_id);

SELECT 'seed complete' AS status,
       (SELECT count(*) FROM users)       AS users,
       (SELECT count(*) FROM products)    AS products,
       (SELECT count(*) FROM orders)      AS orders,
       (SELECT count(*) FROM order_items) AS order_items;
