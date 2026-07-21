# icp-lab

Single-user analytics dashboard for my LinkedIn outreach database. Reads the
same Neon PostgreSQL database as
[outreach-backend](https://github.com/VDEugenio/outreach-backend); its only
write paths are `contacts.responded`, `contacts.responded_at`, and
`contacts.outcome` (recorded manually from the dashboard).

- **Overview** — headline stats, contacted → clicked → responded funnel,
  channel split, response rate over time (weekly/monthly).
- **Breakdowns** — response & click rates by seniority, company size, industry,
  connection degree, country, target role, premium, channel — always with
  sample sizes.
- **Contacts** — full table with inline editing of responded / responded-at /
  outcome.

## Stack

FastAPI + psycopg2 (`backend/`), static vanilla HTML/CSS/JS (`frontend/`, no
build step) served by the same app. One Railway service.

## Local dev

```
pip install -r requirements.txt
copy .env.example .env        # then fill it in (see below)
uvicorn backend.main:app --reload
```

`.env` values:

| Var | What |
|---|---|
| `DATABASE_URL` | Neon connection string |
| `DASHBOARD_PASSWORD_HASH` | output of `python backend/hash_password.py` |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DEV_MODE` | set to `1` locally so the session cookie works over http; never set in production |

## Deploy (Railway)

Procfile + requirements.txt + runtime.txt, same pattern as outreach-backend.
Set `DATABASE_URL`, `DASHBOARD_PASSWORD_HASH`, and `SESSION_SECRET` in the
service Variables tab. Do **not** set `DEV_MODE`.

## Auth

Single user. Login page checks the password against a PBKDF2 hash from the
environment and sets an HMAC-signed, HTTP-only session cookie (30 days).
Ten failed attempts trigger a 15-minute lockout. No credentials are ever
stored in the repo.
