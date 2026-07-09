INSERT INTO facilities (id, name, description, unit)
VALUES
  (1, 'Drone Manufacturing Floor Map', 'Prototype drone production floor with component kitting, airframe build, electronics integration, firmware calibration, motor testing, QA flight testing, packaging, and finished-goods storage.', 'sq ft');

INSERT INTO zones (
  id, facility_id, name, zone_type, process_order, area_sq_ft, primary_flow,
  status, description, capacity, map_x, map_y, map_width, map_height
)
VALUES
  ('receiving', 1, 'Receiving', 'dock', 1, 1800, 'Supplier receipt to Kitting', 'Open',
   'Inbound dock for supplier deliveries and drone component receiving checks.', NULL, 90, 160, 140, 90),
  ('raw', 1, 'Drone Component Kitting', 'warehouse', 2, 12500, 'Receiving to Airframe', 'Kits ready',
   'Receives and stages frames, motors, ESCs, flight controllers, batteries, sensors, propellers, fasteners, and packaging.', NULL, 285, 160, 145, 90),
  ('ws1', 1, 'Workstation 1: Airframe + Motors', 'workstation', 3, 3800, 'Kitting to Electronics', 'Torque verified',
   'Builds the drone frame, mounts arms and motors, routes motor wires, and checks frame alignment before tightening.', 3, 475, 160, 140, 90),
  ('ws2', 1, 'Workstation 2: Electronics + Power', 'workstation', 4, 4100, 'Airframe to Firmware', 'ESD controlled',
   'Installs ESCs, flight controller, receiver, power distribution, battery leads, GPS, cameras, and sensor modules.', 3, 660, 160, 160, 90),
  ('ws3', 1, 'Workstation 3: Firmware + Calibration', 'workstation', 5, 3200, 'Electronics to Motor Test', 'Profiles loaded',
   'Flashes firmware, configures flight controller orientation, binds transmitter and receiver, and calibrates sensors.', 2, 865, 160, 160, 90),
  ('ws4', 1, 'Workstation 4: Motor/ESC Test + Props', 'workstation', 6, 3450, 'Calibration to QA', 'Guarded test stand',
   'Runs motor direction and throttle tests without propellers, then installs matched clockwise and counterclockwise props.', 2, 865, 405, 160, 90),
  ('ws5', 1, 'Workstation 5: Final QA + Flight Test', 'workstation', 7, 4800, 'QA to Packaging', 'Flight cage active',
   'Verifies stability, flight control, GPS, camera/sensor function, communications, balance, and final acceptance.', 2, 660, 405, 160, 90),
  ('fg', 1, 'Finished Goods: Packaged Drones', 'warehouse', 8, 9800, 'Pack to FG Inventory', 'Ready to move',
   'Completes final inspection, documentation, and packaging before transfer to finished goods inventory.', NULL, 475, 405, 140, 90),
  ('inventory', 1, 'FG Inventory', 'warehouse', 9, 7500, 'Finished Goods Inventory', 'Available',
   'Stores packaged finished drones as ready stock for allocation, picking, and shipment release.', NULL, 90, 405, 140, 90);

INSERT INTO materials (id, sku, name, material_type, default_zone_id)
VALUES
  (1, 'DRN-KIT-100', 'Drone Component Kit', 'raw', 'raw'),
  (2, 'DRN-AIR-200', 'Airframe With Motors', 'wip', 'ws1'),
  (3, 'DRN-ELEC-300', 'Integrated Electronics Drone', 'wip', 'ws2'),
  (4, 'DRN-FW-400', 'Calibrated Drone', 'wip', 'ws3'),
  (5, 'DRN-QA-500', 'QA Accepted Drone', 'wip', 'ws5'),
  (6, 'DRN-FG-600', 'Packaged Finished Drone', 'finished', 'fg');

