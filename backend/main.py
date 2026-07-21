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


@app.get("/api/contacts", dependencies=protected)
def contacts():
    return {"contacts": queries.list_contacts()}


# ---------- the only write path ----------

class ContactUpdate(BaseModel):
    responded: bool | None = None
    outcome: str | None = None
    responded_at: datetime | None = None


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
