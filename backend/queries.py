"""Analytics SQL. Read-only except update_contact, the attribution stamps,
and icp-lab's own tables (app_settings, users, usage_events).

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

# Scope: which world a query looks at. NULL purpose = job-search rows (all
# history predates the column); 'work' = work-outreach rows from the Work tab.
SCOPES = {
    "all": "TRUE",
    "job": "c.purpose IS DISTINCT FROM 'work'",
    "work": "c.purpose = 'work'",
}

# Columns the icp_lab DB role may UPDATE for manual enrichment (matches the
# column-level grants; uid/created_at/apollo_raw/linkedin_url are excluded
# both here and at the DB level).
ENRICH_COLUMNS = {
    "first_name", "last_name", "title", "seniority", "departments",
    "company_name", "company_size", "company_industry",
    "city", "state", "country", "years_at_company", "email_status",
    "premium", "follower_count", "connection_degree",
    "target_role", "target_company", "channel", "contacted_at", "purpose",
}

_RETURN_COLS = (
    "uid, responded, responded_at, outcome, first_name, last_name, title, "
    "seniority, departments, company_name, company_size, company_industry, "
    "city, state, country, years_at_company, premium, follower_count, "
    "connection_degree, target_role, target_company, channel, contacted_at, "
    "purpose, created_by, updated_by, updated_at"
)

_COUNTS = """
    count(*) AS contacted,
    count(*) FILTER (WHERE v.visit_count > 0) AS clicked,
    count(*) FILTER (WHERE c.responded IS TRUE) AS responded
"""


def headline_stats(scope: str = "all"):
    where = SCOPES[scope]  # caller validates membership
    overall = db.query_one(f"SELECT {_COUNTS} FROM contacts c {_VISITS_JOIN} WHERE {where}")
    by_channel = db.query_all(
        f"""
        SELECT {DIMENSIONS['channel']} AS channel, {_COUNTS}
        FROM contacts c {_VISITS_JOIN}
        WHERE {where}
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    return {"overall": overall, "by_channel": by_channel}


def breakdown(dim: str, scope: str = "all"):
    expr = DIMENSIONS[dim]  # caller validates membership (scope too)
    return db.query_all(
        f"""
        SELECT {expr} AS grp, {_COUNTS}
        FROM contacts c {_VISITS_JOIN}
        WHERE {SCOPES[scope]}
        GROUP BY 1 ORDER BY contacted DESC, grp
        """
    )


def timeseries(granularity: str, scope: str = "all"):
    assert granularity in ("week", "month")
    return db.query_all(
        f"""
        SELECT date_trunc(%s, c.contacted_at) AS period, {_COUNTS}
        FROM contacts c {_VISITS_JOIN}
        WHERE c.contacted_at IS NOT NULL AND {SCOPES[scope]}
        GROUP BY 1 ORDER BY 1
        """,
        (granularity,),
    )


def icp(dims: list, min_n: int, metric: str, scope: str = "all"):
    """Group by a combination of whitelisted dimensions, drop groups under
    min_n, rank by click or response rate. dims/metric/scope validated by
    caller."""
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
        WHERE {SCOPES[scope]}
        GROUP BY {group_nums}
        HAVING count(*) >= %s
        ORDER BY {rate} DESC, count(*) DESC
        LIMIT 50
        """,
        (min_n,),
    )


def list_contacts(scope: str = "all"):
    return db.query_all(
        f"""
        SELECT c.uid, c.first_name, c.last_name, c.linkedin_url, c.title,
               c.seniority, c.departments, c.company_name, c.company_size,
               c.company_industry, c.connection_degree, c.city, c.state,
               c.country, c.years_at_company, c.target_role,
               c.target_company, c.channel, c.premium, c.follower_count,
               c.created_at, c.contacted_at, c.responded, c.responded_at,
               c.outcome, c.purpose, c.updated_at,
               cu.username AS created_by_name, uu.username AS updated_by_name,
               coalesce(v.visit_count, 0) AS visit_count, v.last_visit
        FROM contacts c {_VISITS_JOIN}
        LEFT JOIN users cu ON cu.id = c.created_by
        LEFT JOIN users uu ON uu.id = c.updated_by
        WHERE {SCOPES[scope]}
        ORDER BY c.contacted_at DESC NULLS LAST, c.created_at DESC NULLS LAST
        """
    )


def update_contact(uid: str, fields: dict, user_id: int) -> dict | None:
    """fields: outcome-recording columns and/or ENRICH_COLUMNS, already
    validated by the API layer. Every write stamps updated_by/updated_at.
    Returns the updated row, or None if uid doesn't exist."""
    allowed = {"responded", "outcome", "responded_at"} | ENRICH_COLUMNS
    assert set(fields) <= allowed, f"unexpected fields: {set(fields) - allowed}"
    if not fields:
        return get_contact(uid)
    sets = ", ".join(f"{col} = %s" for col in fields)
    row = db.query_one(
        f"UPDATE contacts c SET {sets}, updated_by = %s, updated_at = now()"
        f" WHERE uid = %s RETURNING {_RETURN_COLS}",
        (*fields.values(), user_id, uid),
    )
    return row


def stamp_contact(uid: str, user_id: int, created: bool = False) -> None:
    """Attribute a write made through outreach-backend (create / mark
    contacted). created_by is set-if-unset: POST /contacts upserts by
    linkedin_url, so an existing contact keeps its original creator."""
    created_sql = "created_by = coalesce(created_by, %s), " if created else ""
    params = (user_id, user_id, uid) if created else (user_id, uid)
    db.execute(
        f"UPDATE contacts SET {created_sql}updated_by = %s, updated_at = now()"
        " WHERE uid = %s",
        params,
    )


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


