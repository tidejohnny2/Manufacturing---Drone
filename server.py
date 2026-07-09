import csv
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
                    return {"order": None, "balances": [], "materials": [], "activity": [], "ledger": []}
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
                    return {"order": None, "balances": [], "materials": [], "activity": [], "ledger": []}
                selected_state = next(
                    (state for state in active_states if state["order"]["id"] == order["id"]),
                    active_states[-1],
                )
            else:
                active_states = simulate_active_states(cur)
                if not active_states:
                    return {"order": None, "balances": [], "materials": [], "activity": [], "ledger": []}
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

    return {
        "order": order,
        "balances": balances,
        "materials": materials,
        "activity": activity,
        "ledger": ledger,
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
        "plan": load_plan_row(zone_id),
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
