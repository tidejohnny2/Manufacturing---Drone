"""Procurement-backed purchasing (Phase 4c: service-only).

The Purchasing page's vendor master, sourcing catalog, PO register, and policy
inputs are SERVED from the shared onadapt-procurement service over its /v1 API,
and every purchasing WRITE (create PO, receive, vendor / preferred / settings
edits) goes to the service too. Reads return service-native ids. Receiving posts
DR inventory / CR GR-IR then DR GR-IR / CR AP to the GL via the service (net
DR RM / CR AP); the app still fills its local inventory bins + reprices from the
received PO. The one planner input the service doesn't own,
planned_annual_builds, stays in the local purchasing_settings row.

Reads are gated by PROC_READS, writes by PROC_WRITES; when a flag is off the
caller keeps its own local path (usable only while the local tables still
exist — after the Phase-4c drop the flags stay on).

Config (manufacturing.env):
    PROC_BASE_URL   e.g. http://127.0.0.1:8799
    PROC_API_KEY    the manufacturing tenant's bearer key
    PROC_READS      "1" to serve reads from the service
    PROC_WRITES     "1" to route writes through the service
"""
import json
import os
import urllib.error
import urllib.request

PROC_BASE_URL = os.environ.get("PROC_BASE_URL", "").rstrip("/")
PROC_API_KEY = os.environ.get("PROC_API_KEY", "")
PROC_READS = os.environ.get("PROC_READS", "0") == "1"
PROC_WRITES = os.environ.get("PROC_WRITES", "0") == "1"


def enabled() -> bool:
    return PROC_READS and bool(PROC_BASE_URL) and bool(PROC_API_KEY)


def writes_enabled() -> bool:
    return PROC_WRITES and bool(PROC_BASE_URL) and bool(PROC_API_KEY)


