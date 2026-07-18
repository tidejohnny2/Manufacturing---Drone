"""Procurement-backed reads (Phase 3).

When PROC_READS is on, the Purchasing page's vendor master, sourcing catalog, PO
register, and policy inputs are SERVED from the shared onadapt-procurement
service over its /v1 API (with local fallback on any error). The replenishment
planner then prices off the service's catalog.

Writes (create PO, receive, vendor/preferred edits) still go local in Phase 3.
Before a read, sync_now() pushes the latest local purchasing state into the
service (the service's own mirror --push-only) so a just-created PO is current.
The offer/PO ids the page needs for its still-local write buttons are remapped
back to the LOCAL ids via their natural keys — a Phase-3 bridge, dropped in
Phase 4 when the writes flip to the service.

Config (drones.env):
    PROC_BASE_URL       e.g. http://127.0.0.1:8799
    PROC_API_KEY        the manufacturing tenant's bearer key
    PROC_READS          "1" to serve reads from the service, anything else = local
    PROC_DATABASE_URL   the onadapt_procurement URL (sync target)
    PROC_MIRROR_PYTHON / PROC_MIRROR_SCRIPT   override the sync mirror paths
"""
import json
import os
import subprocess
import urllib.error
import urllib.request

import psycopg
from psycopg.rows import dict_row

PROC_BASE_URL = os.environ.get("PROC_BASE_URL", "").rstrip("/")
PROC_API_KEY = os.environ.get("PROC_API_KEY", "")
PROC_READS = os.environ.get("PROC_READS", "0") == "1"
LOCAL_DATABASE_URL = os.environ.get("DATABASE_URL", "")
PROC_DATABASE_URL = os.environ.get("PROC_DATABASE_URL", "")
PROC_MIRROR_PYTHON = os.environ.get("PROC_MIRROR_PYTHON", "/opt/onadapt-procurement/venv/bin/python")
PROC_MIRROR_SCRIPT = os.environ.get("PROC_MIRROR_SCRIPT", "/opt/onadapt-procurement/mirror.py")


def enabled() -> bool:
    return PROC_READS and bool(PROC_BASE_URL) and bool(PROC_API_KEY)


def proc_get(path: str) -> dict:
    req = urllib.request.Request(PROC_BASE_URL + path, headers={"Authorization": f"Bearer {PROC_API_KEY}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.load(resp)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


def sync_now() -> None:
    """Push the local purchasing state into the service (mirror --push-only) so a
    just-created PO / edited vendor is current before we read it back. Raises on
    failure; the caller catches and reads whatever the service already has."""
    if not (LOCAL_DATABASE_URL and PROC_DATABASE_URL):
        return
    env = {**os.environ, "SOURCE_DATABASE_URL": LOCAL_DATABASE_URL, "DATABASE_URL": PROC_DATABASE_URL}
    subprocess.run([PROC_MIRROR_PYTHON, PROC_MIRROR_SCRIPT, "--push-only"],
                   env=env, check=True, capture_output=True, timeout=30)


def _vendor_map() -> dict:
    return {v["id"]: v for v in proc_get("/v1/vendors").get("vendors", [])}


def _local_offer_ids() -> dict:
    """(vendor_id, part_number) -> local vendor_parts.id (for the Prefer write)."""
    if not LOCAL_DATABASE_URL:
        return {}
    with psycopg.connect(LOCAL_DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, vendor_id, part_number FROM vendor_parts")
        return {(r["vendor_id"], r["part_number"]): r["id"] for r in cur.fetchall()}


def _local_po_ids() -> dict:
    """po_no -> local purchase_orders.id."""
    if not LOCAL_DATABASE_URL:
        return {}
    with psycopg.connect(LOCAL_DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, po_no FROM purchase_orders")
        return {r["po_no"]: r["id"] for r in cur.fetchall()}


def vendors() -> list:
    """Vendor master, shaped like the local fetch_purchasing vendors read."""
    vs = proc_get("/v1/vendors").get("vendors", [])
    counts: dict = {}
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        counts[po["vendor_id"]] = counts.get(po["vendor_id"], 0) + 1
    return [{
        "id": v["id"], "account_code": v["account_code"], "name": v["name"],
        "contact": v["contact"], "terms": v["terms"], "lead_time_days": v["lead_time_days"],
        "po_count": counts.get(v["id"], 0),
    } for v in vs]


def catalog() -> list:
    """Sourcing catalog, shaped like the local read: vendor-name join, lead-time
    coalesce, break-key rename, planner ordering. The offer `id` is remapped to
    the LOCAL vendor_parts id (natural key vendor_id+part_number) so the still-
    local 'Prefer' write stays correct."""
    vmap = _vendor_map()
    local_ids = _local_offer_ids()
    out = []
    for o in proc_get("/v1/vendor-parts").get("vendorParts", []):
        vendor = vmap.get(o["vendor_id"], {})
        lead = o["lead_time_days"] if o.get("lead_time_days") is not None else vendor.get("lead_time_days", 7)
        out.append({
            "id": local_ids.get((o["vendor_id"], o["part_number"]), o["id"]),
            "vendor_id": o["vendor_id"], "vendor": vendor.get("name", ""),
            "part_number": o["part_number"], "vendor_model": o["vendor_model"],
            "description": o["description"], "unit_price": float(o["unit_price"]),
            "moq": o["moq"], "lead_time_days": lead,
            "availability": o["availability"], "preferred": o["preferred"],
            "breaks": [{"min_qty": b["minQty"], "unit_price": b["unitPrice"]} for b in o.get("breaks", [])],
        })
    out.sort(key=lambda c: (c["part_number"], not c["preferred"], c["unit_price"]))
    return out


def purchase_orders() -> list:
    """PO register, shaped like the local pos read (id remapped to the LOCAL po id
    via po_no; ordered id desc, capped at 40)."""
    vmap = _vendor_map()
    local_ids = _local_po_ids()
    out = []
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        vendor = vmap.get(po["vendor_id"], {})
        out.append({
            "id": local_ids.get(po["po_no"], po["id"]),
            "po_no": po["po_no"], "status": po["status"],
            "created_at": po["created_at"], "received_at": po["received_at"],
            "vendor": vendor.get("name", ""), "vendor_code": vendor.get("account_code"),
            "lines": [{"part_number": l["part_number"], "vendor_model": l["vendor_model"],
                       "quantity": l["quantity"], "unit_price": float(l["unit_price"])}
                      for l in po.get("lines", [])],
            "total": float(po.get("total", 0)),
        })
    out.sort(key=lambda p: p["id"], reverse=True)
    return out[:40]


def settings() -> dict:
    """The three service-owned policy inputs. planned_annual_builds is app-specific
    and stays local; the caller merges it in."""
    s = proc_get("/v1/settings?company=1").get("settings", {})
    return {"ordering_cost": float(s["ordering_cost"]),
            "carrying_rate_pct": float(s["carrying_rate_pct"]),
            "safety_stock_days": int(s["safety_stock_days"])}
