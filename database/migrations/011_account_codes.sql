-- 011: Subledger account codes — customers C-1001+, vendors V-2001+.
-- Assigned once, permanent, backfilled in id order; new rows continue
-- the sequence. Idempotent; the same statements live in schema.sql.

ALTER TABLE customers ADD COLUMN IF NOT EXISTS account_code TEXT UNIQUE;
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS account_code TEXT UNIQUE;

DO $$
DECLARE
  base INTEGER;
BEGIN
  SELECT COALESCE(MAX(substring(account_code FROM 3)::int), 1000) INTO base
  FROM customers WHERE account_code ~ '^C-[0-9]+$';
  UPDATE customers c SET account_code = 'C-' || r.n
  FROM (
    SELECT id, base + ROW_NUMBER() OVER (ORDER BY id) AS n
    FROM customers WHERE account_code IS NULL
  ) r
  WHERE c.id = r.id;

  SELECT COALESCE(MAX(substring(account_code FROM 3)::int), 2000) INTO base
  FROM vendors WHERE account_code ~ '^V-[0-9]+$';
  UPDATE vendors v SET account_code = 'V-' || r.n
  FROM (
    SELECT id, base + ROW_NUMBER() OVER (ORDER BY id) AS n
    FROM vendors WHERE account_code IS NULL
  ) r
  WHERE v.id = r.id;
END $$;