# ---------- app_settings (icp-lab-owned key/value store) ----------
# One-time owner SQL creates the table (icp_lab can't CREATE TABLE) — see
# docs/architecture.md. Used for the Work tab's message template + filters.

def settings_table_ready() -> bool:
    row = db.query_one("SELECT to_regclass('public.app_settings') AS t")
    return bool(row and row["t"])


def get_setting(key: str):
    """Returns the stored JSON value, or None if unset or table missing."""
    if not settings_table_ready():
        return None
    row = db.query_one("SELECT value FROM app_settings WHERE key = %s", (key,))
    return row["value"] if row else None


def set_setting(key: str, value, user_id: int) -> None:
    from psycopg2.extras import Json

    db.execute(
        """INSERT INTO app_settings (key, value, updated_at, updated_by)
           VALUES (%s, %s, now(), %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
               updated_at = now(), updated_by = EXCLUDED.updated_by""",
        (key, Json(value), user_id),
    )


# ---------- users + usage_events (icp-lab-owned; one-time owner SQL) ----------

_ADMIN_USER_COLS = (
    "u.id, u.username, u.is_admin, u.can_spend, u.disabled_at, u.created_at,"
    " u.last_login_at, cb.username AS created_by_name"
)


def list_users():
    """Every user with how many contacts they've added / last edited."""
    return db.query_all(
        f"""
        SELECT {_ADMIN_USER_COLS},
               (SELECT count(*) FROM contacts WHERE created_by = u.id) AS contacts_created,
               (SELECT count(*) FROM contacts WHERE updated_by = u.id) AS contacts_last_edited
        FROM users u LEFT JOIN users cb ON cb.id = u.created_by
        ORDER BY u.is_admin DESC, u.created_at
        """
    )


def get_user(user_id: int):
    return db.query_one(
        f"SELECT {_ADMIN_USER_COLS} FROM users u"
        " LEFT JOIN users cb ON cb.id = u.created_by WHERE u.id = %s",
        (user_id,),
    )


def create_user(username: str, password_hash: str, can_spend: bool, created_by: int):
    """Returns the new user id, or None if the username is taken."""
    row = db.query_one(
        """
        INSERT INTO users (username, password_hash, can_spend, created_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (username) DO NOTHING
        RETURNING id
        """,
        (username, password_hash, can_spend, created_by),
    )
    return row["id"] if row else None


# Columns update_user may set; values validated by the API layer.
_USER_UPDATABLE = {"can_spend", "disabled_at", "password_hash"}


def update_user(user_id: int, fields: dict) -> bool:
    assert set(fields) <= _USER_UPDATABLE, f"unexpected fields: {set(fields) - _USER_UPDATABLE}"
    if not fields:
        return True
    sets = ", ".join(f"{col} = %s" for col in fields)
    return db.execute(
        f"UPDATE users SET {sets} WHERE id = %s", (*fields.values(), user_id)
    ) > 0


def log_usage(user_id: int, kind: str, credits: int = 0, input_tokens=None,
              output_tokens=None, model=None, detail=None) -> None:
    """Record one billable (or billing-relevant) call. Never raises: usage
    logging must not break the search or reveal it's recording."""
    from psycopg2.extras import Json

    try:
        db.execute(
            """
            INSERT INTO usage_events
                (user_id, kind, credits, input_tokens, output_tokens, model, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, kind, credits, input_tokens, output_tokens, model,
             Json(detail) if detail is not None else None),
        )
    except Exception as e:  # noqa: BLE001 — logged, deliberately swallowed
        print(f"usage logging failed ({kind}, user {user_id}): {e!r}")


def usage_summary(days: int | None):
    """Per-user totals over the last `days` days (None = all time), plus a
    daily series for the same window. Users with no usage still appear."""
    window = "e.at >= now() - make_interval(days => %s)" if days else "TRUE"
    params = (days,) if days else ()
    totals = db.query_all(
        f"""
        SELECT u.id AS user_id, u.username,
               coalesce(sum(e.credits), 0) AS apollo_credits,
               count(e.id) FILTER (WHERE e.kind = 'apollo_reveal') AS reveals,
               count(e.id) FILTER (WHERE e.kind = 'apollo_search') AS searches,
               count(e.id) FILTER (WHERE e.kind = 'claude') AS claude_calls,
               coalesce(sum(e.input_tokens), 0) AS input_tokens,
               coalesce(sum(e.output_tokens), 0) AS output_tokens,
               max(e.at) AS last_used
        FROM users u
        LEFT JOIN usage_events e ON e.user_id = u.id AND {window}
        GROUP BY u.id, u.username
        ORDER BY apollo_credits DESC, claude_calls DESC, u.username
        """,
        params,
    )
    daily = db.query_all(
        f"""
        SELECT date_trunc('day', e.at) AS day, u.username,
               coalesce(sum(e.credits), 0) AS apollo_credits,
               count(*) FILTER (WHERE e.kind = 'claude') AS claude_calls,
               count(*) FILTER (WHERE e.kind = 'apollo_search') AS searches
        FROM usage_events e JOIN users u ON u.id = e.user_id
        WHERE {window}
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        params,
    )
    return {"totals": totals, "daily": daily}
