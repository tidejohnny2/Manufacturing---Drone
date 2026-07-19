-- 017_receipt_webhook.sql
-- Idempotency ledger for the procurement receipt.posted webhook. A receipt is
-- bin-filled exactly once, keyed on the service receipt no, whether it
-- originated in Manufacturing (its own receive) or in the procurement UI (the
-- webhook) — the first writer wins, any echo/retry is a no-op.
CREATE TABLE IF NOT EXISTS processed_receipts (
  receipt_no   TEXT PRIMARY KEY,
  po_no        TEXT,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
