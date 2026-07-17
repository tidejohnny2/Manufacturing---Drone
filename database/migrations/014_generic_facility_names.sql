-- 014: Genericize facility/app identity — station (zone) names and the
-- plant company name lose "drone". These are facility configuration, not
-- product data: the materials/BOM/standard-cost records keep "drone" (we
-- still make drones), so DB-driven product displays still show it.
-- Idempotent (guarded by the old value); mirrored in schema.sql.

UPDATE zones SET name = 'Component Kitting'
  WHERE id = 'raw' AND name = 'Drone Component Kitting';
UPDATE zones SET name = 'Finished Goods: Packaged Units'
  WHERE id = 'fg' AND name = 'Finished Goods: Packaged Drones';

UPDATE zones SET description = 'Inbound dock for supplier deliveries and component receiving checks.'
  WHERE id = 'receiving' AND description LIKE '%drone component receiving%';
UPDATE zones SET description = 'Stores packaged finished units as ready stock for allocation, picking, and shipment release.'
  WHERE id = 'inventory' AND description LIKE '%packaged finished drones%';
UPDATE zones SET description = 'Stores finished transport cases as pull stock for packaging.'
  WHERE id = 'case_inventory' AND description LIKE '%drone transport cases%';
UPDATE zones SET description = 'Builds the frame, mounts arms and motors, routes motor wires, and checks frame alignment before tightening.'
  WHERE id = 'ws1' AND description LIKE '%drone frame%';
UPDATE zones SET description = 'Cuts foam inserts and verifies unit, battery, and accessory cavity fit.'
  WHERE id = 'cws2' AND description LIKE '%verifies drone%';

UPDATE companies SET name = 'Manufacturing Plant'
  WHERE id = 1 AND name = 'Drone Plant';
