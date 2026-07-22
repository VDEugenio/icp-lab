"""Gmail reply scanner: LinkedIn "sent you a message" notification emails
become responded=true on matching contacts.

Policy (settled with Vaughn): an exact full-name match against exactly one
contact is applied automatically (responded_at = the email's date, never
restamped if already responded); anything ambiguous — multiple exact
matches, or only near-matches — lands in a review queue surfaced on the
Contacts tab. Nothing else writes.

State lives in reply_events, a table icp-lab owns outright (one row per
processed Gmail message, keyed by Gmail's message id, so scans are
idempotent). Creation SQL is in docs/architecture.md — the icp_lab role
cannot CREATE TABLE, so it's a one-time owner statement.

Gmail access is read-only (gmail.readonly scope) via a refresh token minted
once with backend/gmail_auth.py. Contacts writes still go through
queries.update_contact like every other write.
"""
import html
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parseaddr

import httpx
import psycopg2.errors

import db
import queries

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"

AUTO_SCAN_INTERVAL = 30 * 60  # dashboard-load scans run at most this often
MAX_MESSAGES = 500            # per scan, across pages
MAX_CANDIDATES = 6            # per pending event

# Only message notifications — invites, job alerts, and InMail promos are
# filtered out by the Gmail query itself (subject phrases LinkedIn uses).
_SEARCH = (
    'from:linkedin.com subject:("messaged you" OR "sent you a message"'
    ' OR "new message from" OR "replied to your")'
)

_SUBJECT_PATTERNS = [
    re.compile(r"^(?P<name>.+?)\s+(?:just\s+)?messaged you", re.I),
    re.compile(r"^(?P<name>.+?)\s+sent you a (?:new )?message", re.I),
    re.compile(r"^(?P<name>.+?)\s+replied to your", re.I),
    re.compile(r"new message from\s+(?P<name>.+?)\s*$", re.I),
]
_FROM_VIA = re.compile(r"^(?P<name>.+?)\s+via LinkedIn$", re.I)
_NOT_A_PERSON = {"linkedin", "linkedin messaging", "linkedin member"}


class ScanError(Exception):
    """User-facing scan failure (config, table, Gmail auth/reachability)."""


def configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    )


# ---------- Gmail access ----------

_token = None
_token_expiry = 0.0
_token_lock = threading.Lock()


def _access_token(client: httpx.Client) -> str:
    global _token, _token_expiry
    with _token_lock:
        if _token and time.time() < _token_expiry - 60:
            return _token
        try:
            r = client.post(TOKEN_URL, data={
                "client_id": os.environ["GMAIL_CLIENT_ID"],
                "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
                "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
                "grant_type": "refresh_token",
            })
        except httpx.HTTPError as e:
            raise ScanError(f"Google token endpoint unreachable: {type(e).__name__}")
        if r.status_code >= 400:
            # invalid_grant = token revoked/expired → re-run the helper
            raise ScanError(
                "Gmail token refresh failed "
                f"({r.status_code}): {r.text[:200]} — re-run backend/gmail_auth.py"
            )
        data = r.json()
        _token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 3600)
        return _token


def _gmail_get(client: httpx.Client, path: str, params: dict) -> dict:
    try:
        r = client.get(
            f"{GMAIL_API}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {_access_token(client)}"},
        )
    except httpx.HTTPError as e:
        raise ScanError(f"Gmail unreachable: {type(e).__name__}")
    if r.status_code == 401:
        global _token
        _token = None  # stale access token; next scan re-mints
        raise ScanError("Gmail rejected the access token — try the scan again")
    if r.status_code >= 400:
        raise ScanError(f"Gmail API error ({r.status_code}): {r.text[:200]}")
    return r.json()


def _list_message_ids(client, days: int) -> list:
    ids, page = [], None
    q = f"{_SEARCH} newer_than:{days}d"
    while True:
        params = {"q": q, "maxResults": 100}
        if page:
            params["pageToken"] = page
        data = _gmail_get(client, "messages", params)
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page or len(ids) >= MAX_MESSAGES:
            return ids[:MAX_MESSAGES]


def _fetch_message(client, gmail_id: str) -> dict:
    """Returns {subject, from, snippet, received_at} for one message."""
    data = _gmail_get(client, f"messages/{gmail_id}", {
        "format": "metadata",
        "metadataHeaders": ["Subject", "From"],
    })
    headers = {
        h["name"].lower(): h["value"]
        for h in data.get("payload", {}).get("headers", [])
    }
    return {
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        # LinkedIn pads snippets with invisible fillers (combining grapheme
        # joiner, zero-width spaces/joiners, word joiner, BOM)
        "snippet": re.sub(
            "[\u034f\u200b-\u200d\u2060\ufeff]", "",
            html.unescape(data.get("snippet", ""))
        ).strip(),
        "received_at": datetime.fromtimestamp(
            int(data.get("internalDate", 0)) / 1000, tz=timezone.utc
        ),
    }


# ---------- name parsing & matching ----------

