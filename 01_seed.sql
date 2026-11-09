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
    'user' || id || '@example.invalid'               AS email,
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

-- Sixty real duck species, grouped by how they feed. ids are assigned in hash
-- order so categories interleave (otherwise a category would own a contiguous
-- id block and the modular order formulas below would skew its revenue).
INSERT INTO products
SELECT
    row_number() OVER (ORDER BY hash(name))                            AS product_id,
    name,
    category,
    ((row_number() OVER (ORDER BY hash(name)) * 137) % 4500) + 500     AS price_cents,
    description
FROM (VALUES
    ('Mallard', 'dabbling', 'Classic green-headed duck; the ancestor of most farmyard ducks'),
    ('Northern Pintail', 'dabbling', 'Elegant long-necked duck with a slender pointed tail'),
    ('Green-winged Teal', 'dabbling', 'Smallest dabbling duck, quick and acrobatic in flight'),
    ('Blue-winged Teal', 'dabbling', 'Small migrant with a white crescent on the face'),
    ('Cinnamon Teal', 'dabbling', 'Rich rust-red plumage, rare in a pond near you'),
    ('Gadwall', 'dabbling', 'Understated grey duck that steals weed from diving coots'),
    ('Eurasian Wigeon', 'dabbling', 'Chestnut head, cream forehead, whistles instead of quacking'),
    ('American Wigeon', 'dabbling', 'Bald-looking white crown and a green eye-stripe'),
    ('Northern Shoveler', 'dabbling', 'Huge spoon-shaped bill for sieving the pond surface'),
    ('American Black Duck', 'dabbling', 'Dusky, wary cousin of the mallard'),
    ('Garganey', 'dabbling', 'Summer visitor with a bold white eyebrow, crackling call'),
    ('Falcated Duck', 'dabbling', 'Drooping sickle-shaped feathers and a shaggy green mane'),
    ('Mottled Duck', 'dabbling', 'Warm-brown Gulf coast resident that never migrates'),
    ('Gray Teal', 'dabbling', 'Plain, hardy Australasian teal, found on any water going'),
    ('Pacific Black Duck', 'dabbling', 'Common Australasian duck with striped face'),
    ('Cape Teal', 'dabbling', 'Pale pink-billed African teal of shallow salt pans'),
    ('Chestnut Teal', 'dabbling', 'Glossy green head and deep chestnut body, Australian'),
    ('Marbled Teal', 'dabbling', 'Soft pale-brown, spotted duck of Mediterranean wetlands'),
    ('Laysan Duck', 'dabbling', 'One of the rarest ducks, a small island dabbler'),
    ('Hawaiian Duck', 'dabbling', 'The koloa, a mottled brown native of Hawaiian streams'),
    ('Yellow-billed Duck', 'dabbling', 'Bright yellow bill, widespread across southern Africa'),
    ('Spot-billed Duck', 'dabbling', 'Yellow-tipped bill and a red spot at its base'),
    ('Canvasback', 'diving', 'Sloping profile and a rust-red head, fast flier'),
    ('Redhead', 'diving', 'Round copper head, dives for pondweed'),
    ('Ring-necked Duck', 'diving', 'Faint chestnut collar, peaked head, dives in lakes'),
    ('Greater Scaup', 'diving', 'Large bay duck with a bright blue bill'),
    ('Lesser Scaup', 'diving', 'Purple-glossed head, dives in flocks on open water'),
    ('Tufted Duck', 'diving', 'Black duck with a floppy crest and gold eyes'),
    ('Common Pochard', 'diving', 'Chestnut head, grey back, dives for plants'),
    ('Ferruginous Duck', 'diving', 'Deep chestnut with a white eye, shy reedbed diver'),
    ('Southern Pochard', 'diving', 'Dark diving duck of South American lakes'),
    ('Common Goldeneye', 'diving', 'Golden eye, whistling wingbeats, nests in tree holes'),
    ('Barrow''s Goldeneye', 'diving', 'Crescent face patch and a purple head'),
    ('Bufflehead', 'diving', 'Tiny, big-headed diver with a white bonnet'),
    ('Hooded Merganser', 'diving', 'Fan-shaped crest it can raise and flatten'),
    ('Common Merganser', 'diving', 'Long saw-edged bill for catching fish underwater'),
    ('Red-breasted Merganser', 'diving', 'Shaggy double-crested diver with a thin red bill'),
    ('Smew', 'diving', 'White ''panda'' mergansers that winter on frozen lakes'),
    ('Ruddy Duck', 'diving', 'Stiff cocked tail and a bright blue bill, dives to feed'),
    ('Common Eider', 'sea', 'Largest European duck, supplier of eiderdown'),
    ('King Eider', 'sea', 'Orange knob on its bill and a pastel-blue crown'),
    ('Spectacled Eider', 'sea', 'Goggle-like white eye patches, Arctic sea duck'),
    ('Steller''s Eider', 'sea', 'Smallest eider, with a tidy black spot on the side'),
    ('Long-tailed Duck', 'sea', 'Streaming tail, dives deeper than most ducks'),
    ('Harlequin Duck', 'sea', 'Painted slate-blue duck that rides white-water torrents'),
    ('Surf Scoter', 'sea', 'Clownish orange-and-white bill and white nape patch'),
    ('White-winged Scoter', 'sea', 'Black sea duck with a white wing flash'),
    ('Black Scoter', 'sea', 'All-black drake with a bright orange knob'),
    ('Velvet Scoter', 'sea', 'Large dark sea duck, white wing patches, winters at sea'),
    ('Common Scoter', 'sea', 'Black sea duck that rafts in huge winter flocks'),
    ('Wood Duck', 'perching', 'Painted crest and red eye, nests high in trees'),
    ('Mandarin Duck', 'perching', 'Ornate orange ''sail'' feathers, from east Asia'),
    ('Muscovy Duck', 'perching', 'Red-faced, claw-footed tree-nester from Central and South America'),
    ('Comb Duck', 'perching', 'Fleshy comb on the bill; perches in trees and wades'),
    ('Maned Duck', 'perching', 'Australian wood duck that grazes on grass'),
    ('Brazilian Teal', 'perching', 'Tiny, bright-billed perching teal from South America'),
    ('Hartlaub''s Duck', 'perching', 'Dark duck of African forest streams'),
    ('Fulvous Whistling Duck', 'perching', 'Long-legged and tawny, whistles in flight'),
    ('White-faced Whistling Duck', 'perching', 'Gregarious tree-duck with a pale face'),
    ('Black-bellied Whistling Duck', 'perching', 'Pink bill, long legs, perches on posts and branches')
) AS species(name, category, description);

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

-- ---- Auto-restock (DuckDB 2.0 trigger) --------------------------------------
-- A public demo drains its own shelves. When an order leaves a product under 5,
-- a "supplier delivery" tops it back up to 40, in the same transaction as the
-- order. It hangs off order_items, not inventory: a trigger can't update the
-- table it is attached to (DuckDB: "recursive trigger chains are not yet
-- supported"), and an order inserts its line items right after decrementing
-- stock. The restock UPDATE fires trg_stock_audit above, so deliveries show up
-- in stock_audit too. An oversell still fails the CHECK (stock >= 0) before this
-- runs, so "not enough stock" keeps working.
CREATE TRIGGER trg_restock AFTER INSERT ON order_items
    REFERENCING NEW TABLE AS n
    FOR EACH STATEMENT
        UPDATE inventory SET stock = 40
        WHERE product_id IN (SELECT product_id FROM n) AND stock < 5;

SELECT 'seed complete' AS status,
       (SELECT count(*) FROM users)       AS users,
       (SELECT count(*) FROM products)    AS products,
       (SELECT count(*) FROM orders)      AS orders,
       (SELECT count(*) FROM order_items) AS order_items;
