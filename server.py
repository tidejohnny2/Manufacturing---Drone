import csv
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row


BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL")
ACTIVE_STATUSES = ("planned", "released", "in_progress", "hold")
DEFAULT_FINISHED_SKU = "DRN-FG-600"
ORDER_PREFIXES = {"DRN-FG-600": "DRN-PO-", "CASE-FG-500": "CASE-PO-"}
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
# Stamped on drone build records at completion (cases carry no firmware).
DRONE_FIRMWARE_VERSION = os.environ.get("DRONE_FIRMWARE_VERSION", "FW 4.2.1")
ASK_AI_SYSTEM = (
    "You are the Ask AI assistant embedded in the Operations Overview of a drone "
    "manufacturing demo plant. The plant has two production lines: the drone floor "
    "(builds DRN-FG-600 packaged inspection drones) and the case line (builds "
    "CASE-FG-500 transport cases, stocked into Case Inventory and pulled by drone "
    "packaging; shortages auto-create replenishment case orders). Answer questions "
    "using ONLY the live data snapshot provided with each question. Be concise and "
    "operational. Use markdown bullet lists or small tables when they help. If the "
    "snapshot does not contain the answer, say so briefly. Quantities are in each "
    "unless stated otherwise."
)


def ask_ai_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def order_prefix_for(finished_sku: str) -> str:
    return ORDER_PREFIXES.get(finished_sku, finished_sku.split("-")[0] + "-PO-")


def read_test_total_minutes() -> float | None:
    raw_value = os.environ.get("TEST_TOTAL_MINUTES", "5")
    try:
        minutes = float(raw_value)
    except ValueError:
        return 5
    return minutes if minutes > 0 else None


def read_simulation_factor() -> float:
    raw_value = os.environ.get("TEST_TIME_FACTOR", "0.05")
    try:
        factor = float(raw_value)
    except ValueError:
        factor = 0.05
    return min(max(factor, 0.001), 1.0)


def simulation_factor_for(recorded_minutes: float) -> float:
    test_total_minutes = read_test_total_minutes()
    if test_total_minutes and recorded_minutes > 0:
        return min(max(test_total_minutes / recorded_minutes, 0.001), 1.0)
    return read_simulation_factor()


def read_plant_settings(cur) -> dict:
    """The single plant_settings row as the API shape; defaults mean 24/7."""
    cur.execute("SELECT to_regclass('plant_settings') AS reg")
    row = None
    if cur.fetchone()["reg"] is not None:
        cur.execute("SELECT work_start, work_end, work_days, time_zone FROM plant_settings WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return {"work_start": None, "work_end": None, "work_days": [], "time_zone": "UTC"}
    days = sorted(
        int(day) for day in (row["work_days"] or "").split(",") if day.strip().isdigit()
    )
    return {
        "work_start": row["work_start"].strftime("%H:%M") if row["work_start"] else None,
        "work_end": row["work_end"].strftime("%H:%M") if row["work_end"] else None,
        "work_days": days,
        "time_zone": row["time_zone"] or "UTC",
    }


def load_work_calendar(cur) -> dict | None:
    """Runtime calendar for schedule math, or None for 24/7."""
    settings = read_plant_settings(cur)
    if not settings["work_start"] or not settings["work_end"] or not settings["work_days"]:
        return None
    if settings["work_start"] == settings["work_end"]:
        return None
    start_hour, start_minute = (int(part) for part in settings["work_start"].split(":"))
    end_hour, end_minute = (int(part) for part in settings["work_end"].split(":"))
    try:
        plant_tz = ZoneInfo(settings["time_zone"])
    except Exception:
        plant_tz = timezone.utc
    return {
        "start": (start_hour, start_minute),
        "end": (end_hour, end_minute),
        "days": set(settings["work_days"]),
        "tz": plant_tz,
    }


def shift_window_for(day_start: datetime, calendar: dict) -> tuple[datetime, datetime]:
    """The working window that begins on the given plant-local day; an end at or
    before the start means an overnight shift running into the next day."""
    window_start = day_start.replace(hour=calendar["start"][0], minute=calendar["start"][1])
    window_end = day_start.replace(hour=calendar["end"][0], minute=calendar["end"][1])
    if window_end <= window_start:
        window_end += timedelta(days=1)
    return window_start, window_end


def next_working(moment: datetime, calendar: dict | None) -> datetime:
    """The given moment if the plant is working, else the next shift start."""
    if not calendar:
        return moment
    local = moment.astimezone(calendar["tz"])
    day_base = local.replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in range(-1, 15):
        day_start = day_base + timedelta(days=day_offset)
        if day_start.weekday() not in calendar["days"]:
            continue
        window_start, window_end = shift_window_for(day_start, calendar)
        if local < window_start:
            return window_start.astimezone(timezone.utc)
        if window_start <= local < window_end:
            return moment
    return moment


def add_working_minutes(moment: datetime, minutes: float, calendar: dict | None) -> datetime:
    """Advance by working minutes, skipping off-shift time entirely."""
    if not calendar:
        return moment + timedelta(minutes=minutes)
    remaining = minutes
    cursor = next_working(moment, calendar)
    for _ in range(400):
        local = cursor.astimezone(calendar["tz"])
        day_base = local.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end_utc = None
        for day_offset in (-1, 0):
            day_start = day_base + timedelta(days=day_offset)
            if day_start.weekday() not in calendar["days"]:
                continue
            window_start, window_end = shift_window_for(day_start, calendar)
            if window_start <= local < window_end:
                window_end_utc = window_end.astimezone(timezone.utc)
        if window_end_utc is None:
            cursor = next_working(cursor + timedelta(minutes=1), calendar)
            continue
        available = (window_end_utc - cursor).total_seconds() / 60
        if remaining <= available + 1e-9:
            return cursor + timedelta(minutes=remaining)
        remaining -= available
        cursor = next_working(window_end_utc, calendar)
    return cursor


def json_response(handler: SimpleHTTPRequestHandler, status: HTTPStatus, payload: dict) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def require_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Example: "
            "postgresql://postgres:your_pgadmin_password@localhost:5432/manufacturing_floor"
        )
    return DATABASE_URL


def fetch_route_steps(cur, facility_id: int = 1) -> list[dict]:
    cur.execute(
        """
        SELECT ps.step_number, ps.source_zone_id, source.name AS source_zone_name,
               source.capacity AS source_capacity,
               ps.target_zone_id, target.name AS target_zone_name,
               target.capacity AS target_capacity,
               ps.expected_minutes
        FROM process_steps ps
        JOIN zones source ON source.id = ps.source_zone_id
        JOIN zones target ON target.id = ps.target_zone_id
        WHERE ps.facility_id = %s
        ORDER BY ps.step_number
        """,
        (facility_id,),
    )
    return cur.fetchall()


def simulated_order_state(order: dict, route_steps: list[dict]) -> dict:
    elapsed_minutes = max(
        0,
        (datetime.now(timezone.utc) - order["created_at"]).total_seconds() / 60,
    )
    zone_order = ["receiving"] + [step["target_zone_id"] for step in route_steps]
    zone_names = {"receiving": "Receiving"}
    durations = {}
    standard_durations = {}

    for index, step in enumerate(route_steps):
        source_zone_id = "receiving" if index == 0 else step["source_zone_id"]
        zone_names[source_zone_id] = "Receiving" if source_zone_id == "receiving" else step["source_zone_name"]
        zone_names[step["target_zone_id"]] = step["target_zone_name"]
        standard_durations[source_zone_id] = step["expected_minutes"]

    recorded_minutes = sum(standard_durations.values())
    simulation_factor = simulation_factor_for(recorded_minutes)
    durations = {
        zone_id: expected_minutes * simulation_factor
        for zone_id, expected_minutes in standard_durations.items()
    }

    total_minutes = sum(durations.values())
    remaining = elapsed_minutes
    current_zone_id = zone_order[-1]
    completed_zone_ids = set(zone_order[:-1])
    status = "complete"
    current_elapsed = 0
    current_duration = 0

    for zone_id in zone_order[:-1]:
        duration = durations.get(zone_id, 0)
        if remaining < duration:
            current_zone_id = zone_id
            completed_zone_ids = set(zone_order[:zone_order.index(zone_id)])
            status = "in_progress" if elapsed_minutes > 0 else "planned"
            current_elapsed = remaining
            current_duration = duration
            break
        remaining -= duration

    percent_complete = 100 if total_minutes == 0 else min(100, round((elapsed_minutes / total_minutes) * 100))
    actual_time_utilization = 100 if recorded_minutes == 0 else min(100, round((elapsed_minutes / recorded_minutes) * 100, 1))

    balances = []
    for sequence_number, zone_id in enumerate(zone_order, start=1):
        is_current = zone_id == current_zone_id and status != "complete"
        is_done = zone_id in completed_zone_ids or (status == "complete" and zone_id == "inventory")
        balances.append(
            {
                "sequence_number": sequence_number,
                "zone_id": zone_id,
                "station": zone_names.get(zone_id, zone_id),
                "wip_quantity": order["quantity"] if is_current else 0,
                "completed_quantity": order["quantity"] if is_done else 0,
                "hold_quantity": 0,
                "operation_status": "in_progress" if is_current else ("complete" if is_done else "queued"),
            }
        )

    return {
        "current_zone_id": current_zone_id,
        "current_zone": zone_names.get(current_zone_id, current_zone_id),
        "production_status": status,
        "elapsed_minutes": round(elapsed_minutes, 1),
        "recorded_minutes": round(recorded_minutes, 1),
        "planned_test_minutes": round(total_minutes, 1),
        "percent_complete": percent_complete,
        "actual_time_utilization_percent": actual_time_utilization,
        "timing_adjustment_percent": round(simulation_factor * 100, 1),
        "station_elapsed_minutes": round(current_elapsed, 1),
        "station_test_minutes": round(current_duration, 1),
        "station_recorded_minutes": round(current_duration / simulation_factor if simulation_factor else 0, 1),
        "balances": balances,
    }


def fetch_active_orders(cur) -> list[dict]:
    cur.execute(
        """
        SELECT po.id, po.order_no, m.sku AS finished_good, po.quantity,
               po.status, po.current_zone_id, z.name AS current_zone,
               po.start_date, po.due_date, po.created_at, po.priority,
               fz.facility_id
        FROM production_orders po
        JOIN materials m ON m.id = po.finished_material_id
        JOIN zones fz ON fz.id = m.default_zone_id
        JOIN zones z ON z.id = po.current_zone_id
        WHERE po.status IN ('planned', 'released', 'in_progress', 'hold')
        ORDER BY po.priority, po.created_at, po.id
        """
    )
    return cur.fetchall()


def build_zone_model(route_steps: list[dict]) -> tuple[list[str], dict, dict, dict, dict]:
    entry_zone_id = route_steps[0]["source_zone_id"]
    zone_order = [entry_zone_id] + [step["target_zone_id"] for step in route_steps]
    zone_names = {entry_zone_id: route_steps[0]["source_zone_name"]}
    capacities = {entry_zone_id: route_steps[0]["source_capacity"]}
    durations = {}
    standard_durations = {}

    for step in route_steps:
        zone_names[step["source_zone_id"]] = step["source_zone_name"]
        zone_names[step["target_zone_id"]] = step["target_zone_name"]
        capacities[step["source_zone_id"]] = step["source_capacity"]
        capacities[step["target_zone_id"]] = step["target_capacity"]
        standard_durations[step["source_zone_id"]] = step["expected_minutes"]

    simulation_factor = simulation_factor_for(sum(standard_durations.values()))
    durations = {
        zone_id: expected_minutes * simulation_factor
        for zone_id, expected_minutes in standard_durations.items()
    }

    return zone_order, zone_names, durations, standard_durations, capacities


def simulated_pipeline_state(
    orders: list[dict], route_steps: list[dict], calendar: dict | None = None
) -> list[dict]:
    zone_order, zone_names, durations, standard_durations, capacities = build_zone_model(route_steps)
    single_pass_minutes = sum(durations.values())
    single_pass_recorded = sum(standard_durations.values())
    simulation_factor = (
        single_pass_minutes / single_pass_recorded if single_pass_recorded else read_simulation_factor()
    )
    station_available = {
        zone_id: datetime.min.replace(tzinfo=timezone.utc)
        for zone_id in zone_order[:-1]
    }
    states = []
    now = datetime.now(timezone.utc)

    for order in orders:
        arrival_time = order["created_at"]
        schedule = []
        previous_end = arrival_time
        recorded_minutes = 0

        for zone_id in zone_order[:-1]:
            # Batch capacity: a station works on at most `capacity` units at a
            # time, so an order larger than capacity multiplies the cycle time.
            capacity = capacities.get(zone_id)
            batches = math.ceil(order["quantity"] / capacity) if capacity else 1
            duration = durations.get(zone_id, 0) * batches
            recorded_minutes += standard_durations.get(zone_id, 0) * batches
            start_time = next_working(max(previous_end, station_available[zone_id]), calendar)
            end_time = add_working_minutes(start_time, duration, calendar)
            schedule.append(
                {
                    "zone_id": zone_id,
                    "start": start_time,
                    "end": end_time,
                    "duration": duration,
                }
            )
            station_available[zone_id] = end_time
            previous_end = end_time

        total_minutes = sum(segment["duration"] for segment in schedule)
        elapsed_minutes = max(0, (now - arrival_time).total_seconds() / 60)
        assigned_index = None
        completed_zone_ids = set()
        status = "complete"
        current_elapsed = 0
        current_duration = 0
        work_elapsed = total_minutes

        if schedule and now < schedule[0]["start"]:
            status = "queued"
            work_elapsed = 0
            current_duration = schedule[0]["duration"]
        else:
            for index, segment in enumerate(schedule):
                if now < segment["start"]:
                    assigned_index = max(0, index - 1)
                    previous_segment = schedule[assigned_index]
                    completed_zone_ids = set(zone_order[:assigned_index])
                    status = "waiting"
                    current_elapsed = previous_segment["duration"]
                    current_duration = previous_segment["duration"]
                    work_elapsed = sum(item["duration"] for item in schedule[: assigned_index + 1])
                    break
                if segment["start"] <= now < segment["end"]:
                    assigned_index = index
                    completed_zone_ids = set(zone_order[:index])
                    status = "in_progress"
                    # A segment can span off-shift gaps, so cap station elapsed
                    # at the segment's working duration.
                    current_elapsed = min(
                        (now - segment["start"]).total_seconds() / 60, segment["duration"]
                    )
                    current_duration = segment["duration"]
                    work_elapsed = sum(item["duration"] for item in schedule[:index]) + current_elapsed
                    break

        if status == "complete":
            assigned_index = len(zone_order) - 1
            completed_zone_ids = set(zone_order)
        elif status == "queued":
            assigned_index = None
        else:
            assigned_index = assigned_index if assigned_index is not None else 0

        final_zone_id = zone_order[-1]
        current_zone_id = "queue"
        current_zone = f"Queued for {zone_names.get(zone_order[0], zone_order[0])}"
        if status == "complete":
            current_zone_id = final_zone_id
            current_zone = zone_names.get(final_zone_id, final_zone_id)
        elif assigned_index is not None:
            current_zone_id = zone_order[assigned_index]
            current_zone = zone_names.get(current_zone_id, current_zone_id)

        percent_complete = 100 if total_minutes == 0 else min(100, round((work_elapsed / total_minutes) * 100))
        actual_time_utilization = 100 if recorded_minutes == 0 else min(100, round((elapsed_minutes / recorded_minutes) * 100, 1))

        balances = []
        for sequence_number, zone_id in enumerate(zone_order, start=1):
            is_current = status not in ("complete", "queued") and zone_id == zone_order[assigned_index]
            is_done = zone_id in completed_zone_ids
            operation_status = "queued"
            if is_current:
                operation_status = status
            elif is_done:
                operation_status = "complete"
            balances.append(
                {
                    "sequence_number": sequence_number,
                    "zone_id": zone_id,
                    "station": zone_names.get(zone_id, zone_id),
                    "capacity": capacities.get(zone_id),
                    "wip_quantity": order["quantity"] if is_current else 0,
                    "completed_quantity": order["quantity"] if is_done else 0,
                    "hold_quantity": 0,
                    "operation_status": operation_status,
                }
            )

        state_order = dict(order)
        state_order["current_zone_id"] = current_zone_id
        state_order["current_zone"] = current_zone
        state_order["production_status"] = status
        state_order["status"] = "complete" if status == "complete" else ("released" if status == "queued" else "in_progress")
        state_order["elapsed_minutes"] = round(elapsed_minutes, 1)
        state_order["recorded_minutes"] = round(recorded_minutes, 1)
        state_order["planned_test_minutes"] = round(total_minutes, 1)
        state_order["percent_complete"] = percent_complete
        state_order["actual_time_utilization_percent"] = actual_time_utilization
        state_order["timing_adjustment_percent"] = round(simulation_factor * 100, 1)
        state_order["station_elapsed_minutes"] = round(current_elapsed, 1)
        state_order["station_test_minutes"] = round(current_duration, 1)
        state_order["station_recorded_minutes"] = round(current_duration / simulation_factor if simulation_factor else 0, 1)

        states.append({"order": state_order, "balances": balances, "schedule": schedule})

    return states