def _sender_name(subject: str, from_header: str) -> str | None:
    display, _ = parseaddr(from_header)
    m = _FROM_VIA.match(display.strip())
    if not m:
        for pat in _SUBJECT_PATTERNS:
            m = pat.search(subject.strip())
            if m:
                break
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group("name")).strip().strip(",")
    if not name or _norm(name) in _NOT_A_PERSON:
        return None
    return name


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().lower()


# Credential tokens LinkedIn display names carry that contact names don't.
_NAME_SUFFIXES = {
    "phd", "ph.d", "ph.d.", "md", "m.d.", "dds", "esq", "esq.",
    "mba", "cpa", "cfa", "pmp", "shrm-cp", "shrm-scp",
    "jr", "jr.", "sr", "sr.", "ii", "iii", "iv",
}


def _clean_name(s) -> str:
    """Normalize a person name for matching: drop everything after a comma
    or pipe ("Amanpreet Kaur, Ph.D."), parentheticals ("(He/Him)"),
    emoji/symbols, and trailing credential tokens ("John Smith MBA")."""
    s = str(s or "").split(",")[0].split("|")[0]
    s = re.sub(r"\([^)]*\)", " ", s)
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("S"))
    tokens = _norm(s).split(" ")
    while tokens and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _match(name: str, contacts: list) -> tuple:
    """Returns (exact, near): contacts whose full name equals the sender's,
    and near-misses (same first + last initial, or same last + first initial)
    worth offering in the review queue. A first-name-only sender (some
    LinkedIn subjects say just "Cole just messaged you") never auto-applies —
    every contact with that first name becomes a review candidate instead."""
    n = _clean_name(name)
    parts = n.split(" ")
    nf, nl = parts[0], parts[-1] if len(parts) > 1 else ""
    exact, near = [], []
    for c in contacts:
        cf, cl = _clean_name(c["first_name"]), _clean_name(c["last_name"])
        full = f"{cf} {cl}".strip()
        if nl and full and full == n:
            exact.append(c)
        elif nf and nl and cf and cl and (
            (cf == nf and cl[0] == nl[0]) or (cl == nl and cf[0] == nf[0])
        ):
            near.append(c)
        elif not nl and cf == nf:
            near.append(c)
    return exact, near[:MAX_CANDIDATES]


def _mark_responded(uid: str, when: datetime) -> bool:
    """Flip responded on, stamping the email's date. Never restamps an
    already-responded contact. Returns whether anything was written."""
    current = queries.get_contact(uid)
    if current is None or current["responded"]:
        return False
    queries.update_contact(uid, {"responded": True, "responded_at": when})
    return True


# ---------- scan ----------

_scan_lock = threading.Lock()
_last_auto_scan = 0.0


def scan(days: int = 30, auto: bool = False) -> dict:
    """Process new LinkedIn message notifications. Idempotent: every Gmail
    message id is recorded in reply_events exactly once."""
    global _last_auto_scan
    if not configured():
        raise ScanError("Gmail is not configured — see docs/operations.md")

    with _scan_lock:
        if auto and time.time() - _last_auto_scan < AUTO_SCAN_INTERVAL:
            return {"throttled": True}

        try:
            existing = {
                r["gmail_id"]
                for r in db.query_all("SELECT gmail_id FROM reply_events")
            }
        except psycopg2.errors.UndefinedTable:
            raise ScanError(
                "reply_events table missing — run the one-time SQL in docs/architecture.md"
            )

        with httpx.Client(timeout=20) as client:
            new_ids = [i for i in _list_message_ids(client, days) if i not in existing]
            messages = [(i, _fetch_message(client, i)) for i in new_ids]

        contacts = db.query_all(
            "SELECT uid, first_name, last_name, company_name, responded FROM contacts"
        )

        auto_applied, pending_new, no_match_new, ignored_new = [], 0, 0, 0
        for gmail_id, msg in messages:
            name = _sender_name(msg["subject"], msg["from"])
            if name is None:
                status, matched_uid, candidate_uids = "ignored", None, None
                ignored_new += 1
            else:
                exact, near = _match(name, contacts)
                if len(exact) == 1:
                    status, matched_uid, candidate_uids = "auto_applied", exact[0]["uid"], None
                    _mark_responded(matched_uid, msg["received_at"])
                    auto_applied.append({"sender_name": name, "uid": matched_uid})
                elif exact or near:
                    status, matched_uid = "pending", None
                    candidate_uids = ",".join(c["uid"] for c in (exact or near))
                    pending_new += 1
                else:
                    status, matched_uid, candidate_uids = "no_match", None, None
                    no_match_new += 1
            db.execute(
                """
                INSERT INTO reply_events
                    (gmail_id, sender_name, received_at, snippet, status,
                     matched_uid, candidate_uids)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gmail_id) DO NOTHING
                """,
                (gmail_id, name or "", msg["received_at"], msg["snippet"][:500],
                 status, matched_uid, candidate_uids),
            )

        # Re-evaluate stored no-matches against current contacts: fixing a
        # mangled name in the Enrich tab makes old notifications match
        # retroactively. Local data only — no Gmail calls.
        for r in db.query_all(
            "SELECT gmail_id, sender_name, received_at FROM reply_events"
            " WHERE status = 'no_match' AND sender_name <> ''"
        ):
            exact, near = _match(r["sender_name"], contacts)
            if len(exact) == 1:
                _mark_responded(exact[0]["uid"], r["received_at"])
                db.execute(
                    "UPDATE reply_events SET status = 'auto_applied',"
                    " matched_uid = %s WHERE gmail_id = %s",
                    (exact[0]["uid"], r["gmail_id"]),
                )
                auto_applied.append(
                    {"sender_name": r["sender_name"], "uid": exact[0]["uid"]}
                )
            elif exact or near:
                db.execute(
                    "UPDATE reply_events SET status = 'pending',"
                    " candidate_uids = %s WHERE gmail_id = %s",
                    (",".join(c["uid"] for c in (exact or near)), r["gmail_id"]),
                )
                pending_new += 1

        _last_auto_scan = time.time()
        return {
            "throttled": False,
            "new_events": len(messages),
            "auto_applied": auto_applied,
            "pending_new": pending_new,
            "no_match_new": no_match_new,
            "ignored_new": ignored_new,
        }