def proc_get(path: str) -> dict:
    req = urllib.request.Request(PROC_BASE_URL + path, headers={"Authorization": f"Bearer {PROC_API_KEY}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.load(resp)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


def proc_post(path: str, body: dict) -> dict:
    """POST to the service. Surfaces a service validation error as ValueError so
    the app's endpoints return 400 (matching the local write behaviour)."""
    req = urllib.request.Request(
        PROC_BASE_URL + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {PROC_API_KEY}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            raise ValueError(json.load(exc).get("error") or f"procurement error {exc.code}")
        except ValueError:
            raise
        except Exception:
            raise ValueError(f"procurement error {exc.code}")
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(data["error"])
    return data


def _vendor_map() -> dict:
    return {v["id"]: v for v in proc_get("/v1/vendors").get("vendors", [])}


# ----- reads (service-native ids) ------------------------------------------

def vendors() -> list:
    vs = proc_get("/v1/vendors").get("vendors", [])
    counts: dict = {}
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        counts[po["vendor_id"]] = counts.get(po["vendor_id"], 0) + 1
    return [{"id": v["id"], "account_code": v["account_code"], "name": v["name"],
             "contact": v["contact"], "terms": v["terms"], "lead_time_days": v["lead_time_days"],
             "po_count": counts.get(v["id"], 0)} for v in vs]


def catalog() -> list:
    vmap = _vendor_map()
    out = []
    for o in proc_get("/v1/vendor-parts").get("vendorParts", []):
        vendor = vmap.get(o["vendor_id"], {})
        lead = o["lead_time_days"] if o.get("lead_time_days") is not None else vendor.get("lead_time_days", 7)
        out.append({"id": o["id"], "vendor_id": o["vendor_id"], "vendor": vendor.get("name", ""),
                    "part_number": o["part_number"], "vendor_model": o["vendor_model"],
                    "description": o["description"], "unit_price": float(o["unit_price"]),
                    "moq": o["moq"], "lead_time_days": lead, "availability": o["availability"],
                    "preferred": o["preferred"],
                    "breaks": [{"min_qty": b["minQty"], "unit_price": b["unitPrice"]}
                               for b in o.get("breaks", [])]})
    out.sort(key=lambda c: (c["part_number"], not c["preferred"], c["unit_price"]))
    return out


def purchase_orders() -> list:
    vmap = _vendor_map()
    out = []
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        vendor = vmap.get(po["vendor_id"], {})
        out.append({"id": po["id"], "po_no": po["po_no"], "status": po["status"],
                    "created_at": po["created_at"], "received_at": po["received_at"],
                    "vendor": vendor.get("name", ""), "vendor_code": vendor.get("account_code"),
                    "lines": [{"part_number": l["part_number"], "vendor_model": l["vendor_model"],
                               "quantity": l["quantity"], "unit_price": float(l["unit_price"])}
                              for l in po.get("lines", [])],
                    "total": float(po.get("total", 0))})
    out.sort(key=lambda p: p["id"], reverse=True)
    return out[:40]


def settings() -> dict:
    s = proc_get("/v1/settings?company=1").get("settings", {})
    return {"ordering_cost": float(s["ordering_cost"]),
            "carrying_rate_pct": float(s["carrying_rate_pct"]),
            "safety_stock_days": int(s["safety_stock_days"])}


def audit(company: int = 1) -> dict:
    """PO Audit package from the service — procure-to-pay controls + GR-IR/AP
    tie-outs to the shared GL. Returns {} if procurement is unreachable so the
    Purchasing page degrades gracefully rather than erroring."""
    try:
        return proc_get(f"/v1/audit?company={int(company)}")
    except Exception:
        return {}


def received_po_total() -> float:
    """Sum of received-PO line values (the AP tie-out control's expected AP)."""
    total = 0.0
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        if po.get("status") == "received":
            total += sum(l["quantity"] * float(l["unit_price"]) for l in po.get("lines", []))
    return round(total, 2)


def fetch_po(po_no: str):
    """The service PO (with lines) — used by receiving to read the ordered lines."""
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        if po["po_no"] == po_no:
            return po
    return None


# ----- writes (service is authoritative; raise on error) -------------------

def _next_po_no() -> str:
    """Next VPO-<n>, continuing the tenant's existing numbering on the service."""
    highest = 0
    for po in proc_get("/v1/purchase-orders?company=1").get("purchaseOrders", []):
        pn = po.get("po_no", "")
        if pn.startswith("VPO-"):
            try:
                highest = max(highest, int(pn[4:]))
            except ValueError:
                pass
    return f"VPO-{highest + 1}"


def _next_account_code() -> str:
    highest = 2000
    for v in proc_get("/v1/vendors").get("vendors", []):
        code = v.get("account_code") or ""
        if code.startswith("V-"):
            try:
                highest = max(highest, int(code[2:]))
            except ValueError:
                pass
    return f"V-{highest + 1}"


def create_po(vendor_id, raw_lines) -> dict:
    """Create + issue a VPO-numbered PO on the service. Returns the page's shape."""
    lines = [{"partNumber": str(l.get("partNumber", "")).strip(),
              "quantity": int(l.get("quantity", 0)),
              "unitPrice": l.get("unitPrice")} for l in (raw_lines or [])]
    po_no = _next_po_no()
    res = proc_post("/v1/purchase-orders?company=1",
                    {"company": 1, "poNo": po_no, "vendorId": vendor_id, "lines": lines})
    proc_post("/v1/purchase-orders/issue?company=1", {"company": 1, "poNo": po_no})
    return {"poNo": po_no, "vendor": res.get("vendor"), "lines": res.get("lines"), "total": res.get("total")}


def manage_vendor(payload: dict) -> dict:
    """Create / update / deactivate (=delete) a vendor on the service."""
    action = str(payload.get("action", "")).strip()
    name = str(payload.get("name", "")).strip()
    if action == "create":
        res = proc_post("/v1/vendors", {
            "action": "create", "name": name, "accountCode": _next_account_code(),
            "contact": payload.get("contact", ""), "terms": payload.get("terms"),
            "leadTimeDays": payload.get("leadTimeDays", 7)})
        return {"action": "create", "vendorId": res.get("vendorId"), "name": name}
    if action == "delete":
        proc_post("/v1/vendors", {"action": "deactivate", "vendorId": payload.get("vendorId")})
        return {"action": "delete", "vendorId": payload.get("vendorId"), "name": name}
    proc_post("/v1/vendors", {"action": "update", "vendorId": payload.get("vendorId"), "name": name,
                              "contact": payload.get("contact", ""), "terms": payload.get("terms"),
                              "leadTimeDays": payload.get("leadTimeDays", 7)})
    return {"action": "update", "vendorId": payload.get("vendorId"), "name": name}


def set_preferred(offer_id) -> dict:
    """Set a catalog offer preferred on the service (offer_id is the service id)."""
    res = proc_post("/v1/vendor-parts", {"action": "update", "id": int(offer_id), "preferred": True})
    return {"vendorPartId": int(offer_id), "partNumber": res.get("partNumber")}


def set_settings(payload: dict) -> None:
    """Set the three service-owned policy inputs on the service."""
    proc_post("/v1/settings?company=1", {
        "company": 1, "orderingCost": payload.get("orderingCost"),
        "carryingRatePct": payload.get("carryingRatePct"),
        "safetyStockDays": payload.get("safetyStockDays")})


def push_receive(po_no, lines):
    """Receive a PO through the service (receipt + auto-invoice + 3-way match).
    Resumable/idempotent per PO (checks service PO status + a posted invoice) so
    a retry after a partial failure completes it. RAISES on failure. Returns the
    service receipt no when a receipt was posted on this call (the caller uses it
    to fill bins exactly once); None if the PO was already received (bins were
    filled on the first pass, so the caller skips)."""
    po = fetch_po(po_no)
    if po is None:
        raise RuntimeError(f"{po_no} is not on the procurement service")
    receipt_no = None
    if po["status"] != "received":
        receipt_no = proc_post("/v1/receipts?company=1", {"company": 1, "poNo": po_no}).get("receiptNo")
    already = any(i.get("purchase_order_id") == po["id"] and i.get("status") == "posted"
                  for i in proc_get("/v1/invoices?company=1").get("invoices", []))
    if not already:
        inv = proc_post("/v1/invoices?company=1",
                        {"company": 1, "vendorId": po["vendor_id"], "poNo": po_no, "lines": lines})
        proc_post("/v1/invoices/match?company=1", {"company": 1, "invoiceNo": inv["invoiceNo"]})
    return receipt_no


def reset() -> None:
    """Clear the tenant's purchasing transactions (POs / receipts / invoices) on
    the service — a dev-mode purge must clear the service too, since the register
    reads from it. Best-effort: never raises."""
    if not (PROC_BASE_URL and PROC_API_KEY):
        return
    try:
        proc_post("/v1/reset", {})
    except Exception:
        pass
