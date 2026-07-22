"""JD → prospect finder: parse a job description with Claude, search Apollo
for people at the target company in four categories, and score each person
against historical click-rate data from this database.

Env vars: ANTHROPIC_API_KEY, APOLLO_API_KEY (same values as OutreachAssistant).
People search costs no Apollo credits; nothing here spends credits.
"""
import asyncio
import os
from urllib.parse import quote_plus

import httpx
from anthropic import AsyncAnthropic, APIError
from fastapi import HTTPException

import db

CLAUDE_MODEL = "claude-haiku-4-5"  # mirrors OutreachAssistant's JD-parse choice
APOLLO_BASE = "https://api.apollo.io/api/v1"
APOLLO_TIMEOUT = httpx.Timeout(30.0)
PER_CATEGORY_RESULTS = 15

CATEGORIES = [
    ("peer", "Peers"),
    ("manager", "Hiring managers"),
    ("adjacent", "Adjacent leaders"),
    ("recruiter", "Recruiters"),
]

# Empirical-Bayes shrinkage: segment rates get pulled toward the overall mean
# with the strength of ~15 pseudo-observations, so tiny segments can't dominate.
SHRINK_K = 15
LIFT_CLAMP = (0.5, 2.0)

_anthropic = None


def _claude() -> AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(500, "ANTHROPIC_API_KEY is not set")
        _anthropic = AsyncAnthropic()
    return _anthropic


def _apollo_headers() -> dict:
    key = os.environ.get("APOLLO_API_KEY")
    if not key:
        raise HTTPException(500, "APOLLO_API_KEY is not set")
    return {"X-Api-Key": key, "Content-Type": "application/json", "accept": "application/json"}


# ---------- 1. JD parsing (forced tool call, as in OutreachAssistant) ----------

EXTRACT_TOOL = {
    "name": "extract_jd_fields",
    "description": "Extract structured info from a job description and propose Apollo search title strings for four contact categories.",
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {"type": "string", "description": "Hiring company, short brand name (e.g. 'Samsara' not 'Samsara, Inc.')."},
            "role_title": {"type": "string", "description": "The exact title of the role being hired for."},
            "department": {"type": "string", "description": "Functional department, e.g. 'Sales Engineering'."},
            "seniority": {"type": "string", "enum": ["entry", "mid", "senior", "staff", "principal", "executive"]},
            "search_titles_peer": {
                "type": "array", "items": {"type": "string"},
                "description": "5-8 LinkedIn title strings peers/ICs currently in this role would use, including realistic variants ('Solutions Engineer', 'Sales Engineer', 'Sr. Solutions Engineer').",
            },
            "search_titles_manager": {
                "type": "array", "items": {"type": "string"},
                "description": "3-5 title strings for the likely hiring manager, one level above the role ('Manager, Solutions Engineering', 'Director, Sales Engineering').",
            },
            "search_titles_adjacent": {
                "type": "array", "items": {"type": "string"},
                "description": "3-6 title strings for senior people in adjacent functions who work closely with this role and could refer or advocate — e.g. for a Solutions Engineer: 'Sales Director', 'VP Sales', 'Head of Customer Success'. Exclude recruiters and titles already in the other lists.",
            },
            "search_titles_recruiter": {
                "type": "array", "items": {"type": "string"},
                "description": "4-6 title strings for recruiters likely to staff this role, generic and role-specific ('Technical Recruiter', 'Talent Acquisition', 'GTM Recruiter').",
            },
        },
        "required": [
            "company_name", "role_title", "department", "seniority",
            "search_titles_peer", "search_titles_manager",
            "search_titles_adjacent", "search_titles_recruiter",
        ],
    },
}

SYSTEM = """You are a recruiting research assistant helping a job seeker find outreach targets. Given a job description, extract the company, role, department, and seniority, then generate realistic LinkedIn title strings for four groups at that specific company:

1. Peers / current ICs in this role
2. The probable hiring manager (one level above the role)
3. Adjacent senior leaders who work closely with the role and could refer or advocate
4. Recruiters likely to staff this role

Think about what these people would actually put on their LinkedIn profile, not generic catch-alls. Avoid duplicates across lists. Always call the extract_jd_fields tool."""