# ---------- review queue ----------

def _contacts_by_uid(uids: list) -> dict:
    if not uids:
        return {}
    rows = db.query_all(
        "SELECT uid, first_name, last_name, company_name, responded"
        " FROM contacts WHERE uid = ANY(%s)",
        (list(uids),),
    )
    return {r["uid"]: r for r in rows}


def _display(c) -> str:
    return " ".join(filter(None, [c["first_name"], c["last_name"]])) or c["uid"]


def status() -> dict:
    """Everything the Replies card needs in one call."""
    if not configured():
        return {"configured": False, "table_ready": False, "last_scan": None,
                "pending": [], "recent_auto": []}
    try:
        pending_rows = db.query_all(
            "SELECT gmail_id, sender_name, received_at, snippet, candidate_uids"
            " FROM reply_events WHERE status = 'pending'"
            " ORDER BY received_at DESC NULLS LAST"
        )
        recent_rows = db.query_all(
            "SELECT gmail_id, sender_name, received_at, matched_uid, status"
            " FROM reply_events WHERE status IN ('auto_applied', 'confirmed')"
            " ORDER BY scanned_at DESC, received_at DESC LIMIT 12"
        )
        last = db.query_one("SELECT max(scanned_at) AS t FROM reply_events")
    except psycopg2.errors.UndefinedTable:
        return {"configured": True, "table_ready": False, "last_scan": None,
                "pending": [], "recent_auto": []}

    uids = {r["matched_uid"] for r in recent_rows if r["matched_uid"]}
    for r in pending_rows:
        uids.update((r["candidate_uids"] or "").split(","))
    by_uid = _contacts_by_uid([u for u in uids if u])

    pending = []
    for r in pending_rows:
        candidates = [
            {"uid": u, "name": _display(c), "company": c["company_name"],
             "responded": c["responded"]}
            for u in (r["candidate_uids"] or "").split(",")
            if u and (c := by_uid.get(u))
        ]
        pending.append({
            "gmail_id": r["gmail_id"], "sender_name": r["sender_name"],
            "received_at": r["received_at"], "snippet": r["snippet"],
            "candidates": candidates,
        })

    recent_auto = [
        {"gmail_id": r["gmail_id"], "sender_name": r["sender_name"],
         "received_at": r["received_at"], "matched_uid": r["matched_uid"],
         "matched_name": _display(by_uid[r["matched_uid"]])
             if r["matched_uid"] in by_uid else r["matched_uid"],
         "status": r["status"]}
        for r in recent_rows
    ]

    return {"configured": True, "table_ready": True,
            "last_scan": last["t"] if last else None,
            "pending": pending, "recent_auto": recent_auto}


def confirm(gmail_id: str, uid: str) -> dict:
    ev = db.query_one(
        "SELECT gmail_id, received_at FROM reply_events"
        " WHERE gmail_id = %s AND status = 'pending'",
        (gmail_id,),
    )
    if ev is None:
        raise LookupError("No such pending reply")
    if queries.get_contact(uid) is None:
        raise ValueError("No such contact")
    applied = _mark_responded(uid, ev["received_at"] or datetime.now(timezone.utc))
    db.execute(
        "UPDATE reply_events SET status = 'confirmed', matched_uid = %s"
        " WHERE gmail_id = %s",
        (uid, gmail_id),
    )
    return {"ok": True, "applied": applied}


def dismiss(gmail_id: str) -> dict:
    n = db.execute(
        "UPDATE reply_events SET status = 'dismissed'"
        " WHERE gmail_id = %s AND status = 'pending'",
        (gmail_id,),
    )
    if n == 0:
        raise LookupError("No such pending reply")
    return {"ok": True}
