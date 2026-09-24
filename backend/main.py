"""icp-lab: analytics dashboard over the outreach database.

Run locally:  uvicorn backend.main:app --reload  (from the repo root)
"""
import os
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
from starlette.concurrency import run_in_threadpool

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
    # rev: Railway injects the deployed commit sha; "dev" locally
    return {"ok": True, "rev": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "dev")[:12]}


# ---------- auth ----------

class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    user = auth.check_login(body.username, body.password)
    if user is None:
        raise HTTPException(401, "Wrong username or password")
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.create_session_token(user["id"]),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        secure=auth.cookie_secure(),
        samesite="lax",
    )
    return {"ok": True, "username": user["username"]}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


protected = [Depends(auth.require_auth)]
admin_only = [Depends(auth.require_admin)]  # Gmail reply scanner + user admin
spend = [Depends(auth.require_spend)]  # Claude / Apollo calls (per-user toggle)


def _uid(request: Request) -> int:
    """Id of the signed-in user (routes are already auth-gated)."""
    return auth.current_user(request)["id"]


@app.get("/api/me", dependencies=protected)
def me(request: Request):
    u = auth.current_user(request)
    return {"username": u["username"], "is_admin": u["is_admin"], "can_spend": u["can_spend"]}


# ---------- read APIs ----------


def _scope(scope: str) -> str:
    if scope not in queries.SCOPES:
        raise HTTPException(400, f"scope must be one of {sorted(queries.SCOPES)}")
    return scope


def _rate(n, d):
    return round(n / d, 4) if d else None


def _with_rates(row):
    row = dict(row)
    row["click_rate"] = _rate(row["clicked"], row["contacted"])
    row["response_rate"] = _rate(row["responded"], row["contacted"])
    return row


@app.get("/api/stats", dependencies=protected)
def stats(scope: str = "all"):
    data = queries.headline_stats(_scope(scope))
    return {
        "overall": _with_rates(data["overall"]),
        "by_channel": [_with_rates(r) for r in data["by_channel"]],
    }


@app.get("/api/breakdown", dependencies=protected)
def breakdown(dim: str, scope: str = "all"):
    if dim not in queries.DIMENSIONS:
        raise HTTPException(400, f"Unknown dimension. One of: {sorted(queries.DIMENSIONS)}")
    return {"dim": dim, "groups": [_with_rates(r) for r in queries.breakdown(dim, _scope(scope))]}


@app.get("/api/timeseries", dependencies=protected)
def timeseries(granularity: str = "week", scope: str = "all"):
    if granularity not in ("week", "month"):
        raise HTTPException(400, "granularity must be 'week' or 'month'")
    return {
        "granularity": granularity,
        "periods": [_with_rates(r) for r in queries.timeseries(granularity, _scope(scope))],
    }


@app.get("/api/icp", dependencies=protected)
def icp(dims: str, min_n: int = 8, metric: str = "click", scope: str = "all"):
    dim_list = list(dict.fromkeys(d for d in dims.split(",") if d))  # dedupe, keep order
    bad = [d for d in dim_list if d not in queries.DIMENSIONS]
    if not dim_list or bad:
        raise HTTPException(400, f"dims must be a comma list from: {sorted(queries.DIMENSIONS)}")
    if metric not in ("click", "response"):
        raise HTTPException(400, "metric must be 'click' or 'response'")
    min_n = max(1, min(min_n, 10000))
    rows = queries.icp(dim_list, min_n, metric, _scope(scope))
    return {
        "dims": dim_list,
        "min_n": min_n,
        "metric": metric,
        "groups": [_with_rates(r) for r in rows],
    }


@app.get("/api/contacts", dependencies=protected)
def contacts(scope: str = "all"):
    return {"contacts": queries.list_contacts(_scope(scope))}


@app.get("/api/enrich-meta", dependencies=protected)
def enrich_meta():
    return queries.enrich_meta()


class JDSearchBody(BaseModel):
    job_description: str
    per_category: int = 15


def _log_search_usage(user_id: int, usage: dict, endpoint: str):
    """usage: {"claude": {...} | None, "apollo_searches": n} from the finders."""
    if usage.get("claude"):
        queries.log_usage(user_id, "claude", detail={"endpoint": endpoint}, **usage["claude"])
    if usage.get("apollo_searches"):
        queries.log_usage(user_id, "apollo_search",
                          detail={"endpoint": endpoint, "calls": usage["apollo_searches"]})


@app.post("/api/jd-search", dependencies=spend)
async def jd_search(body: JDSearchBody, request: Request):
    if not body.job_description.strip():
        raise HTTPException(400, "job_description is empty")
    import jd_finder

    per_category = max(1, min(25, body.per_category))
    result = await jd_finder.find_prospects(body.job_description, per_category)
    await run_in_threadpool(_log_search_usage, _uid(request), result.pop("usage"), "jd-search")
    return result


class RevealBody(BaseModel):
    id: str


