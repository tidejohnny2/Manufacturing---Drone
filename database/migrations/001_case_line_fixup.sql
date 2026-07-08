-- Migration 001: drone case production line + BOM data fixup.
--
-- For databases created BEFORE the case line. Fresh installs do not need this
-- file: schema.sql and seed.sql already include everything below.
--
-- Run order for an existing database:
--   1. Run this file (constraint fixup + case line seed data).
--   2. Re-run schema.sql (safe to re-run) to install the generalized
--      next_production_order_no(p_prefix) and
--      create_production_order(..., p_finished_sku) functions.

-- 1) BOM data fixup.
-- The drone transport case (CASE-FG-500) is both its own BOM parent on the case
-- line and a component line on the drone BOM, so part numbers become unique per
-- parent BOM instead of globally unique, and BOM lines gain an optional
-- inventory-pull source zone.
ALTER TABLE bom_items DROP CONSTRAINT IF EXISTS bom_items_part_number_key;
ALTER TABLE bom_items ADD COLUMN IF NOT EXISTS source_zone_id TEXT REFERENCES zones(id);
DO $$
BEGIN
  ALTER TABLE bom_items ADD CONSTRAINT bom_items_parent_part_unique UNIQUE (parent_material_id, part_number);
EXCEPTION
  WHEN duplicate_object THEN NULL;
  WHEN duplicate_table THEN NULL;
END $$;

-- Stocked sub-assemblies (finished cases) get their own inventory area.
ALTER TABLE inventory_items DROP CONSTRAINT IF EXISTS inventory_items_area_check;
ALTER TABLE inventory_items ADD CONSTRAINT inventory_items_area_check
  CHECK (area IN ('Parts', 'Production', 'Sub-assembly', 'Finished Goods'));

-- 2) Case production line reference data (idempotent inserts).
INSERT INTO facilities (id, name, description, unit)
VALUES
  (2, 'Drone Case Production Line', 'Case line that forms, fits, assembles, inspects, and stocks drone transport cases as manufactured sub-assembly inventory for drone packaging pull.', 'sq ft')
ON CONFLICT (id) DO NOTHING;

INSERT INTO zones (
  id, facility_id, name, zone_type, process_order, area_sq_ft, primary_flow,
  status, description, map_x, map_y, map_width, map_height
)
VALUES
  ('case_receiving', 2, 'Case Receiving', 'dock', 1, 900, 'Supplier receipt to Case Staging', 'Open',
   'Inbound dock for case shells, foam stock, and case hardware deliveries.', 90, 560, 120, 90),
  ('case_raw', 2, 'Case Material Staging', 'warehouse', 2, 3200, 'Case Staging to Shell Forming', 'Kits ready',
   'Stages molded shell blanks, foam blocks, and hardware kits for case orders.', 240, 560, 120, 90),
  ('cws1', 2, 'Case WS1: Shell Forming + Trim', 'workstation', 3, 1600, 'Shell Forming to Foam Fit', 'Fixtures set',
   'Forms and trims shell halves and checks hinge and latch mounting surfaces.', 390, 560, 120, 90),
  ('cws2', 2, 'Case WS2: Foam Cutting + Fit', 'workstation', 4, 1400, 'Foam Fit to Hardware', 'Templates loaded',
   'Cuts foam inserts and verifies drone, battery, and accessory cavity fit.', 540, 560, 120, 90),
  ('cws3', 2, 'Case WS3: Hardware + Assembly', 'workstation', 5, 1500, 'Hardware to Inspection', 'Torque verified',
   'Installs hinges, latches, handle, seal kit, and fastener hardware.', 690, 560, 120, 90),
  ('cws4', 2, 'Case WS4: Inspection + Label', 'workstation', 6, 1100, 'Inspection to Case FG', 'Gauges ready',
   'Inspects closure, seal, and fit, then applies serial and compliance labels.', 840, 560, 120, 90),
  ('case_fg', 2, 'Case Finished Goods', 'warehouse', 7, 1800, 'Case FG to Case Inventory', 'Ready to move',
   'Holds accepted finished cases before stocking into case inventory.', 990, 560, 120, 90),
  ('case_inventory', 2, 'Case Inventory', 'warehouse', 8, 2200, 'Case stock to drone packaging pull', 'Available',
   'Stores finished drone transport cases as pull stock for drone packaging.', 990, 430, 120, 90)
ON CONFLICT (id) DO NOTHING;

INSERT INTO materials (id, sku, name, material_type, default_zone_id)
VALUES
  (7, 'CASE-KIT-100', 'Case Material Kit', 'raw', 'case_raw'),
  (8, 'CASE-SHELL-200', 'Formed Case Shell Set', 'wip', 'cws1'),
  (9, 'CASE-FOAM-300', 'Foam Fitted Case', 'wip', 'cws2'),
  (10, 'CASE-ASSY-400', 'Assembled Case', 'wip', 'cws3'),
  (11, 'CASE-FG-500', 'Drone Transport Case', 'finished', 'case_fg')
ON CONFLICT (id) DO NOTHING;