def simulate_active_states(cur, facility_id: int | None = None) -> list[dict]:
    """Simulate active orders per facility so each product line runs its own route."""
    orders = fetch_active_orders(cur)
    if facility_id is not None:
        orders = [order for order in orders if order["facility_id"] == facility_id]

    calendar = load_work_calendar(cur)
    states = []
    for current_facility_id in sorted({order["facility_id"] for order in orders}):
        facility_orders = [order for order in orders if order["facility_id"] == current_facility_id]
        route_steps = fetch_route_steps(cur, current_facility_id)
        if route_steps:
            states.extend(simulated_pipeline_state(facility_orders, route_steps, calendar))
    return states


def sync_workstation_ledger(cur, order: dict, simulation: dict) -> None:
    reference_base = order["order_no"]
    final_zone_id = simulation["balances"][-1]["zone_id"]
    for row in simulation["balances"]:
        zone_id = row["zone_id"]
        station = row["station"]
        if row["completed_quantity"] > 0:
            cur.execute(
                """
                INSERT INTO production_workstation_ledger (
                  production_order_id, zone_id, transaction_type, quantity_in,
                  balance_after, accounting_event, reference, notes
                )
                VALUES (%s, %s, 'in', %s, %s, 'WIP_IN', %s, %s)
                ON CONFLICT (production_order_id, zone_id, transaction_type, reference) DO NOTHING
                """,
                (
                    order["id"],
                    zone_id,
                    order["quantity"],
                    order["quantity"],
                    f"{reference_base}-{zone_id}-in",
                    f"{station} received production WIP.",
                ),
            )
            if zone_id != final_zone_id:
                cur.execute(
                    """
                    INSERT INTO production_workstation_ledger (
                      production_order_id, zone_id, transaction_type, quantity_out,
                      balance_after, accounting_event, reference, notes
                    )
                    VALUES (%s, %s, 'out', %s, 0, 'WIP_OUT', %s, %s)
                    ON CONFLICT (production_order_id, zone_id, transaction_type, reference) DO NOTHING
                    """,
                    (
                        order["id"],
                        zone_id,
                        order["quantity"],
                        f"{reference_base}-{zone_id}-out",
                        f"{station} released production WIP to the next station.",
                    ),
                )
        elif row["wip_quantity"] > 0:
            cur.execute(
                """
                INSERT INTO production_workstation_ledger (
                  production_order_id, zone_id, transaction_type, quantity_in,
                  balance_after, accounting_event, reference, notes
                )
                VALUES (%s, %s, 'in', %s, %s, 'WIP_IN', %s, %s)
                ON CONFLICT (production_order_id, zone_id, transaction_type, reference) DO NOTHING
                """,
                (
                    order["id"],
                    zone_id,
                    order["quantity"],
                    order["quantity"],
                    f"{reference_base}-{zone_id}-in",
                    f"{station} currently holds production WIP.",
                ),
            )


def post_completion_inventory(cur, order: dict, final_zone_id: str) -> None:
    """Book a completed order into stock and consume its inventory-pull components."""
    # Receive the finished quantity into stock at the route's final zone, so
    # completed case orders raise Case Inventory available for drone packaging pull.
    cur.execute(
        """
        UPDATE inventory_items
        SET quantity_on_hand = quantity_on_hand + %s, updated_at = now()
        WHERE location_zone_id = %s AND part_number = %s
        """,
        (order["quantity"], final_zone_id, order["finished_good"]),
    )
    cur.execute(
        """
        INSERT INTO inventory_transactions (
          production_order_id, transaction_type, to_zone_id, part_number,
          quantity, unit, reference
        )
        VALUES (%s, 'complete', %s, %s, %s, 'each', %s)
        """,
        (order["id"], final_zone_id, order["finished_good"], order["quantity"], order["order_no"]),
    )

    # Traceable build: one production record per finished unit.
    firmware = DRONE_FIRMWARE_VERSION if order["finished_good"] == "DRN-FG-600" else None
    for unit in range(1, order["quantity"] + 1):
        cur.execute(
            """
            INSERT INTO production_records (
              production_order_id, serial_no, sku, firmware_version,
              inspection_result, rework_count, final_zone_id
            )
            VALUES (%s, %s, %s, %s, 'pass', 0, %s)
            ON CONFLICT (serial_no) DO NOTHING
            """,
            (
                order["id"],
                f"SN-{order['order_no']}-{unit:03d}",
                order["finished_good"],
                firmware,
                final_zone_id,
            ),
        )

    # Newly stocked finished goods satisfy waiting short pull lines (oldest
    # order first), so a replenishment case order landing in Case Inventory
    # automatically allocates to the drone order that triggered it.
    cur.execute(
        """
        SELECT pom.id, pom.part_number, pom.required_quantity, pom.unit,
               bi.source_zone_id, po.order_no AS waiting_order_no, po.id AS waiting_order_id
        FROM production_order_materials pom
        JOIN bom_items bi ON bi.id = pom.bom_item_id
        JOIN production_orders po ON po.id = pom.production_order_id
        WHERE pom.status = 'short'
          AND pom.part_number = %s
          AND bi.source_zone_id = %s
          AND po.status NOT IN ('complete', 'cancelled')
        ORDER BY po.id
        """,
        (order["finished_good"], final_zone_id),
    )
    for waiting in cur.fetchall():
        cur.execute(
            """
            SELECT quantity_available FROM inventory_items
            WHERE location_zone_id = %s AND part_number = %s
            """,
            (waiting["source_zone_id"], waiting["part_number"]),
        )
        available_row = cur.fetchone()
        if not available_row or available_row["quantity_available"] < waiting["required_quantity"]:
            continue
        cur.execute(
            """
            UPDATE inventory_items
            SET quantity_allocated = quantity_allocated + %s, updated_at = now()
            WHERE location_zone_id = %s AND part_number = %s
            """,
            (waiting["required_quantity"], waiting["source_zone_id"], waiting["part_number"]),
        )
        cur.execute(
            "UPDATE production_order_materials SET status = 'allocated' WHERE id = %s",
            (waiting["id"],),
        )
        cur.execute(
            """
            INSERT INTO inventory_transactions (
              production_order_id, transaction_type, from_zone_id, part_number,
              quantity, unit, reference
            )
            VALUES (%s, 'allocate', %s, %s, %s, %s, %s)
            """,
            (
                waiting["waiting_order_id"],
                waiting["source_zone_id"],
                waiting["part_number"],
                waiting["required_quantity"],
                waiting["unit"],
                waiting["waiting_order_no"],
            ),
        )

    # Consume allocated inventory-pull components (such as transport cases) from stock.
    cur.execute(
        """
        SELECT pom.id, pom.part_number, pom.required_quantity, pom.unit,
               bi.source_zone_id, bi.station_zone_id
        FROM production_order_materials pom
        JOIN bom_items bi ON bi.id = pom.bom_item_id
        WHERE pom.production_order_id = %s
          AND bi.source_zone_id IS NOT NULL
          AND pom.status = 'allocated'
        """,
        (order["id"],),
    )
    for pull in cur.fetchall():
        cur.execute(
            """
            UPDATE inventory_items
            SET quantity_on_hand = quantity_on_hand - %s,
                quantity_allocated = quantity_allocated - %s,
                updated_at = now()
            WHERE location_zone_id = %s AND part_number = %s
            """,
            (
                pull["required_quantity"],
                pull["required_quantity"],
                pull["source_zone_id"],
                pull["part_number"],
            ),
        )
        cur.execute(
            """
            UPDATE production_order_materials
            SET status = 'consumed', issued_quantity = required_quantity,
                consumed_quantity = required_quantity
            WHERE id = %s
            """,
            (pull["id"],),
        )
        cur.execute(
            """
            INSERT INTO inventory_transactions (
              production_order_id, transaction_type, from_zone_id, to_zone_id,
              part_number, quantity, unit, reference
            )
            VALUES (%s, 'consume', %s, %s, %s, %s, %s, %s)
            """,
            (
                order["id"],
                pull["source_zone_id"],
                pull["station_zone_id"],
                pull["part_number"],
                pull["required_quantity"],
                pull["unit"],
                order["order_no"],
            ),
        )


def complete_finished_pipeline_orders(cur, active_states: list[dict]) -> None:
    # Standard costing keeps pace with production: DM issues and station
    # conversion post for in-flight orders on every poll, and completion
    # closes each order's costing (variances + FG transfer) exactly once.
    costing, cards = sync_costing(cur, active_states)
    for state in active_states:
        order = state["order"]
        if order["production_status"] == "complete":
            final_zone_id = state["balances"][-1]["zone_id"]
            cur.execute(
                """
                UPDATE production_orders
                SET status = 'complete', current_zone_id = %s, updated_at = now()
                WHERE id = %s AND status <> 'complete'
                """,
                (final_zone_id, order["id"]),
            )
            if cur.rowcount:
                post_completion_inventory(cur, order, final_zone_id)
                if costing:
                    sku = order["finished_good"]
                    if sku not in cards:
                        cards[sku] = cost_card(cur, costing, sku)
                    post_completion_costing(cur, order, costing, cards[sku])


def first_station_is_busy(active_states: list[dict]) -> bool:
    for state in active_states:
        if state["order"]["production_status"] == "complete":
            continue
        for row in state["balances"]:
            if row["zone_id"] == "receiving" and row["wip_quantity"] > 0:
                return True
    return False


def normalize_queued_order(cur, state: dict) -> None:
    order = state["order"]
    entry_zone_id = state["balances"][0]["zone_id"]
    entry_zone_name = state["balances"][0]["station"]
    cur.execute(
        """
        UPDATE production_orders
        SET status = 'released', current_zone_id = %s, updated_at = now()
        WHERE id = %s AND status <> 'cancelled'
        """,
        (entry_zone_id, order["id"]),
    )
    cur.execute(
        """
        UPDATE workstation_balances
        SET wip_quantity = 0, completed_quantity = 0, hold_quantity = 0, updated_at = now()
        WHERE production_order_id = %s
        """,
        (order["id"],),
    )
    cur.execute(
        """
        UPDATE production_order_operations
        SET quantity_in = 0, quantity_out = 0, status = 'queued'
        WHERE production_order_id = %s
        """,
        (order["id"],),
    )
    cur.execute(
        """
        UPDATE production_order_activity
        SET notes = %s
        WHERE production_order_id = %s AND activity_type = 'created'
        """,
        (
            f"Production order created and queued for {entry_zone_name} until the first workstation is available.",
            order["id"],
        ),
    )
    cur.execute(
        """
        DELETE FROM production_workstation_ledger
        WHERE production_order_id = %s
          AND zone_id = %s
          AND accounting_event = 'WIP_RECEIPT'
        """,
        (order["id"], entry_zone_id),
    )


def sync_order_runtime_status(cur, state: dict) -> None:
    order = state["order"]
    if order["production_status"] == "complete":
        return

    db_status = "released" if order["production_status"] == "queued" else "in_progress"
    zone_id = state["balances"][0]["zone_id"] if order["current_zone_id"] == "queue" else order["current_zone_id"]
    cur.execute(
        """
        UPDATE production_orders
        SET status = %s, current_zone_id = %s, updated_at = now()
        WHERE id = %s AND status <> 'cancelled'
        """,
        (db_status, zone_id, order["id"]),
    )


def fetch_order_snapshot(order_no: str | None = None) -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if order_no:
                cur.execute(
                    """
                    SELECT po.id, po.order_no, m.sku AS finished_good, po.quantity,
                           po.status, po.current_zone_id, z.name AS current_zone,
                           po.start_date, po.due_date, po.created_at, po.priority,
                           fz.facility_id
                    FROM production_orders po
                    JOIN materials m ON m.id = po.finished_material_id
                    JOIN zones fz ON fz.id = m.default_zone_id
                    JOIN zones z ON z.id = po.current_zone_id
                    WHERE po.order_no = %s
                    """,
                    (order_no,),
                )
                order = cur.fetchone()
                if not order:
                    return {"order": None, "balances": [], "materials": [], "activity": [], "ledger": [], "records": []}
                active_orders = fetch_active_orders(cur)
                if not any(active["id"] == order["id"] for active in active_orders):
                    active_orders.append(order)
                calendar = load_work_calendar(cur)
                active_states = []
                for facility_id in sorted({active["facility_id"] for active in active_orders}):
                    facility_orders = [active for active in active_orders if active["facility_id"] == facility_id]
                    route_steps = fetch_route_steps(cur, facility_id)
                    if route_steps:
                        active_states.extend(simulated_pipeline_state(facility_orders, route_steps, calendar))
                if not active_states:
                    return {"order": None, "balances": [], "materials": [], "activity": [], "ledger": [], "records": []}
                selected_state = next(
                    (state for state in active_states if state["order"]["id"] == order["id"]),
                    active_states[-1],
                )
            else:
                active_states = simulate_active_states(cur)
                if not active_states:
                    return {"order": None, "balances": [], "materials": [], "activity": [], "ledger": [], "records": []}
                selected_state = next(
                    (
                        state
                        for state in reversed(active_states)
                        if state["order"]["production_status"] not in ("queued", "complete")
                    ),
                    active_states[-1],
                )

            order = selected_state["order"]
            balances = selected_state["balances"]
            selected_order_id = order["id"]
            for state in active_states:
                sync_order_runtime_status(cur, state)
            if order["production_status"] == "queued":
                normalize_queued_order(cur, selected_state)
            else:
                sync_workstation_ledger(cur, order, {"balances": balances})
            conn.commit()

            cur.execute(
                """
                SELECT part_number, description, required_quantity, issued_quantity,
                       consumed_quantity, unit, status
                FROM production_order_materials
                WHERE production_order_id = %s
                ORDER BY id
                """,
                (selected_order_id,),
            )
            materials = cur.fetchall()

            cur.execute(
                """
                SELECT activity_type, quantity, notes, created_at
                FROM production_order_activity
                WHERE production_order_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 8
                """,
                (selected_order_id,),
            )
            activity = cur.fetchall()

            cur.execute(
                """
                SELECT pwl.transaction_at, z.name AS station, pwl.transaction_type,
                       pwl.quantity_in, pwl.quantity_out, pwl.adjustment_quantity,
                       pwl.balance_after, pwl.accounting_event, pwl.reference, pwl.notes
                FROM production_workstation_ledger pwl
                JOIN zones z ON z.id = pwl.zone_id
                WHERE pwl.production_order_id = %s
                ORDER BY pwl.transaction_at DESC, pwl.id DESC
                LIMIT 20
                """,
                (selected_order_id,),
            )
            ledger = cur.fetchall()

            cur.execute(
                """
                SELECT pr.serial_no, pr.sku, pr.firmware_version, pr.inspection_result,
                       pr.rework_count, z.name AS final_location, pr.created_at
                FROM production_records pr
                LEFT JOIN zones z ON z.id = pr.final_zone_id
                WHERE pr.production_order_id = %s
                ORDER BY pr.serial_no
                """,
                (selected_order_id,),
            )
            records = cur.fetchall()

    return {
        "order": order,
        "balances": balances,
        "materials": materials,
        "activity": activity,
        "ledger": ledger,
        "records": records,
    }