@app.post("/api/prospect-reveal", dependencies=spend)
async def prospect_reveal(body: RevealBody, request: Request):
    """Spends 1 Apollo credit. Only triggered by the explicit Reveal button."""
    import jd_finder

    result = await jd_finder.reveal_person(body.id)  # raises on failure → not logged
    await run_in_threadpool(
        queries.log_usage, _uid(request), "apollo_reveal", credits=1,
        detail={"apollo_id": body.id, "name": result.get("name")},
    )
    return result


# ---------- work tab (persona → Apollo people search, no company scope) ----------

# Defaults returned until the user saves their own settings (and whenever the
# app_settings table hasn't been created yet — see docs/architecture.md).
WORK_DEFAULTS = {
    "persona": "",
    "locations": ["United States", "Canada"],
    "exclusions": [],
    "template": (
        "Hi {first_name}!\n\n"
        "I'm curious what a broker's day-to-day actually looks like, especially "
        "around comparing quotes. I'm building a tool in this space and would "
        "love insight into your workflow. Any chance you'd have time for a "
        "quick call this week?\n\n"
        "Thanks,\nVaughn"
    ),
}


def _work_settings_key(request: Request) -> str:
    return f"work:{_uid(request)}"


@app.get("/api/work-settings", dependencies=protected)
def work_settings(request: Request):
    # Per-user; the admin falls back to the pre-multi-user shared "work" row.
    stored = queries.get_setting(_work_settings_key(request))
    if stored is None and auth.current_user(request)["is_admin"]:
        stored = queries.get_setting("work")
    stored = stored or {}
    return {
        **WORK_DEFAULTS,
        **{k: v for k, v in stored.items() if k in WORK_DEFAULTS},
        "table_ready": queries.settings_table_ready(),
    }


class WorkSettingsBody(BaseModel):
    persona: str = ""
    locations: list[str] = []
    exclusions: list[str] = []
    template: str = ""


@app.put("/api/work-settings", dependencies=protected)
def save_work_settings(body: WorkSettingsBody, request: Request):
    if not queries.settings_table_ready():
        raise HTTPException(503, "app_settings table missing — run the one-time SQL in docs/architecture.md")
    value = {
        "persona": body.persona.strip(),
        "locations": [l.strip() for l in body.locations if l.strip()],
        "exclusions": [e.strip() for e in body.exclusions if e.strip()],
        "template": body.template,
    }
    queries.set_setting(_work_settings_key(request), value, _uid(request))
    return {"ok": True, **value}


class WorkSearchBody(BaseModel):
    persona: str
    locations: list[str] = []
    exclusions: list[str] = []
    per_page: int = 25
    page: int = 1
    titles: list[str] | None = None  # Load more passes the parsed titles back


@app.post("/api/work-search", dependencies=spend)
async def work_search(body: WorkSearchBody, request: Request):
    if not body.persona.strip() and not body.titles:
        raise HTTPException(400, "persona is empty")
    import work_finder

    result = await work_finder.search(
        body.persona,
        [l.strip() for l in body.locations if l.strip()],
        body.exclusions,
        per_page=max(1, min(work_finder.MAX_PER_PAGE, body.per_page)),
        page=max(1, min(500, body.page)),
        titles=body.titles or None,
    )
    await run_in_threadpool(_log_search_usage, _uid(request), result.pop("usage"), "work-search")
    return result


# ---------- reply scanner (Gmail LinkedIn notifications → responded) ----------

import replies


@app.get("/api/replies", dependencies=protected)
def replies_status(request: Request):
    # Non-admins never see the reply scanner: report "unconfigured" so the
    # frontend hides the card entirely (it contains Gmail snippets).
    if not auth.current_user(request)["is_admin"]:
        return {"configured": False}
    return replies.status()


class ScanBody(BaseModel):
    days: int = 30
    auto: bool = False  # page-load scans are throttled server-side


@app.post("/api/replies/scan", dependencies=admin_only)
def replies_scan(body: ScanBody, request: Request):
    try:
        return replies.scan(_uid(request), days=max(1, min(body.days, 365)), auto=body.auto)
    except replies.ScanError as e:
        raise HTTPException(503, str(e))


class ReplyConfirmBody(BaseModel):
    uid: str


@app.post("/api/replies/{gmail_id}/confirm", dependencies=admin_only)
def reply_confirm(gmail_id: str, body: ReplyConfirmBody, request: Request):
    try:
        return replies.confirm(gmail_id, body.uid, _uid(request))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/replies/{gmail_id}/dismiss", dependencies=admin_only)
def reply_dismiss(gmail_id: str):
    try:
        return replies.dismiss(gmail_id)
    except LookupError as e:
        raise HTTPException(404, str(e))


# ---------- outreach-backend proxies (contact creation + tracking links) ----------
# Contact creation always goes through outreach-backend so UID generation
# stays in one place — icp-lab never inserts contacts itself.

import os as _os

import httpx as _httpx

OUTREACH_BASE = _os.environ.get(
    "OUTREACH_BACKEND_URL", "https://outreach-backend-production-326e.up.railway.app"
)