async def parse_jd(job_description: str) -> dict:
    try:
        resp = await _claude().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM,
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_jd_fields"},
            messages=[{"role": "user", "content": f"Job description:\n\n{job_description}"}],
        )
    except APIError as e:
        raise HTTPException(502, f"Claude API error: {getattr(e, 'message', str(e))}")
    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise HTTPException(502, "Claude did not return the expected tool call")
    return dict(tool_use.input)


# ---------- 2. Apollo people search (free — no credits) ----------

async def search_people(client: httpx.AsyncClient, company: str, titles: list) -> list:
    payload = {
        "q_organization_name": company,
        "person_titles": titles,
        "per_page": PER_CATEGORY_RESULTS,
        "page": 1,
    }
    try:
        r = await client.post(
            f"{APOLLO_BASE}/mixed_people/api_search",
            json=payload, headers=_apollo_headers(), timeout=APOLLO_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Apollo network error: {type(e).__name__}")
    if r.status_code >= 400:
        raise HTTPException(502, f"Apollo search failed ({r.status_code}): {r.text[:200]}")
    return (r.json().get("people") or [])


# ---------- 3. Scoring against our own outreach history ----------

_SIZE_BUCKETS = [(10, "1-10"), (50, "11-50"), (200, "51-200"),
                 (1000, "201-1000"), (5000, "1001-5000")]


def _size_bucket(size):
    if size is None:
        return "Unknown"
    for limit, label in _SIZE_BUCKETS:
        if size <= limit:
            return label
    return "5000+"


def load_segment_stats() -> dict:
    """Click-rate stats per segment, computed from the contacts DB."""
    def rates(expr):
        rows = db.query_all(
            f"""
            SELECT {expr} AS grp, count(*) AS n,
                   count(*) FILTER (WHERE v.uid IS NOT NULL) AS clicked
            FROM contacts c
            LEFT JOIN (SELECT DISTINCT uid FROM visits) v ON v.uid = c.uid
            GROUP BY 1
            """
        )
        return {r["grp"]: (r["clicked"], r["n"]) for r in rows}

    overall = db.query_one(
        """SELECT count(*) AS n, count(*) FILTER (WHERE v.uid IS NOT NULL) AS clicked
           FROM contacts c LEFT JOIN (SELECT DISTINCT uid FROM visits) v ON v.uid = c.uid"""
    )
    size_case = (
        "CASE WHEN c.company_size IS NULL THEN 'Unknown'"
        " WHEN c.company_size <= 10 THEN '1-10' WHEN c.company_size <= 50 THEN '11-50'"
        " WHEN c.company_size <= 200 THEN '51-200' WHEN c.company_size <= 1000 THEN '201-1000'"
        " WHEN c.company_size <= 5000 THEN '1001-5000' ELSE '5000+' END"
    )
    return {
        "p0": (overall["clicked"] / overall["n"]) if overall["n"] else 0.25,
        "seniority": rates("coalesce(nullif(trim(c.seniority), ''), 'Unknown')"),
        "country": rates("coalesce(nullif(trim(c.country), ''), 'Unknown')"),
        "size": rates(size_case),
        "industry": rates("coalesce(nullif(trim(lower(c.company_industry)), ''), 'Unknown')"),
    }


def _shrunk_lift(stats, table, key, p0):
    """(lift, detail) for one dimension, with sample-size shrinkage."""
    clicked, n = stats[table].get(key, (0, 0))
    rate = (clicked + SHRINK_K * p0) / (n + SHRINK_K)
    lift = max(LIFT_CLAMP[0], min(LIFT_CLAMP[1], rate / p0 if p0 else 1.0))
    return lift, {"segment": key, "n": n, "rate": round(rate, 4)}


_TITLE_SENIORITY = [
    (("chief", "cto", "ceo", "cfo", "coo", "ciso", "cro", "cmo"), "c_suite"),
    (("founder", "co-founder", "cofounder"), "founder"),
    (("vp", "vice president"), "vp"),
    (("head of", "head,"), "head"),
    (("director",), "director"),
    (("principal", "principle"), "senior"),
    (("manager", "lead"), "manager"),
    (("senior", "sr.", "sr ", "staff"), "senior"),
    (("intern",), "intern"),
    (("partner",), "partner"),
]


def infer_seniority(person: dict) -> str:
    s = (person.get("seniority") or "").strip().lower()
    if s:
        return s  # Apollo uses the same vocabulary our DB stores
    title = (person.get("title") or "").lower()
    for keywords, value in _TITLE_SENIORITY:
        if any(k in title for k in keywords):
            return value
    return "entry" if title else "Unknown"


def score_person(person: dict, stats: dict) -> dict:
    p0 = stats["p0"]
    sen = infer_seniority(person)
    country = (person.get("country") or "").strip() or "Unknown"

    sen_lift, sen_d = _shrunk_lift(stats, "seniority", sen, p0)
    ctry_lift, ctry_d = _shrunk_lift(stats, "country", country, p0)

    expected = max(0.02, min(0.95, p0 * sen_lift * ctry_lift))
    ratio = expected / p0 if p0 else 1.0
    tier = "strong" if ratio >= 1.25 else "weak" if ratio <= 0.75 else "average"
    return {
        "expected_click_rate": round(expected, 4),
        "pct": round(expected * 100),
        "tier": tier,
        "parts": [
            {"dim": "seniority", **sen_d},
            {"dim": "country", **ctry_d},
        ],
    }


def company_profile(people: list, stats: dict, company_name: str) -> dict:
    """Company-level fit, shown once. Apollo's api_search obfuscates org data
    (has_* flags only), so fall back to what our own DB knows about this
    company from previously-enriched contacts."""
    sizes = [p.get("organization", {}).get("estimated_num_employees") for p in people]
    sizes = [s for s in sizes if s]
    industries = [
        (p.get("organization", {}).get("industry") or "").strip().lower()
        for p in people
    ]
    industries = [i for i in industries if i]
    size = max(set(sizes), key=sizes.count) if sizes else None
    industry = max(set(industries), key=industries.count) if industries else None

    if size is None or industry is None:
        own = db.query_one(
            """SELECT mode() WITHIN GROUP (ORDER BY company_size) AS size,
                      mode() WITHIN GROUP (ORDER BY lower(company_industry)) AS industry
               FROM contacts WHERE lower(trim(company_name)) = lower(trim(%s))""",
            (company_name,),
        ) or {}
        size = size or own.get("size")
        industry = industry or own.get("industry")

    p0 = stats["p0"]
    bucket = _size_bucket(size)
    size_lift, size_d = _shrunk_lift(stats, "size", bucket, p0)
    ind_lift, ind_d = _shrunk_lift(stats, "industry", industry or "Unknown", p0)
    return {
        "company_size": size,
        "size_bucket": bucket,
        "industry": industry,
        "overall_click_rate": round(p0, 4),
        "size_history": size_d,
        "industry_history": ind_d,
        "fit_lift": round(size_lift * ind_lift, 2),
    }


# ---------- 4. Known-contact cross-check ----------

def known_contacts_index() -> dict:
    """Exact full-name index (works when Apollo returns unmasked names) plus
    the raw rows for fuzzy matching against obfuscated results."""
    rows = db.query_all(
        """SELECT c.uid, c.first_name, c.last_name, c.company_name, c.responded,
                  c.outcome, coalesce(v.n, 0) AS visit_count
           FROM contacts c
           LEFT JOIN (SELECT uid, count(*) AS n FROM visits GROUP BY uid) v ON v.uid = c.uid
           WHERE c.first_name IS NOT NULL"""
    )
    exact = {
        f"{(r['first_name'] or '').strip().lower()} {(r['last_name'] or '').strip().lower()}": r
        for r in rows
        if r["last_name"]
    }
    return {"exact": exact, "rows": rows}


def _obfuscated_match(last_name: str, obfuscated: str) -> bool:
    """Apollo masks last names like 'Ve***o' — match on visible prefix/suffix."""
    if not last_name or not obfuscated or "*" not in obfuscated:
        return False
    parts = obfuscated.split("*")
    prefix, suffix = parts[0].lower(), parts[-1].lower()
    l = last_name.strip().lower()
    return bool(prefix or suffix) and l.startswith(prefix) and l.endswith(suffix)


def match_known(person: dict, known: dict, company_name: str):
    first = (person.get("first_name") or "").strip().lower()
    last = (person.get("last_name") or "").strip().lower()
    if first and last:
        hit = known["exact"].get(f"{first} {last}")
        if hit:
            return hit, False
    # fuzzy: same first name at the target company, masked last name compatible
    obf = person.get("last_name_obfuscated") or ""
    company = company_name.strip().lower()
    candidates = [
        r for r in known["rows"]
        if (r["first_name"] or "").strip().lower() == first
        and company and company in (r["company_name"] or "").strip().lower()
        and (_obfuscated_match(r["last_name"] or "", obf) or not r["last_name"])
    ]
    if len(candidates) == 1:
        return candidates[0], True
    return None, False


# ---------- 5. Orchestration ----------

async def find_prospects(job_description: str) -> dict:
    parsed = await parse_jd(job_description)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(search_people(client, parsed["company_name"], parsed[f"search_titles_{key}"])
              for key, _ in CATEGORIES)
        )

    # sync psycopg2 work off the event loop
    from starlette.concurrency import run_in_threadpool

    stats = await run_in_threadpool(load_segment_stats)
    known = await run_in_threadpool(known_contacts_index)

    company = parsed["company_name"]
    all_people = [p for group in results for p in group]
    profile = await run_in_threadpool(company_profile, all_people, stats, company)

    seen_ids = set()
    categories = []
    for (key, label), people in zip(CATEGORIES, results):
        cards = []
        for p in people:
            pid = p.get("id")
            if pid in seen_ids:
                continue  # cross-category dedupe: first category wins
            seen_ids.add(pid)
            match, fuzzy = match_known(p, known, company)
            cards.append({
                "id": pid,
                "name": _display_name(p),
                "title": p.get("title"),
                "linkedin_url": p.get("linkedin_url"),
                "linkedin_search_url": _linkedin_search_url(p, company),
                "country": p.get("country"),
                "seniority": infer_seniority(p),
                "score": score_person(p, stats),
                "known": {
                    "uid": match["uid"],
                    "name": f"{match['first_name']} {match['last_name'] or ''}".strip(),
                    "fuzzy": fuzzy,
                    "clicked": match["visit_count"] > 0,
                    "responded": match["responded"] is True,
                    "outcome": match["outcome"],
                } if match else None,
            })
        cards.sort(key=lambda c: c["score"]["expected_click_rate"], reverse=True)
        categories.append({"key": key, "label": label, "people": cards})

    return {"parsed": parsed, "company_profile": profile, "categories": categories}


def _display_name(p: dict) -> str:
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or "").strip()
    if last:
        return f"{first} {last}".strip()
    obf = p.get("last_name_obfuscated") or ""
    prefix = obf.split("*")[0]
    if prefix:
        return f"{first} {prefix}."
    return p.get("name") or first or "Unknown"


def _linkedin_search_url(p: dict, company: str) -> str:
    """Free fallback when Apollo hides the profile URL: a LinkedIn people
    search for this person's first name + title + company."""
    terms = " ".join(x for x in [(p.get("first_name") or "").strip(),
                                 (p.get("title") or "").strip(), company] if x)
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(terms)}"
