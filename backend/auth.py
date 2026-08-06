"""Single-user auth: PBKDF2 password verification + HMAC-signed session cookie.

Env vars:
  DASHBOARD_PASSWORD_HASH  output of `python backend/hash_password.py <password>`
  SESSION_SECRET           long random string used to sign session cookies
  DEV_MODE                 set to any value locally to allow the cookie over http
"""
import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

PBKDF2_ITERATIONS = 600_000
SESSION_COOKIE = "icp_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days

# Naive in-process brute-force throttle: after too many failures, lock out.
_MAX_FAILURES = 10
_LOCKOUT_SECONDS = 15 * 60
_failures = {"count": 0, "locked_until": 0.0}


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


# Roles: "owner" (Vaughn — full access) and "guest" (a friend — everything
# except the Gmail-backed reply-scanner endpoints). Each role has its own
# env var; both accept a comma-separated hash list.
ROLES = ("owner", "guest")
_ROLE_ENV_VARS = (
    ("DASHBOARD_PASSWORD_HASH", "owner"),
    ("GUEST_PASSWORD_HASH", "guest"),
)


def check_login(password: str):
    """Returns the matching role ('owner'/'guest') or None.
    Shared lockout after repeated failures."""
    now = time.time()
    if now < _failures["locked_until"]:
        raise HTTPException(429, "Too many failed attempts; try again later.")
    for env_var, role in _ROLE_ENV_VARS:
        stored = os.environ.get(env_var, "")
        hashes = [h.strip() for h in stored.split(",") if h.strip()]
        if any(verify_password(password, h) for h in hashes):
            _failures["count"] = 0
            return role
    _failures["count"] += 1
    if _failures["count"] >= _MAX_FAILURES:
        _failures["locked_until"] = now + _LOCKOUT_SECONDS
        _failures["count"] = 0
    return None


def _secret() -> bytes:
    return os.environ["SESSION_SECRET"].encode()


def create_session_token(role: str = "owner") -> str:
    payload = f"{int(time.time()) + SESSION_MAX_AGE}.{role}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str):
    """Returns the token's role, or None if invalid/expired."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        parts = payload.split(".")
        expires = int(parts[0])
        # tokens minted before roles existed carry no role part → owner
        # (only the owner had sessions back then)
        role = parts[1] if len(parts) > 1 else "owner"
        if time.time() >= expires or role not in ROLES:
            return None
        return role
    except (ValueError, KeyError):
        return None


def session_role(request: Request):
    """'owner' / 'guest' / None for the request's cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    return verify_session_token(token) if token else None


def is_authenticated(request: Request) -> bool:
    return session_role(request) is not None


def require_auth(request: Request):
    """FastAPI dependency for API routes (any role)."""
    if session_role(request) is None:
        raise HTTPException(401, "Not authenticated")


def require_owner(request: Request):
    """FastAPI dependency for owner-only routes (Gmail-backed features)."""
    role = session_role(request)
    if role is None:
        raise HTTPException(401, "Not authenticated")
    if role != "owner":
        raise HTTPException(403, "Not available for guest sessions")


def cookie_secure() -> bool:
    return not os.environ.get("DEV_MODE")