def fetch_next_order_no(finished_sku: str = DEFAULT_FINISHED_SKU) -> str:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT next_production_order_no(%s) AS order_no",
                (order_prefix_for(finished_sku),),
            )
            return cur.fetchone()["order_no"]


def fetch_floor_dashboard(facility_id: int = 1) -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, zone_type, capacity FROM zones WHERE facility_id = %s",
                (facility_id,),
            )
            capacities = {
                row["id"]: {"capacity": row["capacity"], "zone_type": row["zone_type"]}
                for row in cur.fetchall()
            }

            # Simulate every facility so case-line orders keep completing and
            # posting inventory even when only the drone floor map is open.
            all_states = simulate_active_states(cur)
            if not all_states:
                return {
                    "summary": {"active_orders": 0, "active_quantity": 0},
                    "zones": [],
                    "capacities": capacities,
                }

            for state in all_states:
                sync_order_runtime_status(cur, state)
                if state["order"]["production_status"] == "queued":
                    normalize_queued_order(cur, state)
                else:
                    sync_workstation_ledger(cur, state["order"], {"balances": state["balances"]})
            complete_finished_pipeline_orders(cur, all_states)
            conn.commit()
            active_states = [
                state
                for state in all_states
                if state["order"]["production_status"] != "complete"
                and state["order"]["facility_id"] == facility_id
            ]
            if not active_states:
                return {
                    "summary": {"active_orders": 0, "active_quantity": 0},
                    "zones": [],
                    "capacities": capacities,
                }

            zone_totals = {}
            for state in active_states:
                if state["order"]["production_status"] == "queued":
                    first_row = state["balances"][0]
                    zone = zone_totals.setdefault(
                        first_row["zone_id"],
                        {
                            "zone_id": first_row["zone_id"],
                            "zone_name": first_row["station"],
                            "wip": 0,
                            "completed": 0,
                            "hold": 0,
                            "queued": 0,
                            "orders": [],
                            "queued_orders": [],
                        },
                    )
                    zone["queued"] += state["order"]["quantity"]
                    zone["queued_orders"].append(state["order"]["order_no"])
                for row in state["balances"]:
                    zone = zone_totals.setdefault(
                        row["zone_id"],
                        {
                            "zone_id": row["zone_id"],
                            "zone_name": row["station"],
                            "wip": 0,
                            "completed": 0,
                            "hold": 0,
                            "queued": 0,
                            "orders": [],
                            "queued_orders": [],
                        },
                    )
                    zone["wip"] += row["wip_quantity"]
                    zone["completed"] += row["completed_quantity"]
                    zone["hold"] += row["hold_quantity"]
                    if row["wip_quantity"] > 0:
                        zone["orders"].append(state["order"]["order_no"])

            zone_order = [row["zone_id"] for row in active_states[0]["balances"]]
            zones = [zone_totals[zone_id] for zone_id in zone_order if zone_id in zone_totals]
            display_state = next(
                (
                    state
                    for state in reversed(active_states)
                    if state["order"]["production_status"] not in ("queued", "complete")
                ),
                active_states[-1],
            )
            summary = {
                "active_orders": len(active_states),
                "active_quantity": sum(state["order"]["quantity"] for state in active_states),
                "display_order_no": display_state["order"]["order_no"],
                "current_zone_id": display_state["order"]["current_zone_id"],
                "current_zone": display_state["order"]["current_zone"],
                "production_status": display_state["order"]["production_status"],
                "percent_complete": display_state["order"]["percent_complete"],
                "actual_time_utilization_percent": display_state["order"]["actual_time_utilization_percent"],
                "timing_adjustment_percent": display_state["order"]["timing_adjustment_percent"],
                "elapsed_minutes": display_state["order"]["elapsed_minutes"],
                "recorded_minutes": display_state["order"]["recorded_minutes"],
            }

    return {"summary": summary, "zones": zones, "capacities": capacities}


def fetch_operations_overview() -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            all_states = simulate_active_states(cur)
            for state in all_states:
                sync_order_runtime_status(cur, state)
                if state["order"]["production_status"] == "queued":
                    normalize_queued_order(cur, state)
                else:
                    sync_workstation_ledger(cur, state["order"], {"balances": state["balances"]})
            complete_finished_pipeline_orders(cur, all_states)
            conn.commit()

            active_states = [state for state in all_states if state["order"]["production_status"] != "complete"]

            cur.execute("SELECT id, name FROM facilities ORDER BY id")
            facilities = cur.fetchall()

            # Line output over the last 24 hours, for utilization against the
            # bottleneck ceiling.
            cur.execute(
                """
                SELECT fz.facility_id, COALESCE(SUM(po.quantity), 0) AS quantity
                FROM production_orders po
                JOIN materials m ON m.id = po.finished_material_id
                JOIN zones fz ON fz.id = m.default_zone_id
                WHERE po.status = 'complete'
                  AND po.updated_at >= now() - interval '24 hours'
                GROUP BY fz.facility_id
                """
            )
            output_24h_by_facility = {row["facility_id"]: float(row["quantity"]) for row in cur.fetchall()}

            now = datetime.now(timezone.utc)
            window_start = now - timedelta(hours=1)
            pipelines = []
            for facility in facilities:
                facility_states = [
                    state for state in active_states
                    if state["order"]["facility_id"] == facility["id"]
                ]
                stations = []
                ceiling_per_hour = None
                bottleneck_station = None
                route_steps = fetch_route_steps(cur, facility["id"])
                if route_steps:
                    zone_order, zone_names, _, standard_durations, zone_capacities = build_zone_model(route_steps)
                    totals = {}
                    busy_seconds = {zone_id: 0.0 for zone_id in zone_order}
                    for state in facility_states:
                        for row in state["balances"]:
                            zone = totals.setdefault(row["zone_id"], {"wip": 0, "done": 0, "orders": []})
                            zone["wip"] += row["wip_quantity"]
                            zone["done"] += row["completed_quantity"]
                            if row["wip_quantity"] > 0:
                                zone["orders"].append(state["order"]["order_no"])
                        for segment in state["schedule"]:
                            overlap_start = max(segment["start"], window_start)
                            overlap_end = min(segment["end"], now)
                            if overlap_end > overlap_start:
                                busy_seconds[segment["zone_id"]] += (overlap_end - overlap_start).total_seconds()
                    for zone_id in zone_order:
                        capacity = zone_capacities.get(zone_id)
                        cycle_minutes = standard_durations.get(zone_id)
                        max_per_hour = (
                            round(capacity * 60 / cycle_minutes, 1)
                            if capacity and cycle_minutes
                            else None
                        )
                        stations.append(
                            {
                                "zone_id": zone_id,
                                "station": zone_names.get(zone_id, zone_id),
                                "capacity": capacity,
                                "max_per_hour": max_per_hour,
                                "bottleneck": False,
                                "busy_pct_last_hour": round(busy_seconds.get(zone_id, 0.0) / 36, 1),
                                **totals.get(zone_id, {"wip": 0, "done": 0, "orders": []}),
                            }
                        )
                    constrained = [s for s in stations if s["max_per_hour"] is not None]
                    if constrained:
                        ceiling_per_hour = min(s["max_per_hour"] for s in constrained)
                        for station in stations:
                            if station["max_per_hour"] == ceiling_per_hour:
                                station["bottleneck"] = True
                                if bottleneck_station is None:
                                    bottleneck_station = station["station"]
                output_24h = output_24h_by_facility.get(facility["id"], 0.0)
                pipelines.append(
                    {
                        "facility_id": facility["id"],
                        "facility_name": facility["name"],
                        "active_orders": len(facility_states),
                        "stations": stations,
                        "ceiling_per_hour": ceiling_per_hour,
                        "bottleneck_station": bottleneck_station,
                        "output_24h": output_24h,
                        "pct_of_ceiling_24h": (
                            round(output_24h / (ceiling_per_hour * 24) * 100, 1)
                            if ceiling_per_hour
                            else None
                        ),
                    }
                )

            orders = [
                {
                    "order_no": state["order"]["order_no"],
                    "finished_good": state["order"]["finished_good"],
                    "facility_id": state["order"]["facility_id"],
                    "quantity": state["order"]["quantity"],
                    "production_status": state["order"]["production_status"],
                    "current_zone": state["order"]["current_zone"],
                    "percent_complete": state["order"]["percent_complete"],
                    "due_date": state["order"]["due_date"],
                }
                for state in active_states
            ]

            cur.execute(
                """
                SELECT i.area, z.name AS location, i.item_name, i.part_number,
                       i.quantity_on_hand, i.quantity_allocated, i.quantity_available,
                       i.min_quantity, i.max_quantity, i.status
                FROM inventory_items i
                JOIN zones z ON z.id = i.location_zone_id
                WHERE i.area IN ('Sub-assembly', 'Finished Goods')
                   OR (i.area = 'Parts' AND i.quantity_available <= i.min_quantity)
                ORDER BY
                  CASE i.area WHEN 'Sub-assembly' THEN 1 WHEN 'Finished Goods' THEN 2 ELSE 3 END,
                  i.id
                """
            )
            inventory_watch = cur.fetchall()

            cur.execute(
                """
                SELECT po.order_no, pom.part_number, pom.description,
                       pom.required_quantity, pom.unit
                FROM production_order_materials pom
                JOIN production_orders po ON po.id = pom.production_order_id
                WHERE pom.status = 'short' AND po.status NOT IN ('complete', 'cancelled')
                ORDER BY po.id
                """
            )
            shortages = cur.fetchall()

            # Rolling 24h window rather than the UTC calendar day, so evening
            # completions don't vanish from the card when UTC midnight passes.
            cur.execute(
                """
                SELECT m.sku, count(*) AS orders, COALESCE(sum(po.quantity), 0) AS quantity
                FROM production_orders po
                JOIN materials m ON m.id = po.finished_material_id
                WHERE po.status = 'complete'
                  AND po.updated_at >= now() - interval '24 hours'
                GROUP BY m.sku
                ORDER BY m.sku
                """
            )
            completed_today = cur.fetchall()

            cur.execute(
                """
                SELECT t.created_at, t.transaction_type, t.part_number, t.quantity, t.reference,
                       fz.name AS from_zone, tz.name AS to_zone
                FROM inventory_transactions t
                LEFT JOIN zones fz ON fz.id = t.from_zone_id
                LEFT JOIN zones tz ON tz.id = t.to_zone_id
                ORDER BY t.id DESC
                LIMIT 10
                """
            )
            recent_transactions = cur.fetchall()

            summary = {
                "active_orders": len(active_states),
                "active_quantity": sum(state["order"]["quantity"] for state in active_states),
                "lines_running": len({state["order"]["facility_id"] for state in active_states}),
                "shortage_count": len(shortages),
            }

            work_hours = read_plant_settings(cur)

    return {
        "summary": summary,
        "pipelines": pipelines,
        "orders": orders,
        "inventory_watch": inventory_watch,
        "shortages": shortages,
        "completed_today": completed_today,
        "recent_transactions": recent_transactions,
        "work_hours": work_hours,
    }


def schedule_segments(state: dict, zone_names: dict) -> list[dict]:
    return [
        {
            "zone_id": segment["zone_id"],
            "station": zone_names.get(segment["zone_id"], segment["zone_id"]),
            "start": segment["start"].isoformat(),
            "end": segment["end"].isoformat(),
        }
        for segment in state["schedule"]
    ]


def fetch_schedule() -> dict:
    """Station-by-station occupancy for every active order on both lines."""
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            all_states = simulate_active_states(cur)
            for state in all_states:
                sync_order_runtime_status(cur, state)
                if state["order"]["production_status"] == "queued":
                    normalize_queued_order(cur, state)
                else:
                    sync_workstation_ledger(cur, state["order"], {"balances": state["balances"]})
            complete_finished_pipeline_orders(cur, all_states)
            conn.commit()

            active_states = [
                state for state in all_states if state["order"]["production_status"] != "complete"
            ]

            cur.execute("SELECT id, name FROM facilities ORDER BY id")
            facilities = cur.fetchall()

            lines = []
            for facility in facilities:
                route_steps = fetch_route_steps(cur, facility["id"])
                if not route_steps:
                    continue
                zone_order, zone_names, _, standard_durations, capacities = build_zone_model(route_steps)

                # Max output per station: capacity units per recorded cycle.
                # The slowest constrained station is the line's bottleneck.
                stations = []
                for zone_id in zone_order[:-1]:
                    capacity = capacities.get(zone_id)
                    cycle_minutes = standard_durations.get(zone_id)
                    max_per_hour = (
                        round(capacity * 60 / cycle_minutes, 1)
                        if capacity and cycle_minutes
                        else None
                    )
                    stations.append(
                        {
                            "zone_id": zone_id,
                            "station": zone_names.get(zone_id, zone_id),
                            "capacity": capacity,
                            "cycle_minutes": cycle_minutes,
                            "max_per_hour": max_per_hour,
                            "bottleneck": False,
                        }
                    )
                constrained = [s for s in stations if s["max_per_hour"] is not None]
                if constrained:
                    slowest = min(s["max_per_hour"] for s in constrained)
                    for station in stations:
                        if station["max_per_hour"] == slowest:
                            station["bottleneck"] = True

                orders = []
                for state in active_states:
                    if state["order"]["facility_id"] != facility["id"]:
                        continue
                    segments = schedule_segments(state, zone_names)
                    orders.append(
                        {
                            "order_no": state["order"]["order_no"],
                            "finished_good": state["order"]["finished_good"],
                            "quantity": state["order"]["quantity"],
                            "production_status": state["order"]["production_status"],
                            "percent_complete": state["order"]["percent_complete"],
                            "priority": state["order"].get("priority"),
                            "due_date": str(state["order"]["due_date"]),
                            "start": segments[0]["start"] if segments else None,
                            "finish": segments[-1]["end"] if segments else None,
                            "segments": segments,
                        }
                    )

                lines.append(
                    {
                        "facility_id": facility["id"],
                        "facility_name": facility["name"],
                        "stations": stations,
                        "orders": orders,
                    }
                )

            work_hours = read_plant_settings(cur)

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "lines": lines,
        "work_hours": work_hours,
    }


def resolve_finished_good(cur, finished_sku: str) -> dict:
    cur.execute(
        """
        SELECT m.id AS material_id, z.facility_id
        FROM materials m
        JOIN zones z ON z.id = m.default_zone_id
        WHERE m.sku = %s AND m.material_type = 'finished'
        """,
        (finished_sku,),
    )
    finished = cur.fetchone()
    if not finished:
        raise ValueError(f"Unknown finished good {finished_sku}.")
    return finished


def project_phantom(facility_orders, route_steps, finished_sku, quantity, due_date, now, calendar=None):
    """Simulate a prospective order appended to the facility's live queue."""
    zone_order, zone_names, _, _, _ = build_zone_model(route_steps)
    phantom = {
        "id": -1,
        "order_no": "PREVIEW",
        "finished_good": finished_sku,
        "quantity": quantity,
        "status": "planned",
        "current_zone_id": zone_order[0],
        "current_zone": zone_names.get(zone_order[0], zone_order[0]),
        "start_date": now.date(),
        "due_date": due_date or str(now.date()),
        "created_at": now,
        "facility_id": None,
    }
    states = simulated_pipeline_state(facility_orders + [phantom], route_steps, calendar)
    phantom_state = next(state for state in states if state["order"]["id"] == -1)
    return phantom_state, zone_names


