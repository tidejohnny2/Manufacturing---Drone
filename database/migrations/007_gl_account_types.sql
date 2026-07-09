-- Chart-of-accounts maintenance: widen gl_accounts.account_type to include
-- income and expense so the chart can grow beyond the seeded costing accounts.
-- For databases created before this feature; fresh databases get it from
-- schema.sql. Safe to re-run.

ALTER TABLE gl_accounts DROP CONSTRAINT IF EXISTS gl_accounts_account_type_check;
ALTER TABLE gl_accounts ADD CONSTRAINT gl_accounts_account_type_check
  CHECK (account_type IN ('asset', 'liability', 'equity', 'income', 'expense', 'variance'));
