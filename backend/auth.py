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


def check_login(password: str) -> bool:
    """Password check with a lockout after repeated failures."""
    now = time.time()
    if now < _failures["locked_until"]:
        raise HTTPException(429, "Too many failed attempts; try again later.")
    stored = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
    if stored and verify_password(password, stored):
        _failures["count"] = 0
        return True
    _failures["count"] += 1
    if _failures["count"] >= _MAX_FAILURES:
        _failures["locked_until"] = now + _LOCKOUT_SECONDS
        _failures["count"] = 0
    return False


def _secret() -> bytes:
    return os.environ["SESSION_SECRET"].encode()


def create_session_token() -> str:
    expires = str(int(time.time()) + SESSION_MAX_AGE)
    sig = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def verify_session_token(token: str) -> bool:
    try:
        expires, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and time.time() < int(expires)
    except (ValueError, KeyError):
        return False


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token) and verify_session_token(token)


def require_auth(request: Request):
    """FastAPI dependency for API routes."""
    if not is_authenticated(request):
        raise HTTPException(401, "Not authenticated")


def cookie_secure() -> bool:
    return not os.environ.get("DEV_MODE")