INSERT INTO process_steps (
  id, facility_id, step_number, source_zone_id, target_zone_id,
  name, expected_minutes, route_type
)
VALUES
  ('receive_to_kitting', 1, 1, 'receiving', 'raw', 'Receive and kit drone components', 35, 'material'),
  ('kitting_to_airframe', 1, 2, 'raw', 'ws1', 'Issue kit to airframe and motor build', 20, 'material'),
  ('airframe_to_electronics', 1, 3, 'ws1', 'ws2', 'Move airframe to electronics integration', 18, 'material'),
  ('electronics_to_firmware', 1, 4, 'ws2', 'ws3', 'Move integrated drone to firmware calibration', 15, 'material'),
  ('firmware_to_motor_test', 1, 5, 'ws3', 'ws4', 'Move calibrated drone to motor and ESC test', 15, 'material'),
  ('motor_test_to_qa', 1, 6, 'ws4', 'ws5', 'Move tested drone to QA and flight test', 25, 'material'),
  ('qa_to_packaging', 1, 7, 'ws5', 'fg', 'Release QA accepted drone to packaging', 20, 'material'),
  ('packaging_to_inventory', 1, 8, 'fg', 'inventory', 'Move packaged drones to finished goods inventory', 18, 'material');

INSERT INTO inventory_balances (material_id, zone_id, quantity_on_hand, reorder_point)
VALUES
  (1, 'raw', 2400, 600),
  (2, 'ws1', 120, 50),
  (3, 'ws2', 95, 40),
  (4, 'ws3', 70, 35),
  (5, 'ws5', 42, 20),
  (6, 'fg', 420, 120);

INSERT INTO inventory_items (
  area, location_zone_id, item_name, part_number, quantity_on_hand,
  quantity_allocated, unit, min_quantity, max_quantity, status, control_note
)
VALUES
  ('Parts', 'raw', 'Carbon fiber main frame kit', 'DRN-FRM-001', 12, 1, 'set', 5, 20, 'Ready',
   'Keep frames in matched hardware lots.'),
  ('Parts', 'raw', 'Carbon fiber arm set', 'DRN-ARM-001', 12, 1, 'set', 5, 20, 'Ready',
   'Pull as complete quadcopter set.'),
  ('Parts', 'raw', 'Brushless motor set', 'DRN-MTR-001', 48, 4, 'each', 20, 80, 'Ready',
   'Maintain clockwise and counterclockwise balance.'),
  ('Parts', 'ws2', 'Electronic speed controller', 'DRN-ESC-001', 40, 4, 'each', 20, 80, 'Ready',
   'Lot trace ESCs used on each drone.'),
  ('Parts', 'ws2', 'Flight controller with IMU', 'DRN-FC-001', 10, 1, 'each', 4, 16, 'Ready',
   'Serialized control item.'),
  ('Parts', 'ws2', 'Lithium polymer flight battery', 'DRN-BAT-001', 8, 1, 'each', 6, 18, 'Watch',
   'Store in battery-safe area and check charge cycle count.'),
  ('Parts', 'ws4', 'Propeller clockwise/counterclockwise set', 'DRN-PROP-001', 18, 1, 'set', 8, 30, 'Ready',
   'Install only after motor direction test passes.'),
  ('Parts', 'fg', 'Protective packaging kit', 'DRN-PKG-001', 9, 1, 'set', 8, 25, 'Reorder',
   'Below preferred stock after current build allocation.'),
  ('Production', 'ws1', 'Drone assembly WIP', 'DRN-WIP-AIR', 1, 1, 'each', 0, 3, 'In process',
   'Unit is active only while mechanical build is open.'),
  ('Production', 'ws2', 'Drone assembly WIP', 'DRN-WIP-ELEC', 0, 0, 'each', 0, 3, 'Open capacity',
   'Receives unit after Airframe quality gate passes.'),
  ('Production', 'ws3', 'Configured drone WIP', 'DRN-WIP-FW', 0, 0, 'each', 0, 2, 'Open capacity',
   'Hold firmware version and serial number together.'),
  ('Production', 'ws5', 'Inspection WIP', 'DRN-WIP-QA', 0, 0, 'each', 0, 2, 'Open capacity',
   'QA WIP cannot release without completed test record.'),
  ('Production', 'raw', 'Rework hold', 'DRN-WIP-RWK', 0, 0, 'each', 0, 2, 'Clear',
   'Use only for failed inspection or blocked material disposition.'),
  ('Finished Goods', 'inventory', 'Packaged inspection drone', 'DRN-FG-600', 3, 1, 'each', 2, 12, 'Ready',
   'Available only after package scan and storage location assignment.');

INSERT INTO work_orders (work_order_no, material_id, current_zone_id, quantity, status, due_date)
VALUES
  ('DRN-WO-1001', 6, 'ws1', 80, 'in_progress', '2026-06-08'),
  ('DRN-WO-1002', 6, 'ws3', 120, 'in_progress', '2026-06-10'),
  ('DRN-WO-1003', 6, 'fg', 60, 'complete', '2026-06-05'),
  ('DRN-WO-1004', 6, 'raw', 150, 'planned', '2026-06-12');

