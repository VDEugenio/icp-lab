"""Analytics SQL. Read-only except update_contact.

Per project decisions: every row in contacts counts in denominators (no
filtering of manual links or never-contacted rows), and visits are counted
raw (no self-click filtering). NULL dimension values become 'Unknown'.
"""
from datetime import datetime

import db

# Subquery joined everywhere a "clicked" flag is needed.
_VISITS_JOIN = """
LEFT JOIN (
    SELECT uid, count(*) AS visit_count, max(visited_at) AS last_visit
    FROM visits
    WHERE kind = 'human'
    GROUP BY uid
) v ON v.uid = c.uid
"""

_SIZE_BUCKET = """
CASE
    WHEN c.company_size IS NULL THEN 'Unknown'
    WHEN c.company_size <= 10 THEN '1-10'
    WHEN c.company_size <= 50 THEN '11-50'
    WHEN c.company_size <= 200 THEN '51-200'
    WHEN c.company_size <= 1000 THEN '201-1000'
    WHEN c.company_size <= 5000 THEN '1001-5000'
    ELSE '5000+'
END
"""

def _text_dim(col):
    return f"coalesce(nullif(trim(c.{col}), ''), 'Unknown')"

# Whitelist: query-param value -> SQL grouping expression. Never interpolate
# user input into SQL outside this mapping.
DIMENSIONS = {
    "seniority": _text_dim("seniority"),
    "company_size": _SIZE_BUCKET,
    "industry": _text_dim("company_industry"),
    "connection_degree": _text_dim("connection_degree"),
    "country": _text_dim("country"),
    "target_role": _text_dim("target_role"),
    "premium": (
        "CASE WHEN c.premium IS TRUE THEN 'Premium'"
        " WHEN c.premium IS FALSE THEN 'Not premium' ELSE 'Unknown' END"
    ),
    "channel": (
        "CASE WHEN c.channel = 'copy' THEN 'LinkedIn DM'"
        " WHEN c.channel = 'email' THEN 'Email'"
        " ELSE coalesce(nullif(trim(c.channel), ''), 'Unknown') END"
    ),
}

OUTCOMES = {"call", "referral", "ghost", "rejected", "other"}

# Columns the icp_lab DB role may UPDATE for manual enrichment (matches the
# column-level grants; uid/created_at/apollo_raw/linkedin_url are excluded
# both here and at the DB level).
ENRICH_COLUMNS = {
    "first_name", "last_name", "title", "seniority", "departments",
    "company_name", "company_size", "company_industry",
    "city", "state", "country", "years_at_company", "email_status",
    "premium", "follower_count", "connection_degree",
    "target_role", "target_company", "channel", "contacted_at",
}

_RETURN_COLS = (
    "uid, responded, responded_at, outcome, first_name, last_name, title, "
    "seniority, departments, company_name, company_size, company_industry, "
    "city, state, country, years_at_company, premium, follower_count, "
    "connection_degree, target_role, target_company, channel, contacted_at"
)

_COUNTS = """
    count(*) AS contacted,
    count(*) FILTER (WHERE v.visit_count > 0) AS clicked,
    count(*) FILTER (WHERE c.responded IS TRUE) AS responded
"""


def headline_stats():
    overall = db.query_one(f"SELECT {_COUNTS} FROM contacts c {_VISITS_JOIN}")
    by_channel = db.query_all(
        f"""
        SELECT {DIMENSIONS['channel']} AS channel, {_COUNTS}
        FROM contacts c {_VISITS_JOIN}
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    return {"overall": overall, "by_channel": by_channel}


def breakdown(dim: str):
    expr = DIMENSIONS[dim]  # caller validates membership
    return db.query_all(
        f"""
        SELECT {expr} AS grp, {_COUNTS}
        FROM contacts c {_VISITS_JOIN}
        GROUP BY 1 ORDER BY contacted DESC, grp
        """
    )


def timeseries(granularity: str):
    assert granularity in ("week", "month")
    return db.query_all(
        f"""
        SELECT date_trunc(%s, c.contacted_at) AS period, {_COUNTS}
        FROM contacts c {_VISITS_JOIN}
        WHERE c.contacted_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        (granularity,),
    )


