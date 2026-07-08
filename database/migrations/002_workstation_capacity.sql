-- Migration 002: per-workstation batch capacity.
--
-- For databases created BEFORE the capacity column. Fresh installs do not need
-- this file: schema.sql and seed.sql already include it. Safe to re-run.
--
-- capacity = units a workstation can work on at once (NULL = unconstrained).
-- Orders larger than capacity run in batches, multiplying the station's cycle
-- time in the floor simulation.

ALTER TABLE zones ADD COLUMN IF NOT EXISTS capacity INTEGER;

UPDATE zones SET capacity = 3 WHERE id = 'ws1' AND capacity IS NULL;
UPDATE zones SET capacity = 3 WHERE id = 'ws2' AND capacity IS NULL;
UPDATE zones SET capacity = 2 WHERE id = 'ws3' AND capacity IS NULL;
UPDATE zones SET capacity = 2 WHERE id = 'ws4' AND capacity IS NULL;
UPDATE zones SET capacity = 2 WHERE id = 'ws5' AND capacity IS NULL;
UPDATE zones SET capacity = 4 WHERE id = 'cws1' AND capacity IS NULL;
UPDATE zones SET capacity = 4 WHERE id = 'cws2' AND capacity IS NULL;
UPDATE zones SET capacity = 5 WHERE id = 'cws3' AND capacity IS NULL;
UPDATE zones SET capacity = 3 WHERE id = 'cws4' AND capacity IS NULL;
