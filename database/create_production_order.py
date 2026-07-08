import argparse
import os
import sys

try:
    import psycopg
except ImportError as exc:
    raise SystemExit(
        "Missing PostgreSQL driver. Install it with: python -m pip install psycopg[binary]"
    ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a drone or case production order in PostgreSQL."
    )
    parser.add_argument("order_no", help="Production order number, for example DRN-PO-1002 or CASE-PO-1002")
    parser.add_argument("quantity", type=int, help="Finished good quantity to build")
    parser.add_argument("due_date", help="Due date in YYYY-MM-DD format")
    parser.add_argument(
        "--start-date",
        default=None,
        help="Optional start date in YYYY-MM-DD format. Defaults to current date in PostgreSQL.",
    )
    parser.add_argument(
        "--sku",
        default="DRN-FG-600",
        help="Finished good SKU to build: DRN-FG-600 (drone) or CASE-FG-500 (transport case).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "DATABASE_URL is required, for example: "
            "postgresql://postgres:your_pgadmin_password@localhost:5432/manufacturing_floor",
            file=sys.stderr,
        )
        return 2

    if args.quantity <= 0:
        print("quantity must be greater than zero", file=sys.stderr)
        return 2

    sql = "SELECT create_production_order(%s, %s, %s, p_finished_sku => %s)"
    params = [args.order_no, args.quantity, args.due_date, args.sku]
    if args.start_date:
        sql = "SELECT create_production_order(%s, %s, %s, %s, %s)"
        params = [args.order_no, args.quantity, args.due_date, args.start_date, args.sku]

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            production_order_id = cur.fetchone()[0]
        conn.commit()

    print(f"Created production order {args.order_no} with id {production_order_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
