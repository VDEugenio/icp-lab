# Architecture

## System context

icp-lab is one piece of a three-part outreach system:

```
┌──────────────────┐   creates contacts,     ┌─────────────────────┐
│ Chrome extension │──── tracking links ────▶│  outreach-backend    │
│ (LinkedIn pages) │                         │  FastAPI on Railway  │
└──────────────────┘                         └──────────┬───────────┘
                                                        │ owns writes:
        tracking-link clicks ──────────────────────────▶│ contacts, visits
                                                        ▼
                                             ┌─────────────────────┐
                                             │   Neon PostgreSQL   │
                                             └──────────▲───────────┘
                                                        │ reads everything;
                                                        │ writes only granted
                                                        │ columns (icp_lab role)
┌───────────────────────────────────────────────────────┴───────────┐
│                            icp-lab                                │
│  analytics · enrichment · prospecting  (this repo, own Railway    │
│  service, same database)                                          │
└───────────────────────────────────────────────────────────────────┘
```

- The **extension** generates personalized messages while browsing LinkedIn,
  gets each person a short tracking link, and records enrichment data.
- **outreach-backend** owns contact creation (3-char `uid` generation),
  serves the `vaughneugenio.com/r/{uid}` redirects, and logs link visits.
- **icp-lab** reads it all, records outcomes, and closes the loop by scoring
  new prospects against historical results. When the Prospect tab needs a new
  contact + tracking link, it calls outreach-backend's API — it never inserts
  rows itself, so uid generation stays in one place.

## Repository layout

```
icp-lab/
├── backend/
│   ├── main.py           FastAPI app: routes, auth wiring, static serving,
│   │                     outreach-backend proxies
│   ├── auth.py           PBKDF2 password check, HMAC session cookies, lockout
│   ├── db.py             psycopg2 pool (semaphore-gated, health-checked)
│   ├── queries.py        all analytics SQL + the only DB write path
│   ├── jd_finder.py      Prospect tab: Claude JD parse, Apollo search,
│   │                     ICP scoring, reveal
│   ├── replies.py        Reply scanner: Gmail LinkedIn notifications →
│   │                     responded, review queue in reply_events
│   ├── gmail_auth.py     CLI helper: one-time Gmail OAuth (refresh token)
│   └── hash_password.py  CLI helper: generates DASHBOARD_PASSWORD_HASH
├── frontend/
│   ├── index.html        the dashboard shell (all tabs)
│   ├── login.html        sign-in page
│   └── static/
│       ├── app.js        all client logic (vanilla JS, no build step)
│       └── style.css     styles incl. light/dark theme via CSS variables
├── docs/                 this documentation
├── Procfile              web: uvicorn backend.main:app …
├── requirements.txt
├── runtime.txt           python-3.12
└── .env.example          placeholder env vars (never commit real .env)
```

There is deliberately **no build step**: the frontend is plain files served by
FastAPI (`/static/*` unauthenticated for assets; `/` and `/login` gated).

## Authentication

Multi-user, all sharing one workspace (same contacts and analytics). Accounts
live in icp-lab's own `users` table; admins create them on the Admin tab.

- **Login**: username + password. Passwords are PBKDF2-SHA256 (600k
  iterations, `pbkdf2_sha256$600000$<salt-hex>$<hash-hex>`), constant-time
  compared. Usernames are stored lowercase.
- **Roles / flags** per user: `is_admin` (Admin tab + the Gmail-backed
  reply scanner) and `can_spend` (Claude-backed searches and Apollo Reveal
  credits — `/api/jd-search`, `/api/work-search`, `/api/prospect-reveal`
  return 403 without it). Everything else is identical for every user.
- **Bootstrap / break-glass**: logging in as `OWNER_USERNAME` (default
  `vaughn`) with a password matching `DASHBOARD_PASSWORD_HASH` creates that
  admin row on first login, or — if it exists — re-enables it and resets
  its password to that hash. So the env var is both the seed and the
  "locked myself out" recovery path.