class OutreachContactBody(BaseModel):
    first_name: str
    last_name: str | None = None
    linkedin_url: str | None = None
    target_role: str | None = None
    target_company: str | None = None
    purpose: str | None = None  # 'work' from the Work tab; omitted = job search


@app.post("/api/outreach-contact", dependencies=protected)
async def outreach_contact(body: OutreachContactBody, request: Request):
    async with _httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(f"{OUTREACH_BASE}/contacts", json=body.model_dump())
        except _httpx.HTTPError as e:
            raise HTTPException(502, f"outreach-backend unreachable: {type(e).__name__}")
    if r.status_code >= 400:
        raise HTTPException(502, f"outreach-backend error ({r.status_code}): {r.text[:200]}")
    data = r.json()
    await run_in_threadpool(queries.stamp_contact, data["uid"], _uid(request), True)
    return {"uid": data["uid"], "tracking_url": data["tracking_url"]}


class OutreachContactedBody(BaseModel):
    uid: str


@app.post("/api/outreach-contacted", dependencies=protected)
async def outreach_contacted(body: OutreachContactedBody, request: Request):
    async with _httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(
                f"{OUTREACH_BASE}/contacts/{body.uid}/contacted", json={"channel": "copy"}
            )
        except _httpx.HTTPError as e:
            raise HTTPException(502, f"outreach-backend unreachable: {type(e).__name__}")
    if r.status_code >= 400:
        raise HTTPException(502, f"outreach-backend error ({r.status_code}): {r.text[:200]}")
    await run_in_threadpool(queries.stamp_contact, body.uid, _uid(request))
    return {"ok": True}


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
    purpose: str | None = None  # null = job search, 'work' = work outreach


ENRICH_ENUMS = {
    "connection_degree": {"1st", "2nd", "3rd"},
    "channel": {"copy", "email"},
    "purpose": {"work"},
}


@app.patch("/api/contacts/{uid}", dependencies=protected)
def update_contact(uid: str, body: ContactUpdate, request: Request):
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

    user = auth.current_user(request)
    updated = queries.update_contact(uid, fields, user["id"])
    if updated is not None and fields:
        updated["updated_by_name"] = user["username"]
    return {"contact": updated}


# ---------- admin: user accounts + per-user usage ----------

import re as _re

USERNAME_RE = _re.compile(r"^[a-z0-9._-]{3,32}$")
MIN_PASSWORD_LEN = 10

# Claude Haiku 4.5 list pricing (USD per million tokens) — for the admin
# page's estimated-cost column only; not billing.
CLAUDE_PRICE_PER_MTOK = {"input": 1.00, "output": 5.00}


def _check_password(pw: str):
    if len(pw) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"password must be at least {MIN_PASSWORD_LEN} characters")


@app.get("/api/admin/users", dependencies=admin_only)
def admin_users():
    return {"users": queries.list_users()}


class NewUserBody(BaseModel):
    username: str
    password: str
    can_spend: bool = False


@app.post("/api/admin/users", dependencies=admin_only)
def admin_create_user(body: NewUserBody, request: Request):
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "username must be 3-32 chars: a-z, 0-9, dot, dash, underscore")
    _check_password(body.password)
    new_id = queries.create_user(
        username, auth.hash_password(body.password), body.can_spend, _uid(request)
    )
    if new_id is None:
        raise HTTPException(409, f"username '{username}' is taken")
    return {"user": queries.get_user(new_id)}


class UserUpdateBody(BaseModel):
    can_spend: bool | None = None
    disabled: bool | None = None
    password: str | None = None


@app.patch("/api/admin/users/{user_id}", dependencies=admin_only)
def admin_update_user(user_id: int, body: UserUpdateBody, request: Request):
    target = queries.get_user(user_id)
    if target is None:
        raise HTTPException(404, "No such user")
    provided = body.model_fields_set
    if user_id == _uid(request) and body.disabled:
        raise HTTPException(400, "You can't disable your own account")
    fields = {}
    if "can_spend" in provided and body.can_spend is not None:
        fields["can_spend"] = body.can_spend
    if "disabled" in provided and body.disabled is not None:
        fields["disabled_at"] = datetime.now(timezone.utc) if body.disabled else None
    if "password" in provided and body.password is not None:
        _check_password(body.password)
        fields["password_hash"] = auth.hash_password(body.password)
    queries.update_user(user_id, fields)
    return {"user": queries.get_user(user_id)}


@app.get("/api/admin/usage", dependencies=admin_only)
def admin_usage(days: int = 30):
    """days <= 0 means all time."""
    days = min(days, 3650)
    data = queries.usage_summary(days if days > 0 else None)
    for row in data["totals"]:
        row["claude_cost_usd"] = round(
            row["input_tokens"] / 1e6 * CLAUDE_PRICE_PER_MTOK["input"]
            + row["output_tokens"] / 1e6 * CLAUDE_PRICE_PER_MTOK["output"],
            4,
        )
    return {"days": days if days > 0 else None, **data}


# ---------- static assets (css/js only; pages are auth-gated above) ----------

app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")
