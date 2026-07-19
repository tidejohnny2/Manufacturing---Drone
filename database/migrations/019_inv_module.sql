-- 019_inv_module.sql — ERP Phase D: the INV module.
--
-- Moves the inventory data model into its own `inv` schema: the item master
-- (materials), item costing (standard_costs), bins (inventory_items), and the
-- movement ledger (inventory_transactions). public keeps compatibility views;
-- floor locations (zones) STAY in MG — they are work centers as much as
-- storage, and every routing/production table hangs off them.
--
-- The payoff: the procurement service now fills bins IN ITS OWN TRANSACTION
-- when it receives a PO (same database — receipt + GL journal + bin fill are
-- atomic), so the receipt.posted webhook bin-fill bridge retires here (the
-- subscription is deactivated; the outbox/dispatcher infra stays for future
-- consumers).
--
-- Also drops public.inventory_balances — dead (zero code references; only the
-- legacy database/setup_postgres.py checker ever named it).
--
-- Run as superuser in ONE transaction. Not re-runnable (guarded).

BEGIN;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'inv') THEN
    RAISE 'inv schema already exists — migration already applied';
  END IF;
END $$;

CREATE SCHEMA inv;

ALTER TABLE public.materials SET SCHEMA inv;
ALTER TABLE public.standard_costs SET SCHEMA inv;
ALTER TABLE public.inventory_items SET SCHEMA inv;
ALTER TABLE public.inventory_transactions SET SCHEMA inv;

CREATE VIEW public.materials AS SELECT * FROM inv.materials;
CREATE VIEW public.standard_costs AS SELECT * FROM inv.standard_costs;
CREATE VIEW public.inventory_items AS SELECT * FROM inv.inventory_items;
CREATE VIEW public.inventory_transactions AS SELECT * FROM inv.inventory_transactions;

DROP TABLE public.inventory_balances;

-- Bin-fill is in-transaction now; retire the webhook bridge.
UPDATE procurement.webhook_subscriptions SET active = FALSE WHERE event = 'receipt.posted';

ALTER SCHEMA inv OWNER TO manufacturing;
ALTER VIEW public.materials OWNER TO manufacturing;
ALTER VIEW public.standard_costs OWNER TO manufacturing;
ALTER VIEW public.inventory_items OWNER TO manufacturing;
ALTER VIEW public.inventory_transactions OWNER TO manufacturing;

DO $$ BEGIN
  IF (SELECT count(*) FROM inv.inventory_items) <> (SELECT count(*) FROM public.inventory_items) THEN
    RAISE 'inventory_items view mismatch';
  END IF;
  IF (SELECT count(*) FROM inv.standard_costs) <> (SELECT count(*) FROM public.standard_costs) THEN
    RAISE 'standard_costs view mismatch';
  END IF;
END $$;

COMMIT;