INSERT INTO bom_items (
  parent_material_id, part_number, description, category, quantity, unit,
  station_zone_id, supply_type, notes
)
VALUES
  (6, 'DRN-FG-600', 'Packaged finished inspection drone', 'Finished good', 1, 'each', 'inventory', 'make', 'Top-level finished drone assembly'),
  (6, 'DRN-FRM-001', 'Carbon fiber main frame kit', 'Airframe', 1, 'set', 'ws1', 'buy', 'Includes top plate, bottom plate, and standoffs'),
  (6, 'DRN-ARM-001', 'Carbon fiber arm set', 'Airframe', 1, 'set', 'ws1', 'buy', 'Four arms for quadcopter frame'),
  (6, 'DRN-HDW-001', 'Fastener and thread-lock kit', 'Airframe', 1, 'set', 'ws1', 'buy', 'Screws, spacers, lock nuts, and thread locker'),
  (6, 'DRN-MTR-001', 'Brushless motor clockwise/counterclockwise set', 'Propulsion', 4, 'each', 'ws1', 'buy', 'Matched motors mounted to arms'),
  (6, 'DRN-ESC-001', 'Electronic speed controller', 'Propulsion', 4, 'each', 'ws2', 'buy', 'One ESC per motor'),
  (6, 'DRN-PDB-001', 'Power distribution board', 'Power', 1, 'each', 'ws2', 'buy', 'Distributes battery power to ESCs and flight controller'),
  (6, 'DRN-BAT-001', 'Lithium polymer flight battery', 'Power', 1, 'each', 'ws2', 'buy', 'Production battery pack'),
  (6, 'DRN-PWR-001', 'XT60 battery lead and capacitor kit', 'Power', 1, 'set', 'ws2', 'buy', 'Main power lead filtering and strain relief'),
  (6, 'DRN-FC-001', 'Flight controller with IMU', 'Controls', 1, 'each', 'ws2', 'buy', 'Mounted with vibration isolation'),
  (6, 'DRN-RX-001', 'Radio receiver', 'Controls', 1, 'each', 'ws2', 'buy', 'Bound during firmware calibration'),
  (6, 'DRN-GPS-001', 'GPS and compass module', 'Navigation', 1, 'each', 'ws2', 'buy', 'Optional by model but included in prototype BOM'),
  (6, 'DRN-CAM-001', 'Camera/sensor payload module', 'Payload', 1, 'each', 'ws2', 'buy', 'Camera or inspection sensor payload'),
  (6, 'DRN-WIR-001', 'Wiring harness and heat shrink kit', 'Electrical', 1, 'set', 'ws2', 'buy', 'Signal wires, power leads, sleeves, and ties'),
  (6, 'DRN-FW-001', 'Firmware image and configuration profile', 'Software', 1, 'each', 'ws3', 'make', 'Loaded and configured per drone'),
  (6, 'DRN-PROP-001', 'Propeller clockwise/counterclockwise set', 'Propulsion', 1, 'set', 'ws4', 'buy', 'Installed after motor direction checks'),
  (6, 'DRN-LBL-001', 'Serial label and compliance label set', 'Packaging', 1, 'set', 'fg', 'buy', 'Applied after final acceptance'),
  (6, 'DRN-PKG-001', 'Protective packaging kit', 'Packaging', 1, 'set', 'fg', 'buy', 'Box, foam inserts, manuals, and accessories bag'),
  (6, 'DRN-DOC-001', 'Inspection and test record', 'Quality', 1, 'each', 'ws5', 'make', 'Traceability record from QA release');

-- Drone case production line: manufactures drone transport cases and stocks them
-- into Case Inventory, where drone packaging pulls them as a BOM component.
INSERT INTO facilities (id, name, description, unit)
VALUES
  (2, 'Drone Case Production Line', 'Case line that forms, fits, assembles, inspects, and stocks drone transport cases as manufactured sub-assembly inventory for drone packaging pull.', 'sq ft');