def pull_impact(cur, material_id: int, quantity: int) -> list[dict]:
    cur.execute(
        """
        SELECT bi.part_number, bi.description, bi.quantity, bi.source_zone_id,
               i.quantity_available
        FROM bom_items bi
        LEFT JOIN inventory_items i
          ON i.location_zone_id = bi.source_zone_id
         AND i.part_number = bi.part_number
        WHERE bi.parent_material_id = %s AND bi.source_zone_id IS NOT NULL
        """,
        (material_id,),
    )
    pulls = []
    for row in cur.fetchall():
        required = float(row["quantity"]) * quantity
        available = float(row["quantity_available"] or 0)
        pulls.append(
            {
                "part_number": row["part_number"],
                "description": row["description"],
                "required": required,
                "available": available,
                "short": required > available,
                "shortfall": max(0, math.ceil(required - available)),
            }
        )
    return pulls


def preview_schedule(payload: dict) -> dict:
    """What-if: project a prospective order through the current queue without creating it."""
    finished_sku = str(payload.get("finishedSku", DEFAULT_FINISHED_SKU)).strip() or DEFAULT_FINISHED_SKU
    quantity = int(payload.get("quantity", 0))
    due_date = str(payload.get("dueDate", "")).strip()
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            finished = resolve_finished_good(cur, finished_sku)
            facility_id = finished["facility_id"]

            route_steps = fetch_route_steps(cur, facility_id)
            if not route_steps:
                raise ValueError("No route defined for that product.")

            now = datetime.now(timezone.utc)
            calendar = load_work_calendar(cur)
            facility_orders = [
                order for order in fetch_active_orders(cur) if order["facility_id"] == facility_id
            ]
            phantom_state, zone_names = project_phantom(
                facility_orders, route_steps, finished_sku, quantity, due_date, now, calendar
            )
            segments = schedule_segments(phantom_state, zone_names)

            pulls = pull_impact(cur, finished["material_id"], quantity)

    return {
        "finished_good": finished_sku,
        "facility_id": facility_id,
        "quantity": quantity,
        "due_date": phantom_state["order"]["due_date"],
        "now": now.isoformat(),
        "start": segments[0]["start"] if segments else None,
        "finish": segments[-1]["end"] if segments else None,
        "planned_test_minutes": phantom_state["order"]["planned_test_minutes"],
        "recorded_minutes": phantom_state["order"]["recorded_minutes"],
        "segments": segments,
        "pulls": pulls,
    }


