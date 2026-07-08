# Manufacturing Floor Database

This folder contains the PostgreSQL schema, seed data, and setup script for the drone manufacturing floor map prototype. It covers two production lines:

- **Facility 1 — Drone floor**: builds packaged inspection drones (`DRN-FG-600`).
- **Facility 2 — Case line**: manufactures drone transport cases (`CASE-FG-500`) and stocks them into Case Inventory. The drone BOM pulls one case per drone from that stock: the pull is allocated when a drone order is created and consumed when the drone order completes.

## Files

- `schema.sql` defines the tables and the production order functions.
- `seed.sql` inserts both production lines: floor plans, process flows, BOMs, inventory, and work orders.
- `setup_postgres.py` creates the schema, seeds PostgreSQL, and verifies row counts and both facility flows.
- `create_production_order.py` calls PostgreSQL to create a production order from the database function.
- `migrations/001_case_line_fixup.sql` upgrades a database created before the case line (BOM constraint fixup plus case line seed data). Fresh installs do not need it.

## Setup

Set `DATABASE_URL` to the PostgreSQL database you want to use, then run the setup script.

Example connection string:

```text
postgresql://postgres:your_pgadmin_password@localhost:5432/manufacturing_floor
```

Example command:

```powershell
$env:DATABASE_URL = "postgresql://postgres:your_pgadmin_password@localhost:5432/manufacturing_floor"
python database/setup_postgres.py
```

## Main Tables

- `facilities`: the drone manufacturing floor (1) and the case production line (2).
- `zones`: drone floor zones plus the case line (case receiving, staging, shell forming, foam fit, hardware, inspection, case FG, and case inventory).
- `materials`: drone and case kits, WIP stages, and finished goods.
- `process_steps`: per-facility movement sequence; each facility runs its own route.
- `inventory_balances`: aggregate current quantity by material and zone.
- `inventory_items`: detailed parts, production WIP, sub-assembly stock (finished cases), rework hold, and finished goods inventory with on-hand, allocated, available, min/max, and status.
- `work_orders`: sample work orders tied to the process map.
- `bom_items`: bill of materials lines per parent (drone BOM and case BOM). Part numbers are unique per parent BOM, and a line with `source_zone_id` set is an inventory pull from that zone: the drone BOM pulls `CASE-FG-500` from `case_inventory` at packaging.
- `production_orders`: production order header from raw material release to finished goods.
- `production_order_materials`: BOM requirements, issue quantities, consumption quantities, and shortage status by production order.
- `production_order_operations`: station routing, labor minutes, quantity in/out, scrap, and operation status.
- `workstation_balances`: WIP, completed, and hold balances by workstation for each production order.
- `production_order_activity`: activity ledger for created, released, issued, started, moved, completed, held, and reworked events.
- `inventory_transactions`: inventory movement ledger tied to production orders and inventory items.
- `production_workstation_ledger`: accounting-facing transaction ledger by workstation with quantity in, quantity out, adjustments, balance after, event code, reference, and notes.

## Create Production Order

Order numbers are unique in PostgreSQL and indexed by `idx_production_orders_order_no_unique`.
Use this helper to see the next available number (drone orders use the `DRN-PO-` prefix, case orders use `CASE-PO-`):

```sql
SELECT next_production_order_no();            -- next DRN-PO number
SELECT next_production_order_no('CASE-PO-');  -- next CASE-PO number
```

The schema includes a PostgreSQL function that creates a production order and initializes the material demand, routing operations, workstation balances, and opening ledger records. It builds either finished good; the facility and route come from the SKU:

```sql
SELECT create_production_order('DRN-PO-1002', 1, '2026-06-14', '2026-06-04');
SELECT create_production_order('CASE-PO-1002', 2, '2026-06-14', '2026-06-04', 'CASE-FG-500');
```

Arguments are `order_no`, `quantity`, `due_date`, optional `start_date`, optional `finished_sku` (defaults to `DRN-FG-600`), and optional `auto_replenish` (defaults to true). Creating a drone order also allocates one transport case per drone from Case Inventory. If stock is short, the case line shows status `short` AND a linked replenishment case order is auto-created for the shortfall, so the case line starts building immediately. When replenishment stock lands in Case Inventory, waiting short lines are automatically allocated (oldest order first).

You can also create one from PowerShell after `DATABASE_URL` is set:

```powershell
python database/create_production_order.py DRN-PO-1002 1 2026-06-14 --start-date 2026-06-04
python database/create_production_order.py CASE-PO-1002 2 2026-06-14 --sku CASE-FG-500
```

When an order completes on the simulated floor, the server books the finished quantity into stock at the route's final zone (cases into Case Inventory, drones into FG Inventory) and consumes any allocated inventory pulls (the drone order's case).

## Workstation Capacity

Each workstation zone has a `capacity` (units it can work on at once; NULL = unconstrained). Orders larger than a station's capacity run in batches, multiplying that station's cycle time in the floor simulation — a quantity-10 order through a capacity-2 station takes 5 cycles. Warehouses and docks are unconstrained. Capacities are seeded per workstation and editable from the floor maps (click a workstation → Capacity field in the detail panel, backed by `POST /api/zone-capacity` with `{zoneId, capacity|null}`; workstations only). Existing databases get the column via `migrations/002_workstation_capacity.sql`.

## Timing Test Logic

For dashboard testing, a production order defaults to a 5-minute end-to-end test cycle. Set `TEST_TOTAL_MINUTES` before starting `server.py` to tune the full test duration. For example, `TEST_TOTAL_MINUTES=10` makes the simulated production route complete in 10 minutes. If `TEST_TOTAL_MINUTES` is not set to a positive value, the server falls back to `TEST_TIME_FACTOR`. The UI also computes actual-time utilization as:

```text
actual elapsed minutes / recorded route minutes * 100
```

This lets the dashboard move quickly for testing while still showing how much of the full recorded production time has actually been used.

## Inventory Check

After running the setup script, this query should return the same inventory rows shown in the prototype inventory page:

```sql
SELECT
  area,
  z.name AS location,
  item_name,
  part_number,
  quantity_on_hand AS on_hand,
  quantity_allocated AS allocated,
  quantity_available AS available,
  min_quantity AS min,
  max_quantity AS max,
  status
FROM inventory_items i
JOIN zones z ON z.id = i.location_zone_id
ORDER BY
  CASE area
    WHEN 'Parts' THEN 1
    WHEN 'Production' THEN 2
    WHEN 'Sub-assembly' THEN 3
    WHEN 'Finished Goods' THEN 4
  END,
  i.id;
```