INSERT INTO process_steps (
  id, facility_id, step_number, source_zone_id, target_zone_id,
  name, expected_minutes, route_type
)
VALUES
  ('case_receive_to_staging', 2, 1, 'case_receiving', 'case_raw', 'Receive and stage case materials', 20, 'material'),
  ('case_staging_to_forming', 2, 2, 'case_raw', 'cws1', 'Issue materials to shell forming', 12, 'material'),
  ('case_forming_to_foam', 2, 3, 'cws1', 'cws2', 'Move formed shells to foam cutting and fit', 25, 'material'),
  ('case_foam_to_hardware', 2, 4, 'cws2', 'cws3', 'Move fitted case to hardware assembly', 18, 'material'),
  ('case_hardware_to_inspect', 2, 5, 'cws3', 'cws4', 'Move assembled case to inspection and label', 15, 'material'),
  ('case_inspect_to_fg', 2, 6, 'cws4', 'case_fg', 'Release inspected case to case finished goods', 10, 'material'),
  ('case_fg_to_inventory', 2, 7, 'case_fg', 'case_inventory', 'Stock finished cases into case inventory', 8, 'material')
ON CONFLICT (id) DO NOTHING;

INSERT INTO inventory_balances (material_id, zone_id, quantity_on_hand, reorder_point)
VALUES
  (7, 'case_raw', 600, 150),
  (11, 'case_inventory', 4, 2)
ON CONFLICT (material_id, zone_id) DO NOTHING;

INSERT INTO inventory_items (
  area, location_zone_id, item_name, part_number, quantity_on_hand,
  quantity_allocated, unit, min_quantity, max_quantity, status, control_note
)
VALUES
  ('Parts', 'cws1', 'Molded shell blank set', 'CASE-SHL-001', 14, 1, 'set', 6, 24, 'Ready',
   'Trim within one shift of molding lot release.'),
  ('Parts', 'cws2', 'Foam insert block set', 'CASE-FOAM-001', 10, 1, 'set', 6, 20, 'Ready',
   'Cut cavities against the current drone fit template.'),
  ('Parts', 'cws3', 'Latch set', 'CASE-LTC-001', 30, 2, 'each', 12, 48, 'Ready',
   'Torque latches to spec and check engagement.'),
  ('Parts', 'cws4', 'Case label and badge set', 'CASE-LBL-001', 16, 1, 'set', 8, 30, 'Ready',
   'Apply serial badge after inspection pass.'),
  ('Production', 'cws1', 'Case forming WIP', 'CASE-WIP-FORM', 0, 0, 'each', 0, 4, 'Open capacity',
   'Active only while a case order is at shell forming.'),
  ('Sub-assembly', 'case_inventory', 'Drone transport case', 'CASE-FG-500', 4, 0, 'each', 2, 16, 'Ready',
   'Manufactured case stock pulled by drone packaging.')
ON CONFLICT (area, location_zone_id, part_number) DO NOTHING;

INSERT INTO bom_items (
  parent_material_id, part_number, description, category, quantity, unit,
  station_zone_id, source_zone_id, supply_type, notes
)
VALUES
  (11, 'CASE-FG-500', 'Drone transport case', 'Finished good', 1, 'each', 'case_inventory', NULL, 'make', 'Top-level manufactured carry case'),
  (11, 'CASE-SHL-001', 'Molded shell blank, top and bottom', 'Shell', 1, 'set', 'cws1', NULL, 'buy', 'Molded halves trimmed and prepped at shell forming'),
  (11, 'CASE-FOAM-001', 'Foam insert block set', 'Interior', 1, 'set', 'cws2', NULL, 'buy', 'Cavities cut for drone, batteries, props, and accessories'),
  (11, 'CASE-HNG-001', 'Hinge set', 'Hardware', 1, 'set', 'cws3', NULL, 'buy', 'Continuous hinge with pins and screws'),
  (11, 'CASE-LTC-001', 'Latch set', 'Hardware', 2, 'each', 'cws3', NULL, 'buy', 'Two locking draw latches per case'),
  (11, 'CASE-HDL-001', 'Carry handle', 'Hardware', 1, 'each', 'cws3', NULL, 'buy', 'Spring-loaded carry handle'),
  (11, 'CASE-SEAL-001', 'Gasket seal and valve kit', 'Hardware', 1, 'set', 'cws3', NULL, 'buy', 'O-ring gasket and pressure relief valve'),
  (11, 'CASE-HW-001', 'Case fastener kit', 'Hardware', 1, 'set', 'cws3', NULL, 'buy', 'Rivets, screws, and inserts for case hardware'),
  (11, 'CASE-LBL-001', 'Case label and badge set', 'Finishing', 1, 'set', 'cws4', NULL, 'buy', 'Serial badge and compliance labels'),
  (6, 'CASE-FG-500', 'Drone transport case', 'Packaging', 1, 'each', 'fg', 'case_inventory', 'make', 'Manufactured carry case pulled from Case Inventory at drone packaging')
ON CONFLICT (parent_material_id, part_number) DO NOTHING;

-- Keep identity sequences ahead of explicitly inserted ids.
SELECT setval(pg_get_serial_sequence('facilities', 'id'), (SELECT MAX(id) FROM facilities));
SELECT setval(pg_get_serial_sequence('materials', 'id'), (SELECT MAX(id) FROM materials));