def max_output_schedule(payload: dict) -> dict:
    """Largest quantity of a product the line can finish within a wall-clock window."""
    finished_sku = str(payload.get("finishedSku", DEFAULT_FINISHED_SKU)).strip() or DEFAULT_FINISHED_SKU
    window_minutes = int(payload.get("windowMinutes", 15))
    if window_minutes < 1 or window_minutes > 1440:
        raise ValueError("Window must be between 1 and 1440 minutes.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            finished = resolve_finished_good(cur, finished_sku)
            facility_id = finished["facility_id"]
            route_steps = fetch_route_steps(cur, facility_id)
            if not route_steps:
                raise ValueError("No route defined for that product.")

            now = datetime.now(timezone.utc)
            deadline = now + timedelta(minutes=window_minutes)
            calendar = load_work_calendar(cur)
            facility_orders = [
                order for order in fetch_active_orders(cur) if order["facility_id"] == facility_id
            ]

            def finish_for(qty: int) -> datetime:
                state, _ = project_phantom(
                    facility_orders, route_steps, finished_sku, qty, "", now, calendar
                )
                return state["schedule"][-1]["end"]

            CAP = 10000
            if finish_for(1) > deadline:
                max_quantity = 0
            else:
                high = 1
                while high < CAP and finish_for(high * 2) <= deadline:
                    high *= 2
                if high >= CAP:
                    max_quantity = CAP
                else:
                    low, infeasible = high, high * 2
                    while infeasible - low > 1:
                        mid = (low + infeasible) // 2
                        if finish_for(mid) <= deadline:
                            low = mid
                        else:
                            infeasible = mid
                    max_quantity = low

            if max_quantity == 0:
                return {
                    "finished_good": finished_sku,
                    "facility_id": facility_id,
                    "window_minutes": window_minutes,
                    "deadline": deadline.isoformat(),
                    "max_quantity": 0,
                    "note": "Not even one unit can finish inside that window with the current queue.",
                }

            phantom_state, zone_names = project_phantom(
                facility_orders, route_steps, finished_sku, max_quantity, "", now, calendar
            )
            segments = schedule_segments(phantom_state, zone_names)
            pulls = pull_impact(cur, finished["material_id"], max_quantity)

    return {
        "finished_good": finished_sku,
        "facility_id": facility_id,
        "window_minutes": window_minutes,
        "deadline": deadline.isoformat(),
        "max_quantity": max_quantity,
        "start": segments[0]["start"] if segments else None,
        "finish": segments[-1]["end"] if segments else None,
        "planned_test_minutes": phantom_state["order"]["planned_test_minutes"],
        "pulls": pulls,
    }


# Maps zone ids to the station names used in production-plan.csv so the
# drill-down can show work scripts, quality gates, and tooling per station.
PLAN_STATION_BY_ZONE = {
    "receiving": "Receiving",
    "raw": "Kitting",
    "ws1": "Airframe",
    "ws2": "Electronics",
    "ws3": "Firmware",
    "ws4": "Motor Test",
    "ws5": "QA Test",
    "fg": "Packaged",
    "inventory": "FG Inventory",
    "case_receiving": "Case Receiving",
    "case_raw": "Case Staging",
    "cws1": "Shell Forming",
    "cws2": "Foam Fit",
    "cws3": "Hardware",
    "cws4": "Inspection",
    "case_inventory": "Case Inventory",
}


def load_plan_row(zone_id: str) -> dict | None:
    station_name = PLAN_STATION_BY_ZONE.get(zone_id)
    if not station_name:
        return None
    plan_path = BASE_DIR / "production-plan.csv"
    if not plan_path.exists():
        return None
    with open(plan_path, newline="", encoding="utf-8") as plan_file:
        for row in csv.DictReader(plan_file):
            if row.get("station") == station_name:
                return row
    return None


def fetch_station(zone_id: str) -> dict:
    """Everything about one workstation: schedule, utilization, parts, work, ledger."""
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT z.id, z.name, z.zone_type, z.description, z.area_sq_ft,
                       z.primary_flow, z.status, z.capacity, z.facility_id,
                       f.name AS facility_name
                FROM zones z
                JOIN facilities f ON f.id = z.facility_id
                WHERE z.id = %s
                """,
                (zone_id,),
            )
            zone = cur.fetchone()
            if not zone:
                raise ValueError("Unknown zone.")

            route_steps = fetch_route_steps(cur, zone["facility_id"])
            cycle_minutes = None
            max_per_hour = None
            bottleneck = False
            if route_steps:
                zone_order, zone_names, _, standard_durations, capacities = build_zone_model(route_steps)
                cycle_minutes = standard_durations.get(zone_id)
                capacity = capacities.get(zone_id)
                if capacity and cycle_minutes:
                    max_per_hour = round(capacity * 60 / cycle_minutes, 1)
                rates = [
                    capacities[z] * 60 / standard_durations[z]
                    for z in zone_order[:-1]
                    if capacities.get(z) and standard_durations.get(z)
                ]
                if max_per_hour is not None and rates and max_per_hour == round(min(rates), 1):
                    bottleneck = True

            now = datetime.now(timezone.utc)
            schedule = []
            busy_seconds_last_hour = 0.0
            window_start = now - timedelta(hours=1)
            for state in simulate_active_states(cur, zone["facility_id"]):
                if state["order"]["production_status"] == "complete":
                    continue
                segment = next(
                    (item for item in state["schedule"] if item["zone_id"] == zone_id), None
                )
                if not segment:
                    continue
                overlap_start = max(segment["start"], window_start)
                overlap_end = min(segment["end"], now)
                if overlap_end > overlap_start:
                    busy_seconds_last_hour += (overlap_end - overlap_start).total_seconds()
                schedule.append(
                    {
                        "order_no": state["order"]["order_no"],
                        "finished_good": state["order"]["finished_good"],
                        "quantity": state["order"]["quantity"],
                        "start": segment["start"].isoformat(),
                        "end": segment["end"].isoformat(),
                        "running": segment["start"] <= now < segment["end"],
                        "done_here": segment["end"] <= now,
                    }
                )
            schedule.sort(key=lambda item: item["start"])
            idle_at = max((item["end"] for item in schedule), default=None)

            cur.execute(
                """
                SELECT COALESCE(SUM(quantity_in) FILTER (WHERE transaction_type = 'in'), 0) AS units_in,
                       COALESCE(SUM(quantity_out) FILTER (WHERE transaction_type = 'out'), 0) AS units_out
                FROM production_workstation_ledger
                WHERE zone_id = %s AND transaction_at >= now() - interval '24 hours'
                """,
                (zone_id,),
            )
            ledger_sums = cur.fetchone()
            units_out_24h = float(ledger_sums["units_out"])
            pct_of_ceiling = (
                round(units_out_24h / 24 / max_per_hour * 100, 1) if max_per_hour else None
            )

            cur.execute(
                """
                SELECT area, item_name, part_number, quantity_on_hand, quantity_allocated,
                       quantity_available, min_quantity, max_quantity, status, control_note
                FROM inventory_items
                WHERE location_zone_id = %s
                ORDER BY area, id
                """,
                (zone_id,),
            )
            parts = cur.fetchall()

            cur.execute(
                """
                SELECT m.sku AS product, bi.part_number, bi.description, bi.category,
                       bi.quantity, bi.unit, bi.supply_type
                FROM bom_items bi
                JOIN materials m ON m.id = bi.parent_material_id
                WHERE bi.station_zone_id = %s
                ORDER BY m.sku, bi.id
                """,
                (zone_id,),
            )
            bom_work = cur.fetchall()

            cur.execute(
                """
                SELECT transaction_at, transaction_type, quantity_in, quantity_out,
                       accounting_event, reference, notes
                FROM production_workstation_ledger
                WHERE zone_id = %s
                ORDER BY transaction_at DESC, id DESC
                LIMIT 12
                """,
                (zone_id,),
            )
            ledger = cur.fetchall()

            # Standard script: structured operator prompts from the plan row,
            # plus recent signoffs recorded at this station.
            plan = load_plan_row(zone_id)
            script = None
            if plan:
                script = {
                    "setup": [item.strip() for item in plan["tools_support"].split(";") if item.strip()],
                    "steps": [item.strip() for item in plan["work_script"].split(";") if item.strip()],
                    "hold_point": plan["material_pull"],
                    "pass_fail": plan["quality_gate"],
                    "signoff_role": plan["primary_role"],
                }
            cur.execute(
                """
                SELECT operator, result, notes, order_no, created_at
                FROM station_signoffs
                WHERE zone_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 8
                """,
                (zone_id,),
            )
            signoffs = cur.fetchall()

    return {
        "now": now.isoformat(),
        "zone": {
            **zone,
            "cycle_minutes": cycle_minutes,
            "max_per_hour": max_per_hour,
            "bottleneck": bottleneck,
        },
        "schedule": schedule,
        # schedule entries carry ISO strings, so idle_at already is one.
        "idle_at": idle_at,
        "utilization": {
            "busy_pct_last_hour": round(busy_seconds_last_hour / 36, 1),
            "units_in_24h": float(ledger_sums["units_in"]),
            "units_out_24h": units_out_24h,
            "pct_of_ceiling_24h": pct_of_ceiling,
        },
        "parts": parts,
        "bom_work": bom_work,
        "plan": plan,
        "script": script,
        "signoffs": signoffs,
        "ledger": ledger,
    }


def fetch_order_history() -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            active_states = simulate_active_states(cur)
            for state in active_states:
                sync_order_runtime_status(cur, state)
                if state["order"]["production_status"] == "queued":
                    normalize_queued_order(cur, state)
                else:
                    sync_workstation_ledger(cur, state["order"], {"balances": state["balances"]})
            complete_finished_pipeline_orders(cur, active_states)
            conn.commit()

            active_by_id = {state["order"]["id"]: state["order"] for state in active_states}
            cur.execute(
                """
                SELECT po.id, po.order_no, m.sku AS finished_good, po.quantity,
                       po.status, po.current_zone_id, z.name AS current_zone,
                       po.start_date, po.due_date, po.created_at, po.updated_at
                FROM production_orders po
                JOIN materials m ON m.id = po.finished_material_id
                JOIN zones z ON z.id = po.current_zone_id
                ORDER BY po.created_at DESC, po.id DESC
                LIMIT 40
                """
            )
            rows = []
            for row in cur.fetchall():
                live = active_by_id.get(row["id"])
                if live:
                    rows.append(
                        {
                            "order_no": live["order_no"],
                            "finished_good": live["finished_good"],
                            "quantity": live["quantity"],
                            "status": live["status"],
                            "production_status": live["production_status"],
                            "current_zone": live["current_zone"],
                            "percent_complete": live["percent_complete"],
                            "elapsed_minutes": live["elapsed_minutes"],
                            "planned_test_minutes": live["planned_test_minutes"],
                            "created_at": live["created_at"],
                            "due_date": live["due_date"],
                        }
                    )
                else:
                    rows.append(
                        {
                            "order_no": row["order_no"],
                            "finished_good": row["finished_good"],
                            "quantity": row["quantity"],
                            "status": row["status"],
                            "production_status": row["status"],
                            "current_zone": row["current_zone"],
                            "percent_complete": 100 if row["status"] == "complete" else 0,
                            "elapsed_minutes": None,
                            "planned_test_minutes": None,
                            "created_at": row["created_at"],
                            "due_date": row["due_date"],
                        }
                    )

    return {"orders": rows}


def set_zone_capacity(payload: dict) -> dict:
    zone_id = str(payload.get("zoneId", "")).strip()
    capacity = payload.get("capacity")
    if capacity is not None:
        capacity = int(capacity)
        if capacity < 1:
            raise ValueError("Capacity must be at least 1, or empty for unconstrained.")
        if capacity > 10000:
            raise ValueError("Capacity is unrealistically large.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name, zone_type FROM zones WHERE id = %s", (zone_id,))
            zone = cur.fetchone()
            if not zone:
                raise ValueError("Unknown zone.")
            if zone["zone_type"] != "workstation":
                raise ValueError("Capacity limits apply to workstations only.")
            cur.execute(
                "UPDATE zones SET capacity = %s WHERE id = %s",
                (capacity, zone_id),
            )
        conn.commit()

    return {"zoneId": zone_id, "name": zone["name"], "capacity": capacity}


def set_order_priority(payload: dict) -> dict:
    """Move an active order one slot up or down its line's queue. The whole
    line is renumbered 10, 20, 30... so the sequence stays unambiguous; the
    deterministic schedule re-times in-flight orders on the next poll."""
    order_no = str(payload.get("orderNo", "")).strip()
    direction = str(payload.get("direction", "")).strip()
    if direction not in ("up", "down"):
        raise ValueError("Direction must be 'up' or 'down'.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            orders = fetch_active_orders(cur)
            target = next((order for order in orders if order["order_no"] == order_no), None)
            if not target:
                raise ValueError("That order is not active.")
            line = [order for order in orders if order["facility_id"] == target["facility_id"]]
            index = line.index(target)
            swap_index = index - 1 if direction == "up" else index + 1
            moved = 0 <= swap_index < len(line)
            if moved:
                line[index], line[swap_index] = line[swap_index], line[index]
                for position, order in enumerate(line, start=1):
                    cur.execute(
                        "UPDATE production_orders SET priority = %s WHERE id = %s",
                        (position * 10, order["id"]),
                    )
        conn.commit()

    return {"orderNo": order_no, "moved": moved, "sequence": [order["order_no"] for order in line]}


def set_zone_cycle(payload: dict) -> dict:
    """Edit a station's recorded cycle minutes (process_steps.expected_minutes
    keyed by source zone). Reweights the simulated schedule, the station's max
    output, and the line bottleneck on the next poll."""
    zone_id = str(payload.get("zoneId", "")).strip()
    minutes = payload.get("minutes")
    if minutes is None:
        raise ValueError("Cycle minutes are required.")
    minutes = int(minutes)
    if minutes < 1:
        raise ValueError("Cycle minutes must be at least 1.")
    if minutes > 10000:
        raise ValueError("Cycle minutes are unrealistically large.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM zones WHERE id = %s", (zone_id,))
            zone = cur.fetchone()
            if not zone:
                raise ValueError("Unknown zone.")
            cur.execute(
                "UPDATE process_steps SET expected_minutes = %s WHERE source_zone_id = %s",
                (minutes, zone_id),
            )
            if cur.rowcount == 0:
                raise ValueError("That zone has no routed cycle time to edit.")
        conn.commit()

    return {"zoneId": zone_id, "name": zone["name"], "minutes": minutes}


def fetch_labor_standards() -> dict:
    """Labor standard per station: direct minutes from the routing plan plus
    indirect adders (material handling, QA review, rework disposition)."""
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT category, description, pct FROM labor_overheads ORDER BY category")
            overheads = cur.fetchall()

    pct_total = sum(float(overhead["pct"]) for overhead in overheads)
    lines = {"drone": [], "case": []}
    plan_path = BASE_DIR / "production-plan.csv"
    if plan_path.exists():
        with open(plan_path, newline="", encoding="utf-8") as plan_file:
            for row in csv.DictReader(plan_file):
                direct = float(row["labor_minutes"])
                entry = {
                    "seq": int(row["seq"]),
                    "station": row["station"],
                    "operation_type": row["operation_type"],
                    "role": row["primary_role"],
                    "direct_minutes": direct,
                    "indirect": {
                        overhead["category"]: round(direct * float(overhead["pct"]) / 100, 1)
                        for overhead in overheads
                    },
                    "indirect_minutes": round(direct * pct_total / 100, 1),
                    "standard_minutes": round(direct * (1 + pct_total / 100), 1),
                }
                lines["drone" if entry["seq"] < 100 else "case"].append(entry)

    totals = {
        key: {
            "direct": round(sum(row["direct_minutes"] for row in rows), 1),
            "indirect": round(sum(row["indirect_minutes"] for row in rows), 1),
            "standard": round(sum(row["standard_minutes"] for row in rows), 1),
        }
        for key, rows in lines.items()
    }
    return {"overheads": overheads, "overhead_pct_total": pct_total, "lines": lines, "totals": totals}


def kit_check(finished_sku: str, quantity: int) -> dict:
    """Material release: every BOM line's availability before a build starts,
    with serialized parts and approved substitutes called out."""
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            finished = resolve_finished_good(cur, finished_sku)
            cur.execute(
                """
                SELECT bi.part_number, bi.description, bi.category, bi.quantity, bi.unit,
                       bi.serialized, bi.substitute_part_number,
                       z.name AS check_zone, i.quantity_available
                FROM bom_items bi
                LEFT JOIN zones z ON z.id = COALESCE(bi.source_zone_id, bi.station_zone_id)
                LEFT JOIN inventory_items i
                  ON i.location_zone_id = COALESCE(bi.source_zone_id, bi.station_zone_id)
                 AND i.part_number = bi.part_number
                WHERE bi.parent_material_id = %s AND bi.part_number <> %s
                ORDER BY bi.id
                """,
                (finished["material_id"], finished_sku),
            )
            kit = []
            for row in cur.fetchall():
                required = float(row["quantity"]) * quantity
                available = (
                    float(row["quantity_available"]) if row["quantity_available"] is not None else None
                )
                if available is None:
                    status = "not stocked"
                elif available >= required:
                    status = "available"
                elif row["substitute_part_number"]:
                    status = "substitute"
                else:
                    status = "short"
                kit.append(
                    {
                        "part_number": row["part_number"],
                        "description": row["description"],
                        "check_zone": row["check_zone"],
                        "required": required,
                        "unit": row["unit"],
                        "available": available,
                        "serialized": row["serialized"],
                        "substitute": row["substitute_part_number"],
                        "status": status,
                    }
                )

    shorts = sum(1 for line in kit if line["status"] == "short")
    return {
        "finished_good": finished_sku,
        "quantity": quantity,
        "kit": kit,
        "summary": {
            "lines": len(kit),
            "available": sum(1 for line in kit if line["status"] == "available"),
            "short": shorts,
            "substitute": sum(1 for line in kit if line["status"] == "substitute"),
            "not_stocked": sum(1 for line in kit if line["status"] == "not stocked"),
            "serialized": sum(1 for line in kit if line["serialized"]),
        },
        "verdict": "RELEASE" if shorts == 0 else "HOLD",
    }


def record_station_signoff(payload: dict) -> dict:
    """Operator signoff against a station's standard script."""
    zone_id = str(payload.get("zoneId", "")).strip()
    operator = str(payload.get("operator", "")).strip()
    result = str(payload.get("result", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    order_no = str(payload.get("orderNo", "")).strip()
    if result not in ("pass", "fail"):
        raise ValueError("Result must be pass or fail.")
    if not operator:
        raise ValueError("Operator name is required for signoff.")
    if len(operator) > 80 or len(notes) > 400:
        raise ValueError("Signoff text is too long.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM zones WHERE id = %s", (zone_id,))
            if not cur.fetchone():
                raise ValueError("Unknown zone.")
            cur.execute(
                """
                INSERT INTO station_signoffs (zone_id, order_no, operator, result, notes)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (zone_id, order_no or None, operator, result, notes),
            )
            conn.commit()
            cur.execute(
                """
                SELECT operator, result, notes, order_no, created_at
                FROM station_signoffs
                WHERE zone_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 8
                """,
                (zone_id,),
            )
            signoffs = cur.fetchall()

    return {"zoneId": zone_id, "recorded": True, "signoffs": signoffs}


# ===== Standard absorption costing =====
# RM carried at actual (PPV recognized at issue), WIP and finished stock at
# standard. Conversion absorbs by station as work completes; labor rate,
# labor efficiency, and overhead absorption variances post at completion.

RM_ACCOUNT = "1310"
SUBASSY_ACCOUNT = "1315"
WIP_ACCOUNT = "1320"
FG_ACCOUNT = "1330"
PAYROLL_ACCOUNT = "2110"
OH_ACCOUNT = "2120"
OPENING_ACCOUNT = "3000"


def costing_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('cost_entries') AS reg")
    return cur.fetchone()["reg"] is not None


def load_plan_map() -> dict:
    plan_path = BASE_DIR / "production-plan.csv"
    if not plan_path.exists():
        return {}
    with open(plan_path, newline="", encoding="utf-8") as plan_file:
        return {row["station"]: row for row in csv.DictReader(plan_file)}


def load_costing(cur) -> dict | None:
    if not costing_ready(cur):
        return None
    cur.execute("SELECT part_number, standard_cost, actual_cost FROM standard_costs")
    parts = {
        row["part_number"]: {"standard": float(row["standard_cost"]), "actual": float(row["actual_cost"])}
        for row in cur.fetchall()
    }
    cur.execute("SELECT role, standard_rate, actual_rate FROM labor_rates")
    rates = {
        row["role"]: {"standard": float(row["standard_rate"]), "actual": float(row["actual_rate"])}
        for row in cur.fetchall()
    }
    cur.execute("SELECT COALESCE(SUM(pct), 0) AS pct FROM labor_overheads")
    oh_pct = float(cur.fetchone()["pct"]) / 100
    return {"parts": parts, "rates": rates, "oh_pct": oh_pct, "plan": load_plan_map()}


def cost_card(cur, costing, sku, _depth=0) -> dict:
    """Standard cost build-up per unit: DM at standard (make components at
    their own card cost), DL from plan labor minutes x role standard rates,
    OH applied as the overhead percentage of DL."""
    finished = resolve_finished_good(cur, sku)
    cur.execute(
        """
        SELECT bi.part_number, bi.description, bi.quantity, bi.unit,
               m.material_type AS component_type
        FROM bom_items bi
        LEFT JOIN materials m ON m.sku = bi.part_number AND m.material_type = 'finished'
        WHERE bi.parent_material_id = %s AND bi.part_number <> %s
        ORDER BY bi.id
        """,
        (finished["material_id"], sku),
    )
    dm_lines = []
    for row in cur.fetchall():
        bom_qty = float(row["quantity"])
        if row["component_type"] == "finished" and _depth == 0:
            sub_card = cost_card(cur, costing, row["part_number"], _depth=1)
            unit_std = sub_card["unit_std"]
            unit_actual = sub_card["unit_std"]  # make items transfer at standard
            source = "make"
        else:
            price = costing["parts"].get(row["part_number"], {"standard": 0.0, "actual": 0.0})
            unit_std = price["standard"]
            unit_actual = price["actual"]
            source = "buy"
        dm_lines.append(
            {
                "part_number": row["part_number"],
                "description": row["description"],
                "quantity": bom_qty,
                "unit": row["unit"],
                "source": source,
                "unit_std": round(unit_std, 2),
                "unit_actual": round(unit_actual, 2),
                "ext_std": round(unit_std * bom_qty, 2),
                "ext_actual": round(unit_actual * bom_qty, 2),
            }
        )
    dm_std = round(sum(line["ext_std"] for line in dm_lines), 2)

    route_steps = fetch_route_steps(cur, finished["facility_id"])
    zone_order = [route_steps[0]["source_zone_id"]] + [step["target_zone_id"] for step in route_steps]
    labor_lines = []
    for zone_id in zone_order:
        plan = costing["plan"].get(PLAN_STATION_BY_ZONE.get(zone_id, ""))
        if not plan:
            continue
        minutes = float(plan["labor_minutes"])
        role = plan["primary_role"]
        rate = costing["rates"].get(role, {"standard": 0.0, "actual": 0.0})
        labor_lines.append(
            {
                "zone_id": zone_id,
                "station": plan["station"],
                "role": role,
                "minutes": minutes,
                "rate_std": rate["standard"],
                "rate_actual": rate["actual"],
                "cost_std": round(minutes * rate["standard"] / 60, 2),
            }
        )
    dl_std = round(sum(line["cost_std"] for line in labor_lines), 2)
    oh_std = round(dl_std * costing["oh_pct"], 2)
    return {
        "sku": sku,
        "facility_id": finished["facility_id"],
        "dm_lines": dm_lines,
        "dm_std": dm_std,
        "labor_lines": labor_lines,
        "dl_std": dl_std,
        "oh_pct": round(costing["oh_pct"] * 100, 1),
        "oh_std": oh_std,
        "unit_std": round(dm_std + dl_std + oh_std, 2),
    }


def post_cost_entry(cur, event_ref, order_id, order_no, event_type, memo, lines) -> bool:
    """Insert one balanced double-entry journal entry; idempotent by event_ref."""
    lines = [
        (account, round(debit, 2), round(credit, 2))
        for account, debit, credit in lines
        if round(debit, 2) > 0 or round(credit, 2) > 0
    ]
    if not lines:
        return False
    total_debit = round(sum(line[1] for line in lines), 2)
    total_credit = round(sum(line[2] for line in lines), 2)
    if abs(total_debit - total_credit) > 0.005:
        raise RuntimeError(
            f"Unbalanced cost entry {event_ref}: DR {total_debit} vs CR {total_credit}"
        )
    cur.execute(
        """
        INSERT INTO cost_entries (event_ref, production_order_id, order_no, event_type, memo)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (event_ref) DO NOTHING
        RETURNING id
        """,
        (event_ref, order_id, order_no, event_type, memo),
    )
    inserted = cur.fetchone()
    if not inserted:
        return False
    for account, debit, credit in lines:
        cur.execute(
            "INSERT INTO cost_lines (entry_id, account_no, debit, credit) VALUES (%s, %s, %s, %s)",
            (inserted["id"], account, debit, credit),
        )
    return True


def dm_issue_amounts(card, quantity) -> tuple[float, float, float, float]:
    """(WIP debit at std, RM credit at actual, subassembly credit at std, PPV)."""
    wip_std = round(card["dm_std"] * quantity, 2)
    rm_actual = round(
        sum(line["ext_actual"] for line in card["dm_lines"] if line["source"] == "buy") * quantity, 2
    )
    sub_std = round(
        sum(line["ext_std"] for line in card["dm_lines"] if line["source"] == "make") * quantity, 2
    )
    ppv = round(rm_actual + sub_std - wip_std, 2)
    return wip_std, rm_actual, sub_std, ppv


def station_conversion(card, costing, quantity) -> list[dict]:
    """Per-station standard conversion cost (DL + OH applied) for an order."""
    rows = []
    for labor in card["labor_lines"]:
        dl = round(labor["minutes"] * quantity * labor["rate_std"] / 60, 2)
        oh = round(dl * costing["oh_pct"], 2)
        rows.append({"zone_id": labor["zone_id"], "station": labor["station"], "dl": dl, "oh": oh})
    return rows


def ensure_opening_entry(cur, costing) -> None:
    """Book opening inventory once: RM at actual, cases and drones at standard."""
    cur.execute("SELECT 1 FROM cost_entries WHERE event_ref = 'OPENING-BAL'")
    if cur.fetchone():
        return
    cur.execute(
        "SELECT part_number, SUM(quantity_on_hand) AS on_hand FROM inventory_items GROUP BY part_number"
    )
    stock = {row["part_number"]: float(row["on_hand"]) for row in cur.fetchall()}
    rm_value = round(
        sum(qty * costing["parts"][part]["actual"] for part, qty in stock.items() if part in costing["parts"]), 2
    )
    case_std = cost_card(cur, costing, "CASE-FG-500")["unit_std"]
    drone_std = cost_card(cur, costing, "DRN-FG-600")["unit_std"]
    case_value = round(stock.get("CASE-FG-500", 0) * case_std, 2)
    drone_value = round(stock.get("DRN-FG-600", 0) * drone_std, 2)
    total = round(rm_value + case_value + drone_value, 2)
    if total <= 0:
        return
    post_cost_entry(
        cur,
        "OPENING-BAL",
        None,
        None,
        "opening",
        "Opening inventory balances at first costing run (RM at actual, stock at standard)",
        [
            (RM_ACCOUNT, rm_value, 0),
            (SUBASSY_ACCOUNT, case_value, 0),
            (FG_ACCOUNT, drone_value, 0),
            (OPENING_ACCOUNT, 0, total),
        ],
    )


def sync_costing(cur, states) -> tuple[dict | None, dict]:
    """Post journal entries for the current production state: opening balances
    once, DM issue when an order starts, and conversion cost as stations
    complete. Idempotent via event_ref. Returns (costing, cards) for reuse."""
    costing = load_costing(cur)
    if costing is None:
        return None, {}
    ensure_opening_entry(cur, costing)

    cards = {}
    order_ids = [state["order"]["id"] for state in states if state["order"]["id"] and state["order"]["id"] > 0]
    posted = set()
    if order_ids:
        cur.execute(
            "SELECT event_ref FROM cost_entries WHERE production_order_id = ANY(%s)", (order_ids,)
        )
        posted = {row["event_ref"] for row in cur.fetchall()}

    for state in states:
        order = state["order"]
        if not order["id"] or order["id"] < 0 or order["production_status"] == "queued":
            continue
        sku = order["finished_good"]
        if sku not in cards:
            cards[sku] = cost_card(cur, costing, sku)
        card = cards[sku]
        quantity = order["quantity"]

        dm_ref = f"PO{order['id']}-DM"
        if dm_ref not in posted:
            wip_std, rm_actual, sub_std, ppv = dm_issue_amounts(card, quantity)
            lines = [(WIP_ACCOUNT, wip_std, 0), (RM_ACCOUNT, 0, rm_actual)]
            if sub_std:
                lines.append((SUBASSY_ACCOUNT, 0, sub_std))
            if ppv > 0:
                lines.append(("5210", ppv, 0))
            elif ppv < 0:
                lines.append(("5210", 0, -ppv))
            post_cost_entry(
                cur, dm_ref, order["id"], order["order_no"], "dm_issue",
                f"Materials issued to WIP at standard for {order['order_no']} (PPV recognized at issue)",
                lines,
            )

        completed_zones = {
            row["zone_id"] for row in state["balances"] if row["completed_quantity"] > 0
        }
        for conv in station_conversion(card, costing, quantity):
            if conv["zone_id"] not in completed_zones:
                continue
            conv_ref = f"PO{order['id']}-{conv['zone_id']}-CONV"
            if conv_ref in posted:
                continue
            post_cost_entry(
                cur, conv_ref, order["id"], order["order_no"], "conversion",
                f"{conv['station']} conversion absorbed at standard for {order['order_no']}",
                [
                    (WIP_ACCOUNT, round(conv["dl"] + conv["oh"], 2), 0),
                    (PAYROLL_ACCOUNT, 0, conv["dl"]),
                    (OH_ACCOUNT, 0, conv["oh"]),
                ],
            )
    return costing, cards


def post_completion_costing(cur, order, costing, card) -> None:
    """Close an order's costing at completion: labor rate / efficiency and
    overhead absorption variances, transfer to finished stock at standard,
    and the per-order cost summary row."""
    quantity = order["quantity"]
    cur.execute(
        "SELECT source_zone_id, expected_minutes, standard_minutes FROM process_steps WHERE facility_id = %s",
        (order["facility_id"],),
    )
    ratios = {
        row["source_zone_id"]: (
            row["expected_minutes"] / row["standard_minutes"] if row["standard_minutes"] else 1.0
        )
        for row in cur.fetchall()
    }

    conv_rows = station_conversion(card, costing, quantity)
    dl_applied = round(sum(row["dl"] for row in conv_rows), 2)
    oh_applied = round(sum(row["oh"] for row in conv_rows), 2)

    actual_dl = 0.0
    lrv = 0.0
    for labor in card["labor_lines"]:
        ratio = ratios.get(labor["zone_id"], 1.0)
        std_minutes = labor["minutes"] * quantity
        actual_minutes = std_minutes * ratio
        actual_dl = round(actual_dl + actual_minutes * labor["rate_actual"] / 60, 2)
        lrv = round(lrv + actual_minutes * (labor["rate_actual"] - labor["rate_std"]) / 60, 2)
    lev = round(actual_dl - dl_applied - lrv, 2)
    actual_oh = round(actual_dl * costing["oh_pct"], 2)
    oh_variance = round(actual_oh - oh_applied, 2)

    variance_lines = []
    for account, amount, clearing in (
        ("5230", lrv, PAYROLL_ACCOUNT),
        ("5240", lev, PAYROLL_ACCOUNT),
        ("5250", oh_variance, OH_ACCOUNT),
    ):
        if amount > 0:
            variance_lines += [(account, amount, 0), (clearing, 0, amount)]
        elif amount < 0:
            variance_lines += [(clearing, -amount, 0), (account, 0, -amount)]
    if variance_lines:
        post_cost_entry(
            cur, f"PO{order['id']}-VAR", order["id"], order["order_no"], "variances",
            f"Labor and overhead variances recognized at completion of {order['order_no']}",
            variance_lines,
        )

    wip_std, rm_actual, sub_std, ppv = dm_issue_amounts(card, quantity)
    # Transfer exactly what was absorbed into WIP for this order (the posted
    # ledger, not a recomputed card), so WIP zeroes to the penny even if a
    # standard was edited while the order was in flight.
    cur.execute(
        """
        SELECT COALESCE(SUM(l.debit), 0) - COALESCE(SUM(l.credit), 0) AS wip_balance
        FROM cost_lines l
        JOIN cost_entries e ON e.id = l.entry_id
        WHERE e.production_order_id = %s AND l.account_no = %s
        """,
        (order["id"], WIP_ACCOUNT),
    )
    posted_wip = round(float(cur.fetchone()["wip_balance"]), 2)
    if posted_wip <= 0:
        posted_wip = round(wip_std + dl_applied + oh_applied, 2)
    # Stock always enters at the CURRENT standard; any difference against what
    # was absorbed (a standard edited mid-flight) posts to Standards
    # Revaluation, so the stock-at-standard tie-outs hold through changes.
    stock_value = round(card["unit_std"] * quantity, 2)
    stock_account = FG_ACCOUNT if order["finished_good"] == "DRN-FG-600" else SUBASSY_ACCOUNT
    fg_lines = [(stock_account, stock_value, 0), (WIP_ACCOUNT, 0, posted_wip)]
    revaluation = round(posted_wip - stock_value, 2)
    if revaluation > 0:
        # Absorbed more than current-standard stock value: expense the excess.
        fg_lines.append(("5260", revaluation, 0))
    elif revaluation < 0:
        # Absorbed less: stock enters higher than cost, credit the difference.
        fg_lines.append(("5260", 0, -revaluation))
    post_cost_entry(
        cur, f"PO{order['id']}-FG", order["id"], order["order_no"], "fg_transfer",
        f"{order['order_no']} transferred to finished stock at current standard",
        fg_lines,
    )
    fg_amount = posted_wip

    cur.execute(
        """
        INSERT INTO order_costs (
          production_order_id, order_no, sku, quantity,
          std_dm, std_dl, std_oh, std_total,
          actual_dm, actual_dl, actual_oh,
          ppv, usage_variance, labor_rate_variance, labor_efficiency_variance,
          oh_variance, total_variance
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s)
        ON CONFLICT (production_order_id) DO NOTHING
        """,
        (
            order["id"], order["order_no"], order["finished_good"], quantity,
            wip_std, dl_applied, oh_applied, fg_amount,
            round(rm_actual + sub_std, 2), actual_dl, actual_oh,
            ppv, lrv, lev, oh_variance,
            round(ppv + lrv + lev + oh_variance, 2),
        ),
    )


def run_simulation_sync(cur) -> list[dict]:
    """The standard polling sync: simulate, persist runtime state, drive
    completions (which also posts costing). Used by the costing endpoints so
    the books stay current whichever page is open."""
    all_states = simulate_active_states(cur)
    for state in all_states:
        sync_order_runtime_status(cur, state)
        if state["order"]["production_status"] == "queued":
            normalize_queued_order(cur, state)
        else:
            sync_workstation_ledger(cur, state["order"], {"balances": state["balances"]})
    complete_finished_pipeline_orders(cur, all_states)
    return all_states


def account_balances(cur) -> list[dict]:
    cur.execute(
        """
        SELECT a.account_no, a.name, a.account_type, a.normal_side,
               COALESCE(SUM(l.debit), 0) AS total_debit,
               COALESCE(SUM(l.credit), 0) AS total_credit
        FROM gl_accounts a
        LEFT JOIN cost_lines l ON l.account_no = a.account_no
        GROUP BY a.account_no, a.name, a.account_type, a.normal_side
        ORDER BY a.account_no
        """
    )
    rows = []
    for row in cur.fetchall():
        debit = float(row["total_debit"])
        credit = float(row["total_credit"])
        balance = round(debit - credit, 2) if row["normal_side"] == "debit" else round(credit - debit, 2)
        rows.append({**row, "total_debit": round(debit, 2), "total_credit": round(credit, 2), "balance": balance})
    return rows


def gl_balance(balances: list[dict], account_no: str) -> float:
    return next((row["balance"] for row in balances if row["account_no"] == account_no), 0.0)


def fetch_cost_cards() -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            costing = load_costing(cur)
            if costing is None:
                raise ValueError("Costing tables are not installed yet.")
            return {
                "cards": [
                    cost_card(cur, costing, "DRN-FG-600"),
                    cost_card(cur, costing, "CASE-FG-500"),
                ]
            }


def fetch_costing_wip() -> dict:
    """Live WIP valuation per active order, tied to the GL WIP balance."""
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            states = run_simulation_sync(cur)
            conn.commit()
            costing = load_costing(cur)
            if costing is None:
                raise ValueError("Costing tables are not installed yet.")
            cards = {}
            rows = []
            absorbed_total = 0.0
            for state in states:
                order = state["order"]
                if order["production_status"] == "complete":
                    continue
                sku = order["finished_good"]
                if sku not in cards:
                    cards[sku] = cost_card(cur, costing, sku)
                card = cards[sku]
                quantity = order["quantity"]
                queued = order["production_status"] == "queued"
                completed_zones = {
                    row["zone_id"] for row in state["balances"] if row["completed_quantity"] > 0
                }
                conv_rows = station_conversion(card, costing, quantity)
                dm_std = 0.0 if queued else round(card["dm_std"] * quantity, 2)
                conv_done = round(
                    sum(row["dl"] + row["oh"] for row in conv_rows if row["zone_id"] in completed_zones), 2
                )
                absorbed = round(dm_std + (0.0 if queued else conv_done), 2)
                at_completion = round(
                    round(card["dm_std"] * quantity, 2)
                    + sum(row["dl"] for row in conv_rows)
                    + sum(row["oh"] for row in conv_rows),
                    2,
                )
                absorbed_total = round(absorbed_total + absorbed, 2)
                rows.append(
                    {
                        "order_no": order["order_no"],
                        "sku": sku,
                        "quantity": quantity,
                        "production_status": order["production_status"],
                        "stations_done": len(completed_zones),
                        "stations_total": len(conv_rows),
                        "dm_absorbed": dm_std,
                        "conversion_absorbed": 0.0 if queued else conv_done,
                        "absorbed": absorbed,
                        "std_at_completion": at_completion,
                    }
                )
            balances = account_balances(cur)
            wip_gl = gl_balance(balances, WIP_ACCOUNT)
    return {
        "orders": rows,
        "absorbed_total": absorbed_total,
        "gl_wip": wip_gl,
        "tie_delta": round(wip_gl - absorbed_total, 2),
    }


def fetch_variance_report() -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            run_simulation_sync(cur)
            conn.commit()
            if not costing_ready(cur):
                raise ValueError("Costing tables are not installed yet.")
            cur.execute(
                """
                SELECT order_no, sku, quantity, std_dm, std_dl, std_oh, std_total,
                       actual_dm, actual_dl, actual_oh, ppv, usage_variance,
                       labor_rate_variance, labor_efficiency_variance, oh_variance,
                       total_variance, completed_at
                FROM order_costs
                ORDER BY completed_at DESC, id DESC
                LIMIT 25
                """
            )
            orders = cur.fetchall()
            cur.execute(
                """
                SELECT COUNT(*) AS orders,
                       COALESCE(SUM(std_total), 0) AS std_total,
                       COALESCE(SUM(ppv), 0) AS ppv,
                       COALESCE(SUM(usage_variance), 0) AS usage_variance,
                       COALESCE(SUM(labor_rate_variance), 0) AS labor_rate_variance,
                       COALESCE(SUM(labor_efficiency_variance), 0) AS labor_efficiency_variance,
                       COALESCE(SUM(oh_variance), 0) AS oh_variance,
                       COALESCE(SUM(total_variance), 0) AS total_variance
                FROM order_costs
                """
            )
            totals = cur.fetchone()
    return {"orders": orders, "totals": totals}


def fetch_cost_ledger(order_no: str | None, limit: int) -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if not costing_ready(cur):
                raise ValueError("Costing tables are not installed yet.")
            params: list[object] = []
            where = ""
            if order_no:
                where = "WHERE e.order_no = %s"
                params.append(order_no)
            params.append(limit)
            cur.execute(
                f"""
                SELECT e.id, e.event_ref, e.order_no, e.event_type, e.memo, e.posted_at
                FROM cost_entries e
                {where}
                ORDER BY e.id DESC
                LIMIT %s
                """,
                params,
            )
            entries = cur.fetchall()
            entry_ids = [entry["id"] for entry in entries]
            lines_by_entry: dict[int, list] = {}
            if entry_ids:
                cur.execute(
                    """
                    SELECT l.entry_id, l.account_no, a.name AS account_name, l.debit, l.credit
                    FROM cost_lines l
                    JOIN gl_accounts a ON a.account_no = l.account_no
                    WHERE l.entry_id = ANY(%s)
                    ORDER BY l.id
                    """,
                    (entry_ids,),
                )
                for line in cur.fetchall():
                    lines_by_entry.setdefault(line["entry_id"], []).append(line)
            for entry in entries:
                entry["lines"] = lines_by_entry.get(entry["id"], [])
    return {"entries": entries}


def fetch_trial_balance() -> dict:
    """Trial balance plus the control checks: balanced books, balanced
    entries, GL-to-operations ties, and ledger immutability."""
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            states = run_simulation_sync(cur)
            conn.commit()
            costing = load_costing(cur)
            if costing is None:
                raise ValueError("Costing tables are not installed yet.")
            balances = account_balances(cur)
            total_debit = round(sum(row["total_debit"] for row in balances), 2)
            total_credit = round(sum(row["total_credit"] for row in balances), 2)

            cur.execute(
                """
                SELECT e.event_ref
                FROM cost_entries e
                JOIN cost_lines l ON l.entry_id = e.id
                GROUP BY e.id, e.event_ref
                HAVING ABS(SUM(l.debit) - SUM(l.credit)) > 0.01
                """
            )
            unbalanced = [row["event_ref"] for row in cur.fetchall()]

            cards = {
                "DRN-FG-600": cost_card(cur, costing, "DRN-FG-600"),
                "CASE-FG-500": cost_card(cur, costing, "CASE-FG-500"),
            }
            # Expected WIP: what the posting rules should have absorbed so far.
            expected_wip = 0.0
            case_in_transit = 0.0
            for state in states:
                order = state["order"]
                if order["production_status"] in ("complete", "queued"):
                    continue
                card = cards.setdefault(
                    order["finished_good"], cost_card(cur, costing, order["finished_good"])
                )
                quantity = order["quantity"]
                completed_zones = {
                    row["zone_id"] for row in state["balances"] if row["completed_quantity"] > 0
                }
                conv_done = sum(
                    row["dl"] + row["oh"]
                    for row in station_conversion(card, costing, quantity)
                    if row["zone_id"] in completed_zones
                )
                expected_wip = round(expected_wip + round(card["dm_std"] * quantity, 2) + conv_done, 2)
                if order["finished_good"] == "DRN-FG-600":
                    case_line = next(
                        (line for line in card["dm_lines"] if line["source"] == "make"), None
                    )
                    if case_line:
                        case_in_transit = round(case_in_transit + case_line["ext_std"] * quantity, 2)

            cur.execute(
                "SELECT part_number, SUM(quantity_on_hand) AS on_hand FROM inventory_items GROUP BY part_number"
            )
            stock = {row["part_number"]: float(row["on_hand"]) for row in cur.fetchall()}
            case_expected = round(
                stock.get("CASE-FG-500", 0) * cards["CASE-FG-500"]["unit_std"] - case_in_transit, 2
            )
            fg_expected = round(stock.get("DRN-FG-600", 0) * cards["DRN-FG-600"]["unit_std"], 2)

            cur.execute(
                "SELECT tgname FROM pg_trigger WHERE tgname IN ('cost_entries_immutable', 'cost_lines_immutable')"
            )
            triggers = [row["tgname"] for row in cur.fetchall()]

            def control(name, ok, detail):
                return {"name": name, "ok": bool(ok), "detail": detail}

            wip_gl = gl_balance(balances, WIP_ACCOUNT)
            case_gl = gl_balance(balances, SUBASSY_ACCOUNT)
            fg_gl = gl_balance(balances, FG_ACCOUNT)
            controls = [
                control(
                    "Trial balance in balance",
                    abs(total_debit - total_credit) <= 0.01,
                    f"Total DR {total_debit:,.2f} vs CR {total_credit:,.2f}",
                ),
                control(
                    "Every journal entry balances",
                    not unbalanced,
                    "All entries balanced" if not unbalanced else f"Unbalanced: {', '.join(unbalanced[:5])}",
                ),
                control(
                    "GL WIP ties to shop-floor absorption",
                    abs(wip_gl - expected_wip) <= 1,
                    f"GL {wip_gl:,.2f} vs simulated absorption {expected_wip:,.2f}",
                ),
                control(
                    "Case subassembly GL ties to case stock at standard",
                    abs(case_gl - case_expected) <= 1,
                    f"GL {case_gl:,.2f} vs stock less in-flight pulls {case_expected:,.2f}",
                ),
                control(
                    "Drone FG GL ties to drone stock at standard",
                    abs(fg_gl - fg_expected) <= 1,
                    f"GL {fg_gl:,.2f} vs stock at standard {fg_expected:,.2f}",
                ),
                control(
                    "Cost ledger immutability enforced",
                    len(triggers) == 2,
                    "UPDATE triggers active on cost_entries and cost_lines"
                    if len(triggers) == 2
                    else "Missing immutability trigger(s)",
                ),
            ]
    return {
        "accounts": balances,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "controls": controls,
        "note": (
            "Raw Materials GL reflects standard-costing issues; physical buy-part "
            "bins are not decremented in this demo, so RM has no physical tie-out."
        ),
    }


def fetch_costing_standards() -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if not costing_ready(cur):
                raise ValueError("Costing tables are not installed yet.")
            cur.execute(
                """
                SELECT sc.part_number, sc.standard_cost, sc.actual_cost, sc.updated_at,
                       (SELECT MIN(bi.description) FROM bom_items bi
                         WHERE bi.part_number = sc.part_number) AS description
                FROM standard_costs sc
                ORDER BY sc.part_number
                """
            )
            materials = cur.fetchall()
            cur.execute("SELECT role, standard_rate, actual_rate, updated_at FROM labor_rates ORDER BY role")
            rates = cur.fetchall()
            cur.execute("SELECT category, description, pct FROM labor_overheads ORDER BY category")
            overheads = cur.fetchall()
            cur.execute(
                """
                SELECT changed_at, actor, item_type, item_key, field, old_value, new_value
                FROM standards_audit
                ORDER BY id DESC
                LIMIT 20
                """
            )
            audit = cur.fetchall()
    return {"materials": materials, "rates": rates, "overheads": overheads, "audit": audit}


def set_costing_standard(payload: dict) -> dict:
    """Edit a material cost or labor rate; every change lands in the audit trail.
    New standards price future postings only - posted entries are immutable."""
    item_type = str(payload.get("itemType", "")).strip()
    key = str(payload.get("key", "")).strip()
    field = str(payload.get("field", "")).strip()
    actor = str(payload.get("actor", "")).strip() or "controller"
    if item_type not in ("material", "labor"):
        raise ValueError("itemType must be material or labor.")
    if field not in ("standard", "actual"):
        raise ValueError("field must be standard or actual.")
    try:
        value = round(float(payload.get("value")), 2)
    except (TypeError, ValueError):
        raise ValueError("value must be a number.")
    if value < 0 or value > 100000:
        raise ValueError("value is out of range.")

    table = "standard_costs" if item_type == "material" else "labor_rates"
    key_column = "part_number" if item_type == "material" else "role"
    column = (
        ("standard_cost" if field == "standard" else "actual_cost")
        if item_type == "material"
        else ("standard_rate" if field == "standard" else "actual_rate")
    )
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {column} AS old_value FROM {table} WHERE {key_column} = %s", (key,))
            existing = cur.fetchone()
            if not existing:
                raise ValueError(f"Unknown {item_type}: {key}")

            # Snapshot the standard cost cards before the change so on-hand
            # finished stock can be revalued to the new standard.
            costing_before = load_costing(cur)
            cards_before = (
                {
                    "CASE-FG-500": cost_card(cur, costing_before, "CASE-FG-500")["unit_std"],
                    "DRN-FG-600": cost_card(cur, costing_before, "DRN-FG-600")["unit_std"],
                }
                if costing_before and field == "standard"
                else None
            )

            cur.execute(
                f"UPDATE {table} SET {column} = %s, updated_at = now() WHERE {key_column} = %s",
                (value, key),
            )
            cur.execute(
                """
                INSERT INTO standards_audit (actor, item_type, item_key, field, old_value, new_value)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (actor, item_type, key, field, str(existing["old_value"]), f"{value:.2f}"),
            )
            audit_id = cur.fetchone()["id"]

            # Standards revaluation: on-hand finished stock moves to the new
            # standard through account 5260, so the stock tie-outs keep holding.
            if cards_before:
                costing_after = load_costing(cur)
                cur.execute(
                    "SELECT part_number, SUM(quantity_on_hand) AS on_hand FROM inventory_items GROUP BY part_number"
                )
                stock = {row["part_number"]: float(row["on_hand"]) for row in cur.fetchall()}
                for sku, account in (("CASE-FG-500", SUBASSY_ACCOUNT), ("DRN-FG-600", FG_ACCOUNT)):
                    on_hand = stock.get(sku, 0)
                    if not on_hand:
                        continue
                    new_std = cost_card(cur, costing_after, sku)["unit_std"]
                    delta = round(on_hand * (new_std - cards_before[sku]), 2)
                    if not delta:
                        continue
                    lines = (
                        [(account, delta, 0), ("5260", 0, delta)]
                        if delta > 0
                        else [("5260", -delta, 0), (account, 0, -delta)]
                    )
                    post_cost_entry(
                        cur, f"REVAL-{audit_id}-{sku}", None, None, "revaluation",
                        f"Revalue {on_hand:g} x {sku} on hand to new standard {new_std:.2f} "
                        f"({key} {field} {existing['old_value']} -> {value:.2f} by {actor})",
                        lines,
                    )
        conn.commit()
    return {"itemType": item_type, "key": key, "field": field, "value": value, "actor": actor}


# ===== Internal audit =====
# Regularly generated audit package: every schedule from the costing working
# papers expressed as machine-checkable assertions, fingerprinted by hash,
# cross-checked and certified by the LLM (or deterministically offline), with
# certifications stored immutably.

AUDIT_SYSTEM = (
    "You are the internal auditor for a drone manufacturing plant's standard "
    "absorption costing system (RM at actual with PPV at issue; WIP and finished "
    "stock at standard; per-role labor rates; overhead at 25% of direct labor; "
    "immutable double-entry cost ledger). You receive the complete audit evidence "
    "package as JSON. Independently CROSS-CHECK it before certifying: (1) recompute "
    "the trial balance footing from the account totals; (2) verify every journal "
    "entry's debits equal its credits from the entry list; (3) rebuild both standard "
    "cost cards from the standards inputs (material qty x standard cost, labor "
    "minutes x rate / 60, overhead pct of direct labor, the case card rolling into "
    "the drone card) and agree them to the system cards; (4) verify each completed "
    "order's variance identity (ppv + usage + labor_rate + labor_efficiency + oh = "
    "total) and that drone unit PPV is consistent across order sizes; (5) evaluate "
    "the control results, the assertions list, the standards change log, and the "
    "non-routine entries (opening, revaluations, adjustments) for propriety and "
    "internal contradictions. Then respond with ONLY a JSON object, no markdown "
    "fences: {\"opinion\": \"UNQUALIFIED\"|\"QUALIFIED\"|\"ADVERSE\", \"basis\": "
    "\"2-4 sentences\", \"findings\": [{\"severity\": \"low\"|\"medium\"|\"high\", "
    "\"area\": \"...\", \"detail\": \"...\"}], \"checks_performed\": [\"...\"]}. "
    "Issue UNQUALIFIED only if all recomputations agree and there are no material "
    "inconsistencies; QUALIFIED for immaterial or clearly scoped exceptions; "
    "ADVERSE for material misstatement or failed controls."
)


def audit_assert(assertions: list, check_id: str, description: str, expected, actual, tolerance=0.0) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        ok = abs(round(float(actual) - float(expected), 2)) <= tolerance
    else:
        ok = expected == actual
    assertions.append(
        {"id": check_id, "check": description, "expected": expected, "actual": actual, "pass": bool(ok)}
    )
    return bool(ok)


def build_audit_package() -> dict:
    """The full audit evidence package, self-describing and machine-checkable."""
    tb = fetch_trial_balance()  # also drives the simulation sync
    cards = fetch_cost_cards()["cards"]
    variances = fetch_variance_report()

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if not costing_ready(cur):
                raise ValueError("Costing tables are not installed yet.")
            cur.execute(
                """
                SELECT e.event_ref, e.event_type, e.order_no, e.posted_at,
                       COALESCE(SUM(l.debit), 0) AS debits, COALESCE(SUM(l.credit), 0) AS credits
                FROM cost_entries e
                LEFT JOIN cost_lines l ON l.entry_id = e.id
                GROUP BY e.id, e.event_ref, e.event_type, e.order_no, e.posted_at
                ORDER BY e.id
                """
            )
            entries = cur.fetchall()
            cur.execute("SELECT part_number, standard_cost, actual_cost FROM standard_costs ORDER BY part_number")
            material_standards = cur.fetchall()
            cur.execute("SELECT role, standard_rate, actual_rate FROM labor_rates ORDER BY role")
            rate_standards = cur.fetchall()
            cur.execute(
                """
                SELECT changed_at, actor, item_type, item_key, field, old_value, new_value
                FROM standards_audit ORDER BY id
                """
            )
            standards_audit = cur.fetchall()
            cur.execute(
                """
                SELECT e.event_ref, e.event_type, e.memo, l.account_no, l.debit, l.credit
                FROM cost_entries e
                JOIN cost_lines l ON l.entry_id = e.id
                WHERE e.event_type IN ('opening', 'revaluation', 'adjustment')
                ORDER BY e.id, l.id
                """
            )
            non_routine = cur.fetchall()
            cur.execute(
                """
                SELECT part_number, SUM(quantity_on_hand) AS on_hand
                FROM inventory_items
                WHERE part_number IN ('CASE-FG-500', 'DRN-FG-600')
                GROUP BY part_number
                """
            )
            finished_stock = cur.fetchall()

    assertions: list[dict] = []
    audit_assert(
        assertions, "A-TB-01", "Trial balance foots (total debits equal total credits)",
        tb["total_debit"], tb["total_credit"], 0.01,
    )
    unbalanced = [e["event_ref"] for e in entries if abs(round(float(e["debits"]) - float(e["credits"]), 2)) > 0.01]
    audit_assert(assertions, "A-JE-01", "Every journal entry balances (unbalanced count)", 0, len(unbalanced))
    refs = [e["event_ref"] for e in entries]
    audit_assert(assertions, "A-JE-02", "Journal entry references are unique", len(refs), len(set(refs)))
    for i, control in enumerate(tb["controls"], start=1):
        audit_assert(assertions, f"A-CT-{i:02d}", f"Control: {control['name']} ({control['detail']})", True, control["ok"])
    for card in cards:
        audit_assert(
            assertions, f"A-CC-{card['sku']}",
            f"Cost card internally consistent for {card['sku']} (DM + DL + OH = unit standard)",
            card["unit_std"], round(card["dm_std"] + card["dl_std"] + card["oh_std"], 2), 0.01,
        )
    identity_breaks = [
        oc["order_no"]
        for oc in variances["orders"]
        if abs(
            round(
                float(oc["ppv"]) + float(oc["usage_variance"]) + float(oc["labor_rate_variance"])
                + float(oc["labor_efficiency_variance"]) + float(oc["oh_variance"])
                - float(oc["total_variance"]),
                2,
            )
        ) > 0.01
    ]
    audit_assert(assertions, "A-VR-01", "Variance identity holds for every completed order (breaks)", 0, len(identity_breaks))
    drone_unit_ppv = sorted(
        {round(float(oc["ppv"]) / oc["quantity"], 2) for oc in variances["orders"] if oc["sku"] == "DRN-FG-600"}
    )
    if len(drone_unit_ppv) > 1:
        audit_assert(assertions, "A-VR-02", "Drone unit PPV constant across order sizes (spread)",
                     0, round(drone_unit_ppv[-1] - drone_unit_ppv[0], 2), 0.01)
    elif drone_unit_ppv:
        audit_assert(assertions, "A-VR-02", "Drone unit PPV constant across order sizes", True, True)

    package = {
        "meta": {
            "entity": "Drone Manufacturing Demo Plant (drones.onadapt.com)",
            "report": "Internal audit package - standard costing cycle",
            "as_of": datetime.now(timezone.utc).isoformat(),
            "basis": (
                "Standard absorption costing. RM at actual (PPV at issue); WIP and finished stock "
                "at standard; per-role labor rates; OH applied at 25% of DL. All amounts USD. "
                "Positive variances are unfavorable. RM has no physical tie-out by design "
                "(buy-part bins are not decremented in this demo)."
            ),
        },
        "trial_balance": {
            "accounts": tb["accounts"],
            "total_debit": tb["total_debit"],
            "total_credit": tb["total_credit"],
        },
        "controls": tb["controls"],
        "journal_entries": entries,
        "cost_cards": cards,
        "order_costs": variances["orders"],
        "variance_totals": variances["totals"],
        "standards": {
            "materials": material_standards,
            "labor_rates": rate_standards,
            "overhead_pct_of_dl": 25.0,
        },
        "standards_audit": standards_audit,
        "non_routine_entries": non_routine,
        "finished_stock": finished_stock,
        "assertions": assertions,
        "assertion_summary": {
            "total": len(assertions),
            "passed": sum(1 for a in assertions if a["pass"]),
        },
    }
    canonical = json.dumps(package, sort_keys=True, default=str)
    package["package_hash"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return package


def parse_certification_json(text: str) -> dict | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
    try:
        parsed = json.loads(cleaned.strip())
    except (json.JSONDecodeError, IndexError):
        return None
    if parsed.get("opinion") not in ("UNQUALIFIED", "QUALIFIED", "ADVERSE"):
        return None
    parsed.setdefault("basis", "")
    parsed.setdefault("findings", [])
    parsed.setdefault("checks_performed", [])
    return parsed


def offline_certification(package: dict) -> dict:
    failed = [a for a in package["assertions"] if not a["pass"]]
    material = [a for a in failed if a["id"].startswith(("A-TB", "A-JE"))]
    opinion = "ADVERSE" if material else ("QUALIFIED" if failed else "UNQUALIFIED")
    basis = (
        f"Deterministic self-certification (no LLM available): "
        f"{package['assertion_summary']['passed']} of {package['assertion_summary']['total']} "
        f"assertions passed."
        + (f" Failed: {', '.join(a['id'] for a in failed)}." if failed else " No exceptions noted.")
    )
    findings = [
        {"severity": "high" if a in material else "medium", "area": a["id"], "detail": a["check"]}
        for a in failed
    ]
    return {"opinion": opinion, "basis": basis, "findings": findings, "checks_performed": ["system assertions"]}


def run_audit_certification(payload: dict) -> dict:
    """Build the audit package, have the LLM cross-check and certify it (offline
    fallback), and store the certification immutably."""
    actor = str(payload.get("actor", "")).strip() or "manual"
    package = build_audit_package()

    certification = None
    mode = "offline"
    model = None
    if ask_ai_available():
        try:
            import anthropic

            client = anthropic.Anthropic()
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
                system=AUDIT_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(package, default=str)}],
            )
            text = next((block.text for block in response.content if block.type == "text"), "")
            certification = parse_certification_json(text)
            if certification:
                mode = "claude"
                model = ANTHROPIC_MODEL
        except Exception:
            certification = None
    if certification is None:
        certification = offline_certification(package)

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_certifications (
                  package_hash, as_of, actor, mode, model, opinion, basis, findings,
                  assertions_total, assertions_passed
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, certified_at
                """,
                (
                    package["package_hash"], package["meta"]["as_of"], actor, mode, model,
                    certification["opinion"], certification["basis"],
                    json.dumps(certification["findings"]),
                    package["assertion_summary"]["total"], package["assertion_summary"]["passed"],
                ),
            )
            stored = cur.fetchone()
        conn.commit()

    return {
        "certification_id": stored["id"],
        "certified_at": str(stored["certified_at"]),
        "package_hash": package["package_hash"],
        "as_of": package["meta"]["as_of"],
        "actor": actor,
        "mode": mode,
        "model": model,
        "opinion": certification["opinion"],
        "basis": certification["basis"],
        "findings": certification["findings"],
        "checks_performed": certification.get("checks_performed", []),
        "assertion_summary": package["assertion_summary"],
    }


def fetch_audit_certifications() -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('audit_certifications') AS reg")
            if cur.fetchone()["reg"] is None:
                raise ValueError("Audit tables are not installed yet.")
            cur.execute(
                """
                SELECT id, package_hash, as_of, actor, mode, model, opinion, basis,
                       findings, assertions_total, assertions_passed, certified_at
                FROM audit_certifications
                ORDER BY id DESC
                LIMIT 20
                """
            )
            certifications = cur.fetchall()
            for row in certifications:
                try:
                    row["findings"] = json.loads(row["findings"])
                except (TypeError, json.JSONDecodeError):
                    row["findings"] = []
    return {"certifications": certifications}


def fetch_plant_settings() -> dict:
    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            return read_plant_settings(cur)


def set_plant_settings(payload: dict) -> dict:
    """Save the plant working-hours calendar. Null start/end clears to 24/7."""
    work_start = payload.get("workStart")
    work_end = payload.get("workEnd")
    work_days = payload.get("workDays") or []
    time_zone = str(payload.get("timeZone", "UTC")).strip() or "UTC"

    if (work_start is None) != (work_end is None):
        raise ValueError("Set both start and end times, or clear both for 24/7.")
    if work_start is not None:
        try:
            datetime.strptime(str(work_start), "%H:%M")
            datetime.strptime(str(work_end), "%H:%M")
        except ValueError:
            raise ValueError("Times must be HH:MM.")
        if str(work_start) == str(work_end):
            raise ValueError("Start and end must differ; clear both for 24/7.")
        days = sorted({int(day) for day in work_days})
        if not days or any(day < 0 or day > 6 for day in days):
            raise ValueError("Pick at least one working day (Monday through Sunday).")
        work_days = days
    else:
        work_days = []
    try:
        ZoneInfo(time_zone)
    except Exception:
        raise ValueError("Unknown time zone.")

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO plant_settings (id, work_start, work_end, work_days, time_zone)
                VALUES (1, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                SET work_start = EXCLUDED.work_start,
                    work_end = EXCLUDED.work_end,
                    work_days = EXCLUDED.work_days,
                    time_zone = EXCLUDED.time_zone
                """,
                (
                    work_start,
                    work_end,
                    ",".join(str(day) for day in work_days) if work_days else None,
                    time_zone,
                ),
            )
            conn.commit()
            return read_plant_settings(cur)


