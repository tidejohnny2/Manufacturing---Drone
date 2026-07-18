"""GL-backed reads + writes (Phase 3-4).

When GL_READS is on, the accounting pages are SERVED from the shared onadapt-gl
service over its /v1 API (with local fallback on any error), and every local
ledger write is POSTED to the GL as a real journal entry via
POST /v1/journal-entries. Manufacturing is a proper API client of the GL — it
posts journals, it does not copy rows into the GL's database. The local ledger
stays as the reconcilable safety net; the GL-side mirror timer only reconciles.

Config comes from the environment (set in drones.env):
    GL_BASE_URL   e.g. http://127.0.0.1:8798
    GL_API_KEY    the manufacturing tenant's bearer key
    GL_READS      "1" to serve reads from the GL, anything else = local
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row

GL_BASE_URL = os.environ.get("GL_BASE_URL", "").rstrip("/")
GL_API_KEY = os.environ.get("GL_API_KEY", "")
GL_READS = os.environ.get("GL_READS", "0") == "1"
LOCAL_DATABASE_URL = os.environ.get("DATABASE_URL", "")


def enabled() -> bool:
    return GL_READS and bool(GL_BASE_URL) and bool(GL_API_KEY)


def gl_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        GL_BASE_URL + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {GL_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as exc:
        # Surface the GL's validation message as ValueError so endpoints return 400.
        try:
            message = json.load(exc).get("error") or f"GL error {exc.code}"
        except Exception:
            message = f"GL error {exc.code}"
        raise ValueError(message)
    if isinstance(result, dict) and result.get("error"):
        raise ValueError(result["error"])
    return result


def reset_gl() -> None:
    """A dev-mode reset clears the tenant's journal via POST /v1/reset
    (chart/companies persist). The engine re-posts the opening balance and new
    entries on the next simulation tick. Best-effort."""
    if not (GL_BASE_URL and GL_API_KEY):
        return
    try:
        gl_post("/v1/reset", {})
    except Exception:
        pass


def gl_get(path: str) -> dict:
    req = urllib.request.Request(
        GL_BASE_URL + path, headers={"Authorization": f"Bearer {GL_API_KEY}"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.load(resp)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


# ===== GL ledger adapter (Stage 1 of the GL-only refactor) =====
# The engine-facing operations that replace the local cost_entries/cost_lines
# ledger: post an entry, learn which refs are already posted (idempotency), read
# an account balance, and read an account's net across one document reference
# (for relieving WIP to exactly what an order absorbed). All go over the /v1 API.

def posted_refs(company_id) -> set:
    """The event_refs already in the GL for a company — the engine's idempotency
    check (replaces `SELECT event_ref FROM cost_entries ...`)."""
    return set(gl_get(f"/v1/journal-entries/refs?company={int(company_id)}").get("refs", []))


def post_entry(company_id, ref, event_type, memo, lines, reference=None) -> bool:
    """Post one balanced entry to the GL as a journal entry. `lines` is the
    engine's (account, debit, credit) tuple list. Returns True if newly posted,
    False if the ref already existed (idempotent). Replaces post_cost_entry's
    local INSERTs."""
    result = gl_post("/v1/journal-entries", {
        "company": int(company_id), "ref": ref, "type": event_type,
        "reference": reference, "memo": memo, "source": "manufacturing",
        "lines": [{"account": account, "debit": round(debit, 2), "credit": round(credit, 2)}
                  for account, debit, credit in lines],
    })
    return not result.get("duplicate", False)


def account_balance(company_id, account_no) -> float:
    """A single account's normal-side balance from the GL."""
    data = gl_get(f"/v1/balances?company={int(company_id)}")
    for row in data.get("accounts", []):
        if row["account_no"] == account_no:
            return float(row["balance"])
    return 0.0


def reference_net(company_id, reference, account_no) -> float:
    """Net (debit - credit) of an account across every entry carrying a document
    reference — used to relieve WIP to exactly what an order absorbed, even if a
    standard changed mid-flight (replaces the cost_lines WIP read)."""
    data = gl_get(
        f"/v1/journal-entries?company={int(company_id)}"
        f"&reference={quote(str(reference))}&limit=200"
    )
    total = 0.0
    for entry in data.get("entries", []):
        for line in entry.get("lines", []):
            if line["account_no"] == account_no:
                total += float(line["debit"]) - float(line["credit"])
    return round(total, 2)


def all_entries(company_id) -> list:
    """Every journal entry for a company (keyset-paged), with lines — for the
    audit package, which tests 100% of the ledger."""
    entries, before = [], None
    while True:
        path = f"/v1/journal-entries?company={int(company_id)}&limit=200"
        if before:
            path += f"&beforeId={before}"
        data = gl_get(path)
        batch = data.get("entries", [])
        entries.extend(batch)
        if not data.get("has_more") or not batch:
            break
        before = batch[-1]["id"]
    return entries


def ledger(company_id, *, order_no=None, limit=30, before_id=None, event_type=None,
           date_from=None, date_to=None) -> dict:
    """The journal browser, served from the GL. Shapes match the local
    endpoint except the GL calls the document field `reference`; the page
    expects `order_no`, so we alias it."""
    params = [f"company={int(company_id)}", f"limit={int(limit)}"]
    if order_no:
        params.append("reference=" + quote(str(order_no)))
    if event_type:
        params.append("eventType=" + quote(str(event_type)))
    if date_from:
        params.append("dateFrom=" + quote(str(date_from)))
    if date_to:
        params.append("dateTo=" + quote(str(date_to)))
    if before_id:
        params.append(f"beforeId={int(before_id)}")
    data = gl_get("/v1/journal-entries?" + "&".join(params))
    for entry in data.get("entries", []):
        entry["order_no"] = entry.get("reference")
    return data


def analytics(company_id, group=None) -> dict:
    """The analytic slices, served from the GL. The GL calls the cost figure
    `expense` (debits to expense accounts = COGS here); the page expects
    `cogs`, so we alias it, and add the `net` total the page shows."""
    path = f"/v1/analytics?company={int(company_id)}"
    if group:
        path += "&group=" + quote(str(group))
    gl = gl_get(path)
    chosen = gl.get("group")
    totals_in = gl.get("totals", {}) or {}
    rows = [{
        "name": r["name"],
        "reference": None,
        "alloc_debit": r["alloc_debit"],
        "alloc_credit": r["alloc_credit"],
        "net": r["net"],
        "revenue": r["revenue"],
        "cogs": r["expense"],
        "margin": r["margin"],
        "lines": r["lines"],
    } for r in gl.get("rows", [])]
    totals = {
        "alloc_debit": totals_in.get("alloc_debit", 0),
        "alloc_credit": totals_in.get("alloc_credit", 0),
        "net": round(float(totals_in.get("alloc_debit", 0)) - float(totals_in.get("alloc_credit", 0)), 2),
        "revenue": totals_in.get("revenue", 0),
        "cogs": totals_in.get("expense", 0),
        "margin": totals_in.get("margin", 0),
    }
    return {
        "groups": [{"name": g["name"], "color": None} for g in gl.get("groups", [])],
        "group": chosen["name"] if chosen else None,
        "rows": rows,
        "totals": totals,
    }
