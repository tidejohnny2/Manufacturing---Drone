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
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.load(resp)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    return result


def post_pending() -> None:
    """Post every local ledger entry the GL doesn't yet hold as a real balanced
    journal entry via POST /v1/journal-entries (idempotent by ref, so retries
    and overlaps are safe). This is how Manufacturing writes to the GL. The
    analytic tag overlay is a separate report-only layer synced by the GL-side
    mirror, not part of the journal, so it isn't posted here. Raises on failure
    so read-time callers fall back to the local ledger; the write path uses
    push_best_effort() which swallows."""
    if not enabled() or not LOCAL_DATABASE_URL:
        return
    with psycopg.connect(LOCAL_DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT company_id FROM cost_entries")
            for cid in sorted(r["company_id"] for r in cur.fetchall()):
                have = set(gl_get(f"/v1/journal-entries/refs?company={cid}").get("refs", []))
                cur.execute(
                    "SELECT id, event_ref, event_type, order_no, memo FROM cost_entries "
                    "WHERE company_id = %s ORDER BY id",
                    (cid,),
                )
                pending = [e for e in cur.fetchall() if e["event_ref"] not in have]
                for entry in pending:
                    cur.execute(
                        "SELECT account_no, debit, credit FROM cost_lines "
                        "WHERE entry_id = %s ORDER BY id",
                        (entry["id"],),
                    )
                    lines = [
                        {"account": l["account_no"], "debit": float(l["debit"]), "credit": float(l["credit"])}
                        for l in cur.fetchall()
                    ]
                    gl_post("/v1/journal-entries", {
                        "company": cid, "ref": entry["event_ref"], "type": entry["event_type"],
                        "reference": entry["order_no"], "memo": entry["memo"],
                        "source": "manufacturing", "lines": lines,
                    })


def sync_gl_now() -> None:
    """Post pending local entries to the GL now so a GL-backed read is fresh.
    Raises on failure so the caller falls back to the local ledger."""
    post_pending()


def push_best_effort() -> None:
    """Dual-write: after a local ledger write, post it to the GL right away.
    Never raises — the local write already succeeded and the next read/worker
    cycle catches up. Only runs when reads are on the GL."""
    if not enabled():
        return
    try:
        post_pending()
    except Exception:
        pass


def reset_gl() -> None:
    """Reflect a dev-mode local purge in the GL: clear the tenant's journal via
    POST /v1/reset (chart/companies persist), then re-post the fresh source. A
    purge can't be conveyed by posting, so the clear goes whenever the GL is
    configured. Best-effort."""
    if not (GL_BASE_URL and GL_API_KEY):
        return
    try:
        gl_post("/v1/reset", {})
        if enabled():
            post_pending()
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