def build_ask_ai_context() -> tuple[str, dict]:
    """Render the live operations snapshot as compact text for the AI prompt."""
    data = fetch_operations_overview()
    lines = ["LIVE PLANT SNAPSHOT"]
    summary = data["summary"]
    lines.append(
        f"Summary: {summary['active_orders']} active orders, "
        f"{summary['active_quantity']} units in WIP, "
        f"{summary['lines_running']} lines running, "
        f"{summary['shortage_count']} shortages."
    )

    lines.append("Active orders:")
    if data["orders"]:
        for order in data["orders"]:
            line_name = "drone floor" if order["facility_id"] == 1 else "case line"
            lines.append(
                f"- {order['order_no']} ({order['finished_good']}, qty {order['quantity']}, {line_name}): "
                f"{order['production_status']} at {order['current_zone']}, "
                f"{order['percent_complete']}% complete, due {order['due_date']}"
            )
    else:
        lines.append("- none (both lines idle)")

    for pipeline in data["pipelines"]:
        busy = [
            f"{station['station']} (WIP {station['wip']}"
            + (f", capacity {station['capacity']}" if station.get("capacity") else "")
            + f", orders {', '.join(station['orders'])})"
            for station in pipeline["stations"]
            if station["wip"] > 0
        ]
        utilization = ""
        if pipeline.get("ceiling_per_hour"):
            utilization = (
                f"; ceiling {pipeline['ceiling_per_hour']}/hr at {pipeline['bottleneck_station']}, "
                f"last-24h output {pipeline['output_24h']} ({pipeline['pct_of_ceiling_24h']}% of ceiling)"
            )
        lines.append(
            f"{pipeline['facility_name']}: {pipeline['active_orders']} active orders; "
            + ("busy stations: " + "; ".join(busy) if busy else "all stations idle")
            + utilization
        )

    hours = data.get("work_hours") or {}
    if hours.get("work_start") and hours.get("work_end"):
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        days = ", ".join(day_names[day] for day in hours["work_days"])
        lines.append(
            f"Plant working hours: {hours['work_start']}-{hours['work_end']} on {days} "
            f"({hours['time_zone']}); schedules skip off-shift time."
        )
    else:
        lines.append("Plant working hours: 24/7.")

    lines.append("Inventory watch (on hand/allocated/available, min-max):")
    for row in data["inventory_watch"]:
        lines.append(
            f"- {row['part_number']} ({row['item_name']}, {row['area']} at {row['location']}): "
            f"{row['quantity_on_hand']}/{row['quantity_allocated']}/{row['quantity_available']}, "
            f"min {row['min_quantity']} max {row['max_quantity']}, status {row['status']}"
        )

    if data["shortages"]:
        lines.append("Shortages on open orders:")
        for shortage in data["shortages"]:
            lines.append(
                f"- {shortage['order_no']} is short {shortage['required_quantity']} {shortage['unit']} "
                f"of {shortage['part_number']} ({shortage['description']})"
            )
    else:
        lines.append("Shortages: none.")

    if data["completed_today"]:
        lines.append("Completed in the last 24 hours:")
        for completed in data["completed_today"]:
            lines.append(
                f"- {completed['quantity']} x {completed['sku']} across {completed['orders']} order(s)"
            )
    else:
        lines.append("Completed in the last 24 hours: nothing yet.")

    lines.append("Recent inventory movements (newest first):")
    for txn in data["recent_transactions"]:
        route = ""
        if txn["from_zone"]:
            route += f" from {txn['from_zone']}"
        if txn["to_zone"]:
            route += f" to {txn['to_zone']}"
        lines.append(
            f"- {txn['transaction_type']} {txn['quantity']} x {txn['part_number']}{route} ({txn['reference']})"
        )

    return "\n".join(lines), data


