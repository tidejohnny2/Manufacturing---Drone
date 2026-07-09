-- Planning suite: order priority/re-sequencing and the plant working-hours
-- calendar. For databases created before this feature; fresh databases get
-- both from schema.sql. Safe to re-run.

-- Queue sequence: lower runs first, created_at breaks ties. Re-sequencing
-- assigns 10, 20, 30... so new orders (default 1000) join the back of the line.
ALTER TABLE production_orders ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 1000;

-- Single-row plant working-hours calendar. NULL work_start/work_end (or no
-- selected days) means 24/7. Times are wall-clock in time_zone; work_days is a
-- comma-separated list of weekday numbers, Monday = 0.
CREATE TABLE IF NOT EXISTS plant_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  work_start TIME,
  work_end TIME,
  work_days TEXT,
  time_zone TEXT NOT NULL DEFAULT 'UTC'
);
INSERT INTO plant_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