- **Session**: an HMAC-SHA256-signed token (`<expiry>.u<user_id>.<sig>`,
  keyed by `SESSION_SECRET`) in the `icp_session` cookie — HTTP-only,
  SameSite=Lax, Secure (unless `DEV_MODE`), 30-day expiry. Every request
  re-loads the user by id and rejects disabled ones, so disabling a user
  signs them out immediately. Pre-multi-user tokens no longer verify.
- **Lockout**: 10 failed logins for one username → 15-minute lockout for
  that username; 50 failures across all usernames → global 15-minute
  lockout. In-process (resets on redeploy).
- **Route gating**: every `/api/*` route except `/api/login` requires a valid
  session (FastAPI dependency → 401); `require_admin` / `require_spend`
  layer the flags on top. `/` redirects to `/login` when unauthenticated.
  `/health` is open. API docs (`/docs`, `/openapi.json`) are disabled.

### Attribution

Every write icp-lab makes is stamped with the signed-in user:

| Write | Stamp |
|---|---|
| Contacts-tab / Enrich edits (`PATCH /api/contacts/{uid}`) | `contacts.updated_by`, `updated_at` |
| Contact created via outreach-backend (`/api/outreach-contact`) | `contacts.created_by` (set-if-unset — outreach-backend upserts by `linkedin_url`, so an existing contact keeps its creator) + `updated_by`/`updated_at` |
| Marked contacted (`/api/outreach-contacted`) | `updated_by`, `updated_at` |
| Reply scanner auto-apply / confirm (admin session) | `updated_by`, `updated_at` |
| Work-tab settings (per user: key `work:<user_id>`) | `app_settings.updated_by` |

`created_by` NULL = created by the Chrome extension or before multi-user.
`contacts.created_by`/`updated_by` deliberately have **no** foreign key:
outreach-backend owns `contacts` and shouldn't depend on an icp-lab table.
Money-spending calls are logged separately in `usage_events` (one row per
Claude call with token counts, per Apollo search, and per Reveal with
`credits = 1`); logging failures never break the call being logged.

## Database access — defense in depth

Two layers guarantee icp-lab cannot corrupt the shared database:

**Layer 1 — application:** `queries.update_contact()` is the only write path.
It asserts its field whitelist; the PATCH endpoint validates values (outcome
enum, channel/connection-degree enums, numbers). All SQL is parameterized;
breakdown dimensions come from a hard-coded whitelist mapping, never from
request strings.

**Layer 2 — the `icp_lab` Postgres role.** The `DATABASE_URL` icp-lab uses
belongs to a role created with column-level grants (created via SQL, **not**
the Neon console — console-created roles get `neon_superuser`):

```sql
CREATE ROLE icp_lab WITH LOGIN PASSWORD '<generated>';
GRANT CONNECT ON DATABASE neondb TO icp_lab;
GRANT USAGE ON SCHEMA public TO icp_lab;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO icp_lab;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO icp_lab;
GRANT UPDATE (
  responded, responded_at, outcome,
  first_name, last_name, title, seniority, departments,
  company_name, company_size, company_industry,
  city, state, country, years_at_company, email_status,
  premium, follower_count, connection_degree,
  target_role, target_company, channel, contacted_at
) ON contacts TO icp_lab;
```

Deliberately **not** updatable, even by a bug: `uid` (tracking id),
`created_at`, `apollo_raw` (raw Apollo response), `linkedin_url`. No INSERT
or DELETE anywhere on the shared tables; `visits` is fully read-only.

### The reply_events table (icp-lab's own state)

The reply scanner needs one table of its own — one row per processed Gmail
message, so scans are idempotent and review decisions persist. The `icp_lab`
role can't CREATE TABLE, so this is a one-time statement run as the database
owner (same session type as the role setup):

```sql
CREATE TABLE IF NOT EXISTS reply_events (
    gmail_id        TEXT PRIMARY KEY,     -- Gmail message id (dedupe key)
    sender_name     TEXT NOT NULL,        -- parsed from the notification
    received_at     TIMESTAMPTZ,          -- the email's date
    snippet         TEXT,
    status          TEXT NOT NULL,        -- auto_applied | pending | confirmed
                                          -- | dismissed | no_match | ignored
    matched_uid     TEXT,                 -- contact it was applied to
    candidate_uids  TEXT,                 -- comma-joined, for pending review
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON reply_events TO icp_lab;
```