def offline_ask_ai_answer(context_text: str) -> str:
    """No-Claude fallback: return the snapshot itself as a readable answer."""
    lines = context_text.split("\n")[1:]
    return "Here is the current plant status:\n" + "\n".join(
        line if line.startswith("-") else f"**{line}**" for line in lines if line.strip()
    )


def ask_ai(payload: dict) -> dict:
    question = str(payload.get("question", "")).strip()
    if not question:
        raise ValueError("Ask a question about the plant.")
    if len(question) > 2000:
        raise ValueError("Question is too long.")

    context_text, _ = build_ask_ai_context()

    if not ask_ai_available():
        return {
            "answer": offline_ask_ai_answer(context_text),
            "mode": "offline",
            "note": "Offline mode - set ANTHROPIC_API_KEY on the server to enable Claude.",
        }

    try:
        import anthropic
    except ImportError:
        return {
            "answer": offline_ask_ai_answer(context_text),
            "mode": "offline",
            "note": "Offline mode - the anthropic Python package is not installed on the server.",
        }

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=ASK_AI_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"{context_text}\n\nQuestion: {question}",
                }
            ],
        )
        answer = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        if not answer:
            raise RuntimeError("Claude returned no text answer")
        return {"answer": answer, "mode": "claude", "model": ANTHROPIC_MODEL}
    except anthropic.AuthenticationError:
        note = "Claude unavailable - the configured API key was rejected."
    except anthropic.RateLimitError:
        note = "Claude unavailable - rate limited, try again shortly."
    except anthropic.APIConnectionError:
        note = "Claude unavailable - network error reaching the API."
    except anthropic.APIStatusError as exc:
        note = f"Claude unavailable - API error {exc.status_code}."
    except Exception as exc:
        note = f"Claude unavailable - {type(exc).__name__}."
    return {
        "answer": offline_ask_ai_answer(context_text),
        "mode": "offline",
        "note": note,
    }


