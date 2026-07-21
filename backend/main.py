"""icp-lab: analytics dashboard over the outreach database.

Run locally:  uvicorn backend.main:app --reload  (from the repo root)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # local dev; on Railway env vars come from the dashboard

sys.path.insert(0, str(Path(__file__).parent))  # so sibling imports work

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import queries

app = FastAPI(title="icp-lab", docs_url=None, redoc_url=None, openapi_url=None)

FRONTEND = Path(__file__).parent.parent / "frontend"


# ---------- pages ----------

@app.get("/", include_in_schema=False)
def index(request: Request):
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(FRONTEND / "index.html")


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if auth.is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(FRONTEND / "login.html")


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}


# ---------- auth ----------

class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    if not auth.check_login(body.password):
        raise HTTPException(401, "Wrong password")
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.create_session_token(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        secure=auth.cookie_secure(),
        samesite="lax",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


# ---------- read APIs ----------

protected = [Depends(auth.require_auth)]


def _rate(n, d):
    return round(n / d, 4) if d else None


def _with_rates(row):
    row = dict(row)
    row["click_rate"] = _rate(row["clicked"], row["contacted"])
    row["response_rate"] = _rate(row["responded"], row["contacted"])
    return row


@app.get("/api/stats", dependencies=protected)
def stats():
    data = queries.headline_stats()
    return {
        "overall": _with_rates(data["overall"]),
        "by_channel": [_with_rates(r) for r in data["by_channel"]],
    }


@app.get("/api/breakdown", dependencies=protected)
def breakdown(dim: str):
    if dim not in queries.DIMENSIONS:
        raise HTTPException(400, f"Unknown dimension. One of: {sorted(queries.DIMENSIONS)}")
    return {"dim": dim, "groups": [_with_rates(r) for r in queries.breakdown(dim)]}


@app.get("/api/timeseries", dependencies=protected)
def timeseries(granularity: str = "week"):
    if granularity not in ("week", "month"):
        raise HTTPException(400, "granularity must be 'week' or 'month'")
    return {
        "granularity": granularity,
        "periods": [_with_rates(r) for r in queries.timeseries(granularity)],
    }


@app.get("/api/icp", dependencies=protected)
def icp(dims: str, min_n: int = 8, metric: str = "click"):
    dim_list = list(dict.fromkeys(d for d in dims.split(",") if d))  # dedupe, keep order
    bad = [d for d in dim_list if d not in queries.DIMENSIONS]
    if not dim_list or bad:
        raise HTTPException(400, f"dims must be a comma list from: {sorted(queries.DIMENSIONS)}")
    if metric not in ("click", "response"):
        raise HTTPException(400, "metric must be 'click' or 'response'")
    min_n = max(1, min(min_n, 10000))
    rows = queries.icp(dim_list, min_n, metric)
    return {
        "dims": dim_list,
        "min_n": min_n,
        "metric": metric,
        "groups": [_with_rates(r) for r in rows],
    }


@app.get("/api/contacts", dependencies=protected)
def contacts():
    return {"contacts": queries.list_contacts()}


@app.get("/api/enrich-meta", dependencies=protected)
def enrich_meta():
    return queries.enrich_meta()


# ---------- the only write path ----------

class ContactUpdate(BaseModel):
    responded: bool | None = None
    outcome: str | None = None
    responded_at: datetime | None = None
    # manual enrichment fields (must stay within queries.ENRICH_COLUMNS)
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    seniority: str | None = None
    departments: str | None = None
    company_name: str | None = None
    company_size: int | None = None
    company_industry: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    years_at_company: float | None = None
    email_status: str | None = None
    premium: bool | None = None
    follower_count: int | None = None
    connection_degree: str | None = None
    target_role: str | None = None
    target_company: str | None = None
    channel: str | None = None
    contacted_at: datetime | None = None


ENRICH_ENUMS = {
    "connection_degree": {"1st", "2nd", "3rd"},
    "channel": {"copy", "email"},
}


@app.patch("/api/contacts/{uid}", dependencies=protected)
def update_contact(uid: str, body: ContactUpdate):
    current = queries.get_contact(uid)
    if current is None:
        raise HTTPException(404, "No such contact")

    provided = body.model_fields_set
    fields = {}

    if "outcome" in provided:
        if body.outcome is not None and body.outcome not in queries.OUTCOMES:
            raise HTTPException(400, f"outcome must be one of {sorted(queries.OUTCOMES)} or null")
        fields["outcome"] = body.outcome

    for name in sorted(provided & queries.ENRICH_COLUMNS - {"contacted_at"}):
        val = getattr(body, name)
        if isinstance(val, str):
            val = val.strip() or None
        if val is not None and name in ENRICH_ENUMS and val not in ENRICH_ENUMS[name]:
            raise HTTPException(400, f"{name} must be one of {sorted(ENRICH_ENUMS[name])} or null")
        fields[name] = val
    if "contacted_at" in provided:
        fields["contacted_at"] = body.contacted_at

    if "responded" in provided:
        fields["responded"] = body.responded
        if body.responded:
            if "responded_at" in provided:
                fields["responded_at"] = body.responded_at
            elif not current["responded"]:
                fields["responded_at"] = datetime.now(timezone.utc)
        else:
            # flipping off clears the timestamp
            fields["responded_at"] = None
    elif "responded_at" in provided:
        # date-only edit (backfilling the real response date)
        fields["responded_at"] = body.responded_at

    updated = queries.update_contact(uid, fields)
    return {"contact": updated}


# ---------- static assets (css/js only; pages are auth-gated above) ----------

app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")
