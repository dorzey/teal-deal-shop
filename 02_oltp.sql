-- ============================================================================
-- 02_oltp.sql  —  Placing an order as a real transaction (oltp.duckdb)
--
-- This is the tier DuckDB is "not supposed" to be good at, which makes it the
-- most instructive. Everything here assumes a SINGLE writer: one process holds
-- the sole write connection and serializes order placement. Readers use
-- separate read-only connections. That's the honest way to run an app on an
-- embedded OLAP engine.
--
-- The transaction below is all-or-nothing: allocate an order id, decrement
-- stock, write the line items. If any product would go negative, the
-- CHECK (stock >= 0) constraint fires and the whole thing rolls back — the
-- constraint doing the work a pessimistic row lock would do in Postgres.
-- ============================================================================

-- ---- Happy path: user 7 buys 2x product 3 and 1x product 10 ----------------
BEGIN TRANSACTION;

    -- Allocate the order (order_id comes from order_seq via the column DEFAULT)
    INSERT INTO orders (user_id) VALUES (7);

    -- Decrement inventory. The CHECK constraint is the guard rail.
    UPDATE inventory SET stock = stock - 2 WHERE product_id = 3;
    UPDATE inventory SET stock = stock - 1 WHERE product_id = 10;

    -- Write line items, capturing price at purchase time
    INSERT INTO order_items (order_id, product_id, qty, price_cents)
    SELECT currval('order_seq'), p.product_id, q.qty, p.price_cents
    FROM (VALUES (3, 2), (10, 1)) AS q(product_id, qty)
    JOIN products p USING (product_id);

COMMIT;

-- ---- Verify the write landed ------------------------------------------------
SELECT
    o.order_id,
    o.user_id,
    oi.product_id,
    oi.qty,
    oi.price_cents,
    inv.stock AS stock_remaining
FROM orders o
JOIN order_items oi USING (order_id)
JOIN inventory   inv USING (product_id)
WHERE o.order_id = currval('order_seq')
ORDER BY oi.product_id;

-- ---- The trigger on inventory (see 01_seed.sql) logged both decrements -----
SELECT product_id, old_stock, new_stock FROM stock_audit ORDER BY changed_at, product_id;

-- ---- The oversell case ------------------------------------------------------
-- Uncomment to watch the CHECK fire and abort the transaction:
--
--   BEGIN TRANSACTION;
--     INSERT INTO orders (user_id) VALUES (7);
--     UPDATE inventory SET stock = stock - 999999 WHERE product_id = 3;  -- boom
--   COMMIT;
--
-- The UPDATE raises a Constraint Error; nothing commits; inventory is intact.