def icp(dims: list, min_n: int, metric: str):
    """Group by a combination of whitelisted dimensions, drop groups under
    min_n, rank by click or response rate. dims/metric validated by caller."""
    exprs = [DIMENSIONS[d] for d in dims]
    select_cols = ", ".join(f"{e} AS {d}" for d, e in zip(dims, exprs))
    group_nums = ", ".join(str(i + 1) for i in range(len(dims)))
    rate = {
        "click": "count(*) FILTER (WHERE v.visit_count > 0)::float / count(*)",
        "response": "count(*) FILTER (WHERE c.responded IS TRUE)::float / count(*)",
    }[metric]
    return db.query_all(
        f"""
        SELECT {select_cols}, {_COUNTS}
        FROM contacts c {_VISITS_JOIN}
        GROUP BY {group_nums}
        HAVING count(*) >= %s
        ORDER BY {rate} DESC, count(*) DESC
        LIMIT 50
        """,
        (min_n,),
    )


def list_contacts():
    return db.query_all(
        f"""
        SELECT c.uid, c.first_name, c.last_name, c.linkedin_url, c.title,
               c.seniority, c.departments, c.company_name, c.company_size,
               c.company_industry, c.connection_degree, c.city, c.state,
               c.country, c.years_at_company, c.target_role,
               c.target_company, c.channel, c.premium, c.follower_count,
               c.created_at, c.contacted_at, c.responded, c.responded_at,
               c.outcome,
               coalesce(v.visit_count, 0) AS visit_count, v.last_visit
        FROM contacts c {_VISITS_JOIN}
        ORDER BY c.contacted_at DESC NULLS LAST, c.created_at DESC NULLS LAST
        """
    )


def update_contact(uid: str, fields: dict) -> dict | None:
    """fields: outcome-recording columns and/or ENRICH_COLUMNS, already
    validated by the API layer. Returns the updated row, or None if uid
    doesn't exist."""
    allowed = {"responded", "outcome", "responded_at"} | ENRICH_COLUMNS
    assert set(fields) <= allowed, f"unexpected fields: {set(fields) - allowed}"
    if not fields:
        return get_contact(uid)
    sets = ", ".join(f"{col} = %s" for col in fields)
    row = db.query_one(
        f"UPDATE contacts c SET {sets} WHERE uid = %s RETURNING {_RETURN_COLS}",
        (*fields.values(), uid),
    )
    return row


def get_contact(uid: str) -> dict | None:
    return db.query_one(
        f"SELECT {_RETURN_COLS} FROM contacts WHERE uid = %s",
        (uid,),
    )


def enrich_meta():
    """Suggestion data for the enrichment form: known orgs (with their most
    common size/industry for autofill) and distinct values for datalists."""
    orgs = db.query_all(
        """
        SELECT company_name,
               mode() WITHIN GROUP (ORDER BY company_size) AS company_size,
               mode() WITHIN GROUP (ORDER BY company_industry) AS company_industry,
               count(*) AS n
        FROM contacts
        WHERE nullif(trim(company_name), '') IS NOT NULL
        GROUP BY company_name
        ORDER BY count(*) DESC, company_name
        """
    )

    def distinct(col):  # col is code-controlled, never user input
        return [
            r["v"]
            for r in db.query_all(
                f"SELECT DISTINCT nullif(trim({col}), '') AS v FROM contacts"
                f" WHERE nullif(trim({col}), '') IS NOT NULL ORDER BY 1"
            )
        ]

    return {
        "orgs": orgs,
        "seniorities": distinct("seniority"),
        "industries": distinct("company_industry"),
        "countries": distinct("country"),
        "titles": distinct("title"),
    }