No DELETE grant — events are only ever re-statused. Writes to `contacts`
still go exclusively through `queries.update_contact()`; `replies.py` only
writes its own table plus that one call path.

### The purpose column + app_settings table (Work tab, added 2026-08)

`contacts.purpose` separates the two outreach worlds: NULL = job search
(all pre-existing rows), `'work'` = work outreach from the Work tab. The
column itself is created by outreach-backend's `init_db()` migration; the
value is set by `POST /contacts` (set-if-unset) and re-taggable from the
Contacts tab via the normal PATCH path.

`app_settings` is icp-lab's second owned table (same pattern as
`reply_events`): a JSONB key/value store holding the Work tab's message
template, persona, locations and client-exclusion list. One-time owner SQL
(already run):

```sql
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS purpose TEXT;
GRANT UPDATE (purpose) ON contacts TO icp_lab;
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON app_settings TO icp_lab;
```

Work rows have no tracking links (deliberate — no vaughneugenio.com links
in messages sent on the employer's behalf), so under the `work` scope the
frontend hides every click metric instead of showing a permanent 0%.

### The users + usage_events tables (multi-user, added 2026-09)

icp-lab's third and fourth owned tables, plus attribution columns on
`contacts`. One-time owner SQL:

```sql
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,        -- stored lowercase
    password_hash TEXT NOT NULL,               -- pbkdf2_sha256$600000$salt$hash
    is_admin      BOOLEAN NOT NULL DEFAULT false,
    can_spend     BOOLEAN NOT NULL DEFAULT false,
    disabled_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    INTEGER REFERENCES users(id),
    last_login_at TIMESTAMPTZ
);
GRANT SELECT, INSERT, UPDATE ON users TO icp_lab;
GRANT USAGE ON SEQUENCE users_id_seq TO icp_lab;

CREATE TABLE IF NOT EXISTS usage_events (
    id            BIGSERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    kind          TEXT NOT NULL,               -- apollo_reveal | apollo_search | claude
    credits       INTEGER NOT NULL DEFAULT 0,  -- Apollo credits spent
    input_tokens  INTEGER,
    output_tokens INTEGER,
    model         TEXT,
    detail        JSONB,
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT ON usage_events TO icp_lab;
GRANT USAGE ON SEQUENCE usage_events_id_seq TO icp_lab;

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS created_by INTEGER,
                     ADD COLUMN IF NOT EXISTS updated_by INTEGER,
                     ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
GRANT UPDATE (created_by, updated_by, updated_at) ON contacts TO icp_lab;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS updated_by INTEGER;
```

Existing extension/outreach-backend code is unaffected: it only reads and
writes named columns.

## Connection pooling

`backend/db.py` wraps a `ThreadedConnectionPool(1, 8)` with two fixes learned
the hard way:

1. **Semaphore-gated checkout** — psycopg2 pools *raise* "pool exhausted"
   instead of queueing. The dashboard fires six API calls on page load; a
   `BoundedSemaphore(8)` makes excess requests wait instead of 500ing.
2. **Health check on checkout** — Neon closes idle connections; the pool
   would happily return a dead one. Each checkout runs `SELECT 1` and
   discards broken connections (important on Railway, where the app may
   idle for hours).

Transactions: commit on success, rollback on exception; connections that fail
rollback are closed rather than returned to the pool.

## Frontend architecture

One `app.js`, organized in sections per tab. Conventions:

- `api(path, opts)` — fetch wrapper; 401 responses redirect to `/login`.
- All user data rendered through `esc()` or DOM `textContent` (no unescaped
  interpolation of DB/Apollo content into HTML).
- A single shared tooltip layer (`#tooltip`) and toast (`#toast`).
- Analytics tabs cache data at load; any successful write calls
  `refreshAnalytics()` to reload stats/timeseries/breakdown/ICP quietly.
- Charts are hand-rolled SVG (timeseries) and CSS bars (funnel, breakdowns) —
  colors follow a validated light/dark palette defined as CSS variables in
  `style.css`; both themes ship via `prefers-color-scheme`.
