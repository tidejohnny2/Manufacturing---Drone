-- 016_rename_module_facilities.sql
-- The module is "Manufacturing", not "Drone" -- drone is just one product line
-- (DRN-FG-600) built alongside the case line (CASE-FG-500). Drop "Drone" from
-- the facility / line names and descriptions. Idempotent (keyed by id).
UPDATE facilities SET
  name = 'Manufacturing Floor Map',
  description = 'Prototype production floor with component kitting, airframe build, electronics integration, firmware calibration, motor testing, QA flight testing, packaging, and finished-goods storage.'
WHERE id = 1;

UPDATE facilities SET
  name = 'Case Production Line',
  description = 'Case line that forms, fits, assembles, inspects, and stocks transport cases as manufactured sub-assembly inventory for packaging pull.'
WHERE id = 2;