def reset_activity(payload: dict) -> dict:
    if str(payload.get("confirm", "")).strip() != "RESET":
        raise ValueError('Type RESET to confirm deleting all production activity.')

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT reset_activity()")
            cur.execute(
                """
                SELECT
                  (SELECT count(*) FROM production_orders) AS production_orders,
                  (SELECT count(*) FROM inventory_transactions) AS inventory_transactions,
                  (SELECT count(*) FROM work_orders) AS work_orders,
                  (SELECT count(*) FROM production_workstation_ledger) AS ledger_rows,
                  (SELECT count(*) FROM bom_items) AS bom_items,
                  (SELECT count(*) FROM zones) AS zones,
                  (SELECT count(*) FROM materials) AS materials
                """
            )
            counts = cur.fetchone()
        conn.commit()

    return {"reset": True, "counts": counts}


def create_order(payload: dict) -> dict:
    order_no = str(payload.get("orderNo", "")).strip()
    quantity = int(payload.get("quantity", 0))
    due_date = str(payload.get("dueDate", "")).strip()
    start_date = str(payload.get("startDate", "")).strip()
    finished_sku = str(payload.get("finishedSku", DEFAULT_FINISHED_SKU)).strip() or DEFAULT_FINISHED_SKU

    if not order_no:
        order_no = fetch_next_order_no(finished_sku)
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")
    if not due_date:
        raise ValueError("Due date is required.")

    sql = "SELECT create_production_order(%s, %s, %s, p_finished_sku => %s) AS order_id"
    params: list[object] = [order_no, quantity, due_date, finished_sku]
    if start_date:
        sql = "SELECT create_production_order(%s, %s, %s, %s, %s) AS order_id"
        params = [order_no, quantity, due_date, start_date, finished_sku]

    with psycopg.connect(require_database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM materials WHERE sku = %s AND material_type = 'finished'",
                (finished_sku,),
            )
            if cur.fetchone() is None:
                raise ValueError(f"Unknown finished good {finished_sku}.")

            active_states = simulate_active_states(cur)
            complete_finished_pipeline_orders(cur, active_states)
            cur.execute(sql, params)
            order_id = cur.fetchone()["order_id"]
            active_states = simulate_active_states(cur)
            new_state = next((state for state in active_states if state["order"]["id"] == order_id), None)
            if new_state and new_state["order"]["production_status"] == "queued":
                normalize_queued_order(cur, new_state)
        conn.commit()

    return fetch_order_snapshot(order_no)


class ManufacturingHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        if path == "/api/production-orders/latest":
            try:
                json_response(self, HTTPStatus.OK, fetch_order_snapshot(query.get("orderNo", [None])[0]))
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/production-orders/history":
            try:
                json_response(self, HTTPStatus.OK, fetch_order_history())
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/production-orders/next-number":
            try:
                finished_sku = query.get("sku", [DEFAULT_FINISHED_SKU])[0] or DEFAULT_FINISHED_SKU
                json_response(self, HTTPStatus.OK, {"orderNo": fetch_next_order_no(finished_sku)})
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/floor-dashboard":
            try:
                facility_id = int(query.get("facility", ["1"])[0])
                json_response(self, HTTPStatus.OK, fetch_floor_dashboard(facility_id))
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/operations-overview":
            try:
                json_response(self, HTTPStatus.OK, fetch_operations_overview())
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/ask-ai/status":
            json_response(self, HTTPStatus.OK, {"live": ask_ai_available(), "model": ANTHROPIC_MODEL})
            return
        if path == "/api/schedule":
            try:
                json_response(self, HTTPStatus.OK, fetch_schedule())
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/plant-settings":
            try:
                json_response(self, HTTPStatus.OK, fetch_plant_settings())
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/labor-standards":
            try:
                json_response(self, HTTPStatus.OK, fetch_labor_standards())
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path in (
            "/api/costing/cost-cards",
            "/api/costing/wip",
            "/api/costing/variances",
            "/api/costing/ledger",
            "/api/costing/trial-balance",
            "/api/costing/standards",
            "/api/audit/package",
            "/api/audit/certifications",
        ):
            try:
                if path == "/api/costing/cost-cards":
                    payload = fetch_cost_cards()
                elif path == "/api/costing/wip":
                    payload = fetch_costing_wip()
                elif path == "/api/costing/variances":
                    payload = fetch_variance_report()
                elif path == "/api/costing/ledger":
                    order_no = query.get("orderNo", [None])[0]
                    limit = min(max(int(query.get("limit", ["30"])[0]), 1), 200)
                    payload = fetch_cost_ledger(order_no, limit)
                elif path == "/api/costing/trial-balance":
                    payload = fetch_trial_balance()
                elif path == "/api/audit/package":
                    payload = build_audit_package()
                elif path == "/api/audit/certifications":
                    payload = fetch_audit_certifications()
                else:
                    payload = fetch_costing_standards()
                json_response(self, HTTPStatus.OK, payload)
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/kit-check":
            try:
                finished_sku = query.get("sku", [DEFAULT_FINISHED_SKU])[0] or DEFAULT_FINISHED_SKU
                quantity = int(query.get("qty", ["1"])[0])
                json_response(self, HTTPStatus.OK, kit_check(finished_sku, quantity))
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if path == "/api/station":
            try:
                zone_id = query.get("zone", [""])[0].strip()
                if not zone_id:
                    raise ValueError("zone parameter is required")
                json_response(self, HTTPStatus.OK, fetch_station(zone_id))
            except ValueError as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in (
            "/api/production-orders",
            "/api/reset-activity",
            "/api/ask-ai",
            "/api/zone-capacity",
            "/api/zone-cycle",
            "/api/order-priority",
            "/api/plant-settings",
            "/api/station-signoff",
            "/api/costing/standards",
            "/api/audit/certify",
            "/api/schedule/preview",
            "/api/schedule/max-output",
        ):
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/schedule/preview":
                json_response(self, HTTPStatus.OK, preview_schedule(payload))
                return
            if path == "/api/schedule/max-output":
                json_response(self, HTTPStatus.OK, max_output_schedule(payload))
                return
            if path == "/api/zone-capacity":
                json_response(self, HTTPStatus.OK, set_zone_capacity(payload))
                return
            if path == "/api/zone-cycle":
                json_response(self, HTTPStatus.OK, set_zone_cycle(payload))
                return
            if path == "/api/order-priority":
                json_response(self, HTTPStatus.OK, set_order_priority(payload))
                return
            if path == "/api/plant-settings":
                json_response(self, HTTPStatus.OK, set_plant_settings(payload))
                return
            if path == "/api/station-signoff":
                json_response(self, HTTPStatus.OK, record_station_signoff(payload))
                return
            if path == "/api/costing/standards":
                json_response(self, HTTPStatus.OK, set_costing_standard(payload))
                return
            if path == "/api/audit/certify":
                json_response(self, HTTPStatus.OK, run_audit_certification(payload))
                return
            if path == "/api/ask-ai":
                json_response(self, HTTPStatus.OK, ask_ai(payload))
                return
            if path == "/api/reset-activity":
                json_response(self, HTTPStatus.OK, reset_activity(payload))
                return
            json_response(self, HTTPStatus.CREATED, create_order(payload))
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except psycopg.errors.UniqueViolation:
            json_response(self, HTTPStatus.CONFLICT, {"error": "That production order already exists."})
        except psycopg.errors.RaiseException as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": exc.diag.message_primary or str(exc)})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), ManufacturingHandler)
    print(f"Manufacturing app running at http://127.0.0.1:{port}/production-orders.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