INSERT INTO zones (
  id, facility_id, name, zone_type, process_order, area_sq_ft, primary_flow,
  status, description, capacity, map_x, map_y, map_width, map_height
)
VALUES
  ('case_receiving', 2, 'Case Receiving', 'dock', 1, 900, 'Supplier receipt to Case Staging', 'Open',
   'Inbound dock for case shells, foam stock, and case hardware deliveries.', NULL, 90, 560, 120, 90),
  ('case_raw', 2, 'Case Material Staging', 'warehouse', 2, 3200, 'Case Staging to Shell Forming', 'Kits ready',
   'Stages molded shell blanks, foam blocks, and hardware kits for case orders.', NULL, 240, 560, 120, 90),
  ('cws1', 2, 'Case WS1: Shell Forming + Trim', 'workstation', 3, 1600, 'Shell Forming to Foam Fit', 'Fixtures set',
   'Forms and trims shell halves and checks hinge and latch mounting surfaces.', 4, 390, 560, 120, 90),
  ('cws2', 2, 'Case WS2: Foam Cutting + Fit', 'workstation', 4, 1400, 'Foam Fit to Hardware', 'Templates loaded',
   'Cuts foam inserts and verifies drone, battery, and accessory cavity fit.', 4, 540, 560, 120, 90),
  ('cws3', 2, 'Case WS3: Hardware + Assembly', 'workstation', 5, 1500, 'Hardware to Inspection', 'Torque verified',
   'Installs hinges, latches, handle, seal kit, and fastener hardware.', 5, 690, 560, 120, 90),
  ('cws4', 2, 'Case WS4: Inspection + Label', 'workstation', 6, 1100, 'Inspection to Case FG', 'Gauges ready',
   'Inspects closure, seal, and fit, then applies serial and compliance labels.', 3, 840, 560, 120, 90),
  ('case_fg', 2, 'Case Finished Goods', 'warehouse', 7, 1800, 'Case FG to Case Inventory', 'Ready to move',
   'Holds accepted finished cases before stocking into case inventory.', NULL, 990, 560, 120, 90),
  ('case_inventory', 2, 'Case Inventory', 'warehouse', 8, 2200, 'Case stock to drone packaging pull', 'Available',
   'Stores finished drone transport cases as pull stock for drone packaging.', NULL, 990, 430, 120, 90);

INSERT INTO materials (id, sku, name, material_type, default_zone_id)
VALUES
  (7, 'CASE-KIT-100', 'Case Material Kit', 'raw', 'case_raw'),
  (8, 'CASE-SHELL-200', 'Formed Case Shell Set', 'wip', 'cws1'),
  (9, 'CASE-FOAM-300', 'Foam Fitted Case', 'wip', 'cws2'),
  (10, 'CASE-ASSY-400', 'Assembled Case', 'wip', 'cws3'),
  (11, 'CASE-FG-500', 'Drone Transport Case', 'finished', 'case_fg');

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
  ('case_fg_to_inventory', 2, 7, 'case_fg', 'case_inventory', 'Stock finished cases into case inventory', 8, 'material');

INSERT INTO inventory_balances (material_id, zone_id, quantity_on_hand, reorder_point)
VALUES
  (7, 'case_raw', 600, 150),
  (11, 'case_inventory', 4, 2);

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
   'Manufactured case stock pulled by drone packaging.');

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
  (6, 'CASE-FG-500', 'Drone transport case', 'Packaging', 1, 'each', 'fg', 'case_inventory', 'make', 'Manufactured carry case pulled from Case Inventory at drone packaging');

-- Material-release attributes: serialized parts and approved substitutes.
-- (schema.sql runs these too, but on a fresh database the BOM rows only exist
-- after the inserts above, so they must run again here.)
UPDATE bom_items SET serialized = TRUE
WHERE part_number IN ('DRN-MTR-001', 'DRN-ESC-001', 'DRN-FC-001', 'DRN-BAT-001', 'CASE-FG-500');
UPDATE bom_items SET substitute_part_number = 'DRN-PROP-002 (alt vendor matched set)'
WHERE part_number = 'DRN-PROP-001';
UPDATE bom_items SET substitute_part_number = 'CASE-LTC-002 (equivalent draw latch)'
WHERE part_number = 'CASE-LTC-001';

SELECT create_production_order('CASE-PO-1001', 2, '2026-06-12', '2026-06-04', 'CASE-FG-500');
SELECT create_production_order('DRN-PO-1001', 1, '2026-06-14', '2026-06-04');

-- Plant working-hours calendar: single row, defaults to 24/7 until edited.
INSERT INTO plant_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
