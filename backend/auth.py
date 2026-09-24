"""Multi-user auth: PBKDF2 passwords in the `users` table + HMAC-signed
session cookie carrying the user id.

Env vars:
  SESSION_SECRET           long random string used to sign session cookies
  OWNER_USERNAME           admin username (default "vaughn") — see bootstrap
  DASHBOARD_PASSWORD_HASH  break-glass admin password hash(es), output of
                           `python backend/hash_password.py`
  DEV_MODE                 set to any value locally to allow the cookie over http

Bootstrap / break-glass: logging in as OWNER_USERNAME with a password that
matches DASHBOARD_PASSWORD_HASH creates that admin row if it doesn't exist
yet (first deploy), or re-enables and resets it if it does (locked out).
"""
import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

import db

PBKDF2_ITERATIONS = 600_000
SESSION_COOKIE = "icp_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days

# In-process brute-force throttle: per-username lockout, plus a global cap
# so failures spread across many usernames still trip it.
_MAX_FAILURES = 10
_GLOBAL_MAX_FAILURES = 50
_LOCKOUT_SECONDS = 15 * 60
_failures: dict = {}  # key (username or "*") -> {"count", "locked_until"}

_USER_COLS = "id, username, is_admin, can_spend, disabled_at"


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.strip().split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, AttributeError):
        return False


def owner_username() -> str:
    return os.environ.get("OWNER_USERNAME", "vaughn").strip().lower()


def _locked(key: str, now: float) -> bool:
    return now < _failures.get(key, {}).get("locked_until", 0.0)


def _record_failure(key: str, limit: int, now: float):
    f = _failures.setdefault(key, {"count": 0, "locked_until": 0.0})
    f["count"] += 1
    if f["count"] >= limit:
        f["locked_until"] = now + _LOCKOUT_SECONDS
        f["count"] = 0


def _break_glass(password: str) -> str | None:
    """The env-var hash that matches, if any."""
    stored = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
    for h in (h.strip() for h in stored.split(",")):
        if h and verify_password(password, h):
            return h
    return None


def check_login(username: str, password: str):
    """Returns the logged-in user dict, or None on bad credentials."""
    now = time.time()
    username = (username or "").strip().lower()
    if _locked("*", now) or _locked(username, now):
        raise HTTPException(429, "Too many failed attempts; try again later.")

    row = db.query_one(
        f"SELECT {_USER_COLS}, password_hash FROM users WHERE username = %s",
        (username,),
    )
    user = None
    if row and row["disabled_at"] is None and verify_password(password, row["password_hash"]):
        user = row
    elif username == owner_username():
        env_hash = _break_glass(password)
        if env_hash:
            user = db.query_one(
                f"""
                INSERT INTO users (username, password_hash, is_admin, can_spend)
                VALUES (%s, %s, true, true)
                ON CONFLICT (username) DO UPDATE
                   SET password_hash = EXCLUDED.password_hash,
                       is_admin = true, disabled_at = NULL
                RETURNING {_USER_COLS}
                """,
                (username, env_hash),
            )

    if user is None:
        _record_failure(username, _MAX_FAILURES, now)
        _record_failure("*", _GLOBAL_MAX_FAILURES, now)
        return None
    _failures.pop(username, None)
    db.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user["id"],))
    user.pop("password_hash", None)
    return user


def _secret() -> bytes:
    return os.environ["SESSION_SECRET"].encode()


def create_session_token(user_id: int) -> str:
    payload = f"{int(time.time()) + SESSION_MAX_AGE}.u{user_id}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str):
    """Returns the token's user id, or None if invalid/expired. Pre-users
    tokens (`<expiry>.<sig>` / `<expiry>.<role>.<sig>`) no longer verify."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        expires, subject = payload.split(".")
        if time.time() >= int(expires) or not subject.startswith("u"):
            return None
        return int(subject[1:])
    except (ValueError, KeyError):
        return None


def current_user(request: Request):
    """The request's active user (dict) or None. One indexed lookup per
    request, so disabling a user takes effect immediately."""
    if hasattr(request.state, "user"):
        return request.state.user
    token = request.cookies.get(SESSION_COOKIE)
    user_id = verify_session_token(token) if token else None
    user = None
    if user_id is not None:
        user = db.query_one(
            f"SELECT {_USER_COLS} FROM users WHERE id = %s AND disabled_at IS NULL",
            (user_id,),
        )
    request.state.user = user
    return user


def is_authenticated(request: Request) -> bool:
    return current_user(request) is not None


def require_auth(request: Request):
    """FastAPI dependency for API routes (any active user)."""
    if current_user(request) is None:
        raise HTTPException(401, "Not authenticated")


def require_admin(request: Request):
    """FastAPI dependency for admin-only routes (Gmail + user management)."""
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admins only")


def require_spend(request: Request):
    """FastAPI dependency for routes that cost money (Claude / Apollo)."""
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "Not authenticated")
    if not user["can_spend"]:
        raise HTTPException(403, "Spending not enabled for this account")


def cookie_secure() -> bool:
    return not os.environ.get("DEV_MODE")
