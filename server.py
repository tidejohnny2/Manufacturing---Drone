import json
import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg.rows import dict_row


BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL")
ACTIVE_STATUSES = ("planned", "released", "in_progress", "hold")
DEFAULT_FINISHED_SKU = "DRN-FG-600"
ORDER_PREFIXES = {"DRN-FG-600": "DRN-PO-", "CASE-FG-500": "CASE-PO-"}


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
               ps.target_zone_id, target.name AS target_zone_name, ps.expected_minutes
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
               po.start_date, po.due_date, po.created_at,
               fz.facility_id
        FROM production_orders po
        JOIN materials m ON m.id = po.finished_material_id
        JOIN zones fz ON fz.id = m.default_zone_id
        JOIN zones z ON z.id = po.current_zone_id
        WHERE po.status IN ('planned', 'released', 'in_progress', 'hold')
        ORDER BY po.created_at, po.id
        """
    )
    return cur.fetchall()


def build_zone_model(route_steps: list[dict]) -> tuple[list[str], dict, dict, dict]:
    entry_zone_id = route_steps[0]["source_zone_id"]
    zone_order = [entry_zone_id] + [step["target_zone_id"] for step in route_steps]
    zone_names = {entry_zone_id: route_steps[0]["source_zone_name"]}
    durations = {}
    standard_durations = {}

    for step in route_steps:
        zone_names[step["source_zone_id"]] = step["source_zone_name"]
        zone_names[step["target_zone_id"]] = step["target_zone_name"]
        standard_durations[step["source_zone_id"]] = step["expected_minutes"]

    simulation_factor = simulation_factor_for(sum(standard_durations.values()))
    durations = {
        zone_id: expected_minutes * simulation_factor
        for zone_id, expected_minutes in standard_durations.items()
    }

    return zone_order, zone_names, durations, standard_durations


def simulated_pipeline_state(orders: list[dict], route_steps: list[dict]) -> list[dict]:
    zone_order, zone_names, durations, standard_durations = build_zone_model(route_steps)
    total_minutes = sum(durations.values())
    recorded_minutes = sum(standard_durations.values())
    simulation_factor = total_minutes / recorded_minutes if recorded_minutes else read_simulation_factor()
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

        for zone_id in zone_order[:-1]:
            duration = durations.get(zone_id, 0)
            start_time = max(previous_end, station_available[zone_id])
            end_time = start_time + timedelta(minutes=duration)
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
                    current_elapsed = (now - segment["start"]).total_seconds() / 60
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

        states.append({"order": state_order, "balances": balances})

    return states


def simulate_active_states(cur, facility_id: int | None = None) -> list[dict]:
    """Simulate active orders per facility so each product line runs its own route."""
    orders = fetch_active_orders(cur)
    if facility_id is not None:
        orders = [order for order in orders if order["facility_id"] == facility_id]

    states = []
    for current_facility_id in sorted({order["facility_id"] for order in orders}):
        facility_orders = [order for order in orders if order["facility_id"] == current_facility_id]
        route_steps = fetch_route_steps(cur, current_facility_id)
        if route_steps:
            states.extend(simulated_pipeline_state(facility_orders, route_steps))
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
                           po.start_date, po.due_date, po.created_at,
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
                active_states = []
                for facility_id in sorted({active["facility_id"] for active in active_orders}):
                    facility_orders = [active for active in active_orders if active["facility_id"] == facility_id]
                    route_steps = fetch_route_steps(cur, facility_id)
                    if route_steps:
                        active_states.extend(simulated_pipeline_state(facility_orders, route_steps))
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
            # Simulate every facility so case-line orders keep completing and
            # posting inventory even when only the drone floor map is open.
            all_states = simulate_active_states(cur)
            if not all_states:
                return {"summary": {"active_orders": 0, "active_quantity": 0}, "zones": []}

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
                return {"summary": {"active_orders": 0, "active_quantity": 0}, "zones": []}

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
                "current_zone": display_state["order"]["current_zone"],
                "production_status": display_state["order"]["production_status"],
                "percent_complete": display_state["order"]["percent_complete"],
                "actual_time_utilization_percent": display_state["order"]["actual_time_utilization_percent"],
                "timing_adjustment_percent": display_state["order"]["timing_adjustment_percent"],
                "elapsed_minutes": display_state["order"]["elapsed_minutes"],
                "recorded_minutes": display_state["order"]["recorded_minutes"],
            }

    return {"summary": summary, "zones": zones}


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
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/production-orders":
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
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
