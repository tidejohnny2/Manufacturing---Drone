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
PROC_WRITES = os.environ.get("PROC_WRITES", "0") == "1"
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
    coalesce, break-key rename, planner ordering. Returns the SERVICE offer id
    (Phase 4c: local is gone, so the Prefer write uses service ids natively)."""
    vmap = _vendor_map()
    out = []
    for o in proc_get("/v1/vendor-parts").get("vendorParts", []):
        vendor = vmap.get(o["vendor_id"], {})
        lead = o["lead_time_days"] if o.get("lead_time_days") is not None else vendor.get("lead_time_days", 7)
        out.append({
            "id": o["id"],
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
    """PO register, shaped like the local pos read; returns SERVICE po ids
    (Phase 4c). Ordered id desc, capped at 40."""
    vmap = _vendor_map()
    out = []
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        vendor = vmap.get(po["vendor_id"], {})
        out.append({
            "id": po["id"],
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


# ===== Phase 4a: dual-write the GL-safe purchasing writes to the service =====
# When PROC_WRITES is on, each local purchasing write is ALSO written to the
# service (best-effort, never raises), with ids aligned to local (vendor id,
# po_no) so the two stay 1:1. The local write remains authoritative and is the
# safety net; this makes the service write-current so Phase 4c can drop local.
# Receiving (which posts to the GL) and Prefer flip in later stages.

def writes_enabled() -> bool:
    return PROC_WRITES and bool(PROC_BASE_URL) and bool(PROC_API_KEY)


def proc_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        PROC_BASE_URL + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {PROC_API_KEY}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


def push_vendor(vendor_id, deleted: bool = False) -> None:
    """Mirror a vendor create/update/delete to the service (delete -> deactivate,
    since the service never deletes vendors). Best-effort; re-reads local for the
    authoritative row so the account_code + id line up."""
    if not writes_enabled() or vendor_id is None:
        return
    try:
        if deleted:
            proc_post("/v1/vendors", {"action": "deactivate", "vendorId": vendor_id})
            return
        with psycopg.connect(LOCAL_DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, account_code, contact, terms, lead_time_days FROM vendors WHERE id=%s", (vendor_id,))
            v = cur.fetchone()
        if not v:
            return
        body = {"vendorId": v["id"], "name": v["name"], "accountCode": v["account_code"],
                "contact": v["contact"], "terms": v["terms"], "leadTimeDays": v["lead_time_days"]}
        try:
            proc_post("/v1/vendors", {**body, "action": "create"})
        except Exception:
            proc_post("/v1/vendors", {**body, "action": "update"})
    except Exception:
        pass


def push_settings(payload: dict) -> None:
    """Mirror a settings edit (the three service-owned inputs) to the service."""
    if not writes_enabled():
        return
    try:
        proc_post("/v1/settings?company=1", {
            "company": 1, "orderingCost": payload.get("orderingCost"),
            "carryingRatePct": payload.get("carryingRatePct"),
            "safetyStockDays": payload.get("safetyStockDays")})
    except Exception:
        pass


def push_purchase_order(po_no) -> None:
    """Mirror a newly-created local PO to the service with the SAME po_no, then
    issue it (local POs are created issued). Re-reads the local lines so prices
    line up exactly. Best-effort."""
    if not writes_enabled() or not po_no:
        return
    try:
        with psycopg.connect(LOCAL_DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, vendor_id FROM purchase_orders WHERE po_no=%s", (po_no,))
            po = cur.fetchone()
            if not po:
                return
            cur.execute("SELECT part_number, quantity, unit_price FROM purchase_order_lines "
                        "WHERE purchase_order_id=%s ORDER BY id", (po["id"],))
            lines = [{"partNumber": l["part_number"], "quantity": l["quantity"],
                      "unitPrice": float(l["unit_price"])} for l in cur.fetchall()]
        proc_post("/v1/purchase-orders?company=1",
                  {"company": 1, "poNo": po_no, "vendorId": po["vendor_id"], "lines": lines})
        proc_post("/v1/purchase-orders/issue?company=1", {"company": 1, "poNo": po_no})
    except Exception:
        pass


def push_preferred(payload: dict) -> None:
    """Mirror a Prefer toggle: resolve the SERVICE offer id for the local offer's
    (vendor, part) — local sends the local offer id — and set it preferred on the
    service. Best-effort."""
    if not writes_enabled():
        return
    try:
        offer_id = int(payload.get("vendorPartId", 0))
        with psycopg.connect(LOCAL_DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT vendor_id, part_number FROM vendor_parts WHERE id=%s", (offer_id,))
            o = cur.fetchone()
        if not o:
            return
        offers = proc_get(f"/v1/vendor-parts?vendor={o['vendor_id']}").get("vendorParts", [])
        match = next((s for s in offers if s["part_number"] == o["part_number"]), None)
        if match:
            proc_post("/v1/vendor-parts", {"action": "update", "id": match["id"], "preferred": True})
    except Exception:
        pass


# ===== Phase 4b: Receive via the service (auto-invoice at receive) =====
# The one write that posts to the GL. Receiving calls the service to receive +
# auto-create-invoice + auto-match, so the GL gets DR inventory / CR GR-IR then
# DR GR-IR / CR AP (net DR RM / CR AP) — under the manufacturing tenant's GL
# books, via the service's gl_credentials. The local receive stops its own
# DR RM / CR AP post (no double). Bins + reprice stay local.

def _service_po(po_no):
    for p in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        if p["po_no"] == po_no:
            return p
    return None


def push_receive(po_no, lines) -> None:
    """Receive a PO through the service (receipt + auto-invoice + 3-way match).
    Resumable/idempotent per PO so a retry after a partial failure completes it.
    RAISES on failure — receiving must never silently skip the AP posting."""
    po = _service_po(po_no)
    if po is None:
        raise RuntimeError(f"{po_no} is not on the procurement service")
    if po["status"] != "received":
        proc_post("/v1/receipts?company=1", {"company": 1, "poNo": po_no})
    already_posted = any(
        i.get("purchase_order_id") == po["id"] and i.get("status") == "posted"
        for i in proc_get("/v1/invoices?company=1").get("invoices", []))
    if not already_posted:
        inv = proc_post("/v1/invoices?company=1",
                        {"company": 1, "vendorId": po["vendor_id"], "poNo": po_no, "lines": lines})
        proc_post("/v1/invoices/match?company=1", {"company": 1, "invoiceNo": inv["invoiceNo"]})
