"""GL-backed reads (Phase 3).

When GL_READS is on, the accounting reporting pages are served from the shared
onadapt-gl service over its /v1 API instead of the local ledger. Every call
falls back to the local function on any error, so a GL hiccup never breaks a
page. Writes still go to the local ledger; the mirror timer
(onadapt-gl-mirror) keeps the GL current within ~60s.

Config comes from the environment (set in drones.env):
    GL_BASE_URL   e.g. http://127.0.0.1:8798
    GL_API_KEY    the manufacturing tenant's bearer key
    GL_READS      "1" to serve reads from the GL, anything else = local
"""
import json
import os
import subprocess
import urllib.request
from urllib.parse import quote

GL_BASE_URL = os.environ.get("GL_BASE_URL", "").rstrip("/")
GL_API_KEY = os.environ.get("GL_API_KEY", "")
GL_READS = os.environ.get("GL_READS", "0") == "1"

# Inline read-time sync (Phase 3b): push new local postings into the GL right
# before a GL-backed read so its balances are current. Reuses the onadapt-gl
# mirror in --push-only mode.
GL_DATABASE_URL = os.environ.get("GL_DATABASE_URL", "")
LOCAL_DATABASE_URL = os.environ.get("DATABASE_URL", "")
GL_MIRROR_PYTHON = os.environ.get("GL_MIRROR_PYTHON", "/opt/onadapt-gl/venv/bin/python")
GL_MIRROR_SCRIPT = os.environ.get("GL_MIRROR_SCRIPT", "/opt/onadapt-gl/mirror.py")


def enabled() -> bool:
    return GL_READS and bool(GL_BASE_URL) and bool(GL_API_KEY)


def sync_gl_now() -> None:
    """Push new local postings into the GL now so a GL-backed read is fresh.
    Runs the onadapt-gl mirror (--push-only). Raises on failure so the caller
    falls back to the local ledger rather than showing controls against a stale
    GL."""
    if not (GL_DATABASE_URL and LOCAL_DATABASE_URL and os.path.exists(GL_MIRROR_SCRIPT)):
        raise RuntimeError("GL inline sync is not configured")
    env = dict(os.environ)
    env["SOURCE_DATABASE_URL"] = LOCAL_DATABASE_URL
    env["DATABASE_URL"] = GL_DATABASE_URL
    subprocess.run(
        [GL_MIRROR_PYTHON, GL_MIRROR_SCRIPT, "--push-only"],
        env=env, timeout=20, capture_output=True, check=True,
    )


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
