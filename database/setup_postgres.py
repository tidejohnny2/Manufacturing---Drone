import os
import sys
from pathlib import Path

try:
    import psycopg
except ImportError as exc:
    raise SystemExit(
        "Missing PostgreSQL driver. Install it with: python -m pip install psycopg[binary]"
    ) from exc


EXPECTED_COUNTS = {
    "facilities": 2,
    "zones": 17,
    "materials": 11,
    "process_steps": 15,
    "inventory_balances": 8,
    "inventory_items": 20,
    "work_orders": 4,
    "bom_items": 29,
    "production_orders": 2,
    "production_order_materials": 27,
    "production_order_operations": 15,
    "workstation_balances": 17,
    "production_order_activity": 2,
    "inventory_transactions": 3,
    "production_workstation_ledger": 2,
}

EXPECTED_FLOWS = {
    1: [
        ("receiving", "raw"),
        ("raw", "ws1"),
        ("ws1", "ws2"),
        ("ws2", "ws3"),
        ("ws3", "ws4"),
        ("ws4", "ws5"),
        ("ws5", "fg"),
        ("fg", "inventory"),
    ],
    2: [
        ("case_receiving", "case_raw"),
        ("case_raw", "cws1"),
        ("cws1", "cws2"),
        ("cws2", "cws3"),
        ("cws3", "cws4"),
        ("cws4", "case_fg"),
        ("case_fg", "case_inventory"),
    ],
}


def read_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def reset_seeded_tables(conn: psycopg.Connection) -> None:
    tables = (
        "bom_items",
        "inventory_transactions",
        "production_workstation_ledger",
        "production_order_activity",
        "workstation_balances",
        "production_order_operations",
        "production_order_materials",
        "production_orders",
        "work_orders",
        "inventory_items",
        "inventory_balances",
        "process_steps",
        "materials",
        "zones",
        "facilities",
    )
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")


def verify_counts(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for table, expected in EXPECTED_COUNTS.items():
            cur.execute(f"SELECT count(*) FROM {table}")
            actual = cur.fetchone()[0]
            if actual != expected:
                raise RuntimeError(f"{table} expected {expected} rows, found {actual}")
            print(f"{table}: {actual}")


def verify_flow(conn: psycopg.Connection) -> None:
    for facility_id, expected_flow in EXPECTED_FLOWS.items():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_zone_id, target_zone_id
                FROM process_steps
                WHERE facility_id = %s
                ORDER BY step_number
                """,
                (facility_id,),
            )
            actual = cur.fetchall()

        if actual != expected_flow:
            raise RuntimeError(f"Unexpected facility {facility_id} process flow: {actual}")

        print(f"Facility {facility_id} flow:")
        for index, (source, target) in enumerate(actual, start=1):
            print(f"{index}. {source} -> {target}")


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "DATABASE_URL is required, for example: "
            "postgresql://postgres:your_pgadmin_password@localhost:5432/manufacturing_floor",
            file=sys.stderr,
        )
        return 2

    base_dir = Path(__file__).resolve().parent
    schema_sql = read_sql(base_dir / "schema.sql")
    seed_sql = read_sql(base_dir / "seed.sql")

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute(schema_sql)
            reset_seeded_tables(conn)
            conn.execute(seed_sql)

        verify_counts(conn)
        verify_flow(conn)

    print("PostgreSQL manufacturing floor database is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
