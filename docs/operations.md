# Operations

## Environment variables

| Var | Required | What |
|---|---|---|
| `DATABASE_URL` | ✅ | Neon connection string **for the `icp_lab` role** (not the owner). Format: `postgresql://icp_lab:<pw>@<host>/neondb?sslmode=require&channel_binding=require` |
| `DASHBOARD_PASSWORD_HASH` | ✅ | Output of `python backend/hash_password.py` (PBKDF2-SHA256, 600k iterations) |
| `SESSION_SECRET` | ✅ | Long random string signing session cookies: `python -c "import secrets; print(secrets.token_hex(32))"` — use a *different* value in prod so local and prod sessions are independent |
| `ANTHROPIC_API_KEY` | for Prospect | Claude API key (JD parsing, Haiku 4.5 — pennies per search) |
| `APOLLO_API_KEY` | for Prospect | Apollo.io API key (people search free; reveal 1 credit/person) |
| `OUTREACH_BACKEND_URL` | optional | Defaults to the production Railway URL of outreach-backend |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` | for reply scanner | Google Cloud **Desktop app** OAuth client (see setup below) |
| `GMAIL_REFRESH_TOKEN` | for reply scanner | Output of `python backend/gmail_auth.py` (read-only Gmail scope). All three unset → the feature hides itself |
| `DEV_MODE` | local only | Any value → session cookie works over plain http. **Never set in production.** |

Secrets hygiene: `.gitignore` covers `.env` (and `.env.*` except
`.env.example`) from the first commit. Never put credentials in committed
files; the repo is public. Keep only the *hash* of the dashboard password
anywhere on disk.

## Local development

```
pip install -r requirements.txt
copy .env.example .env      # then fill it in
python backend/hash_password.py          # → DASHBOARD_PASSWORD_HASH
python -m uvicorn backend.main:app --reload
```

(`uvicorn` bare may not be on PATH on Windows — use `python -m uvicorn`.)
Set `DEV_MODE=1` locally or the login cookie won't survive plain http.

## Database role setup (one-time, already done)

The `icp_lab` role must be created **via SQL** (Neon console-created roles
get `neon_superuser`, defeating the point). Full statement in
[architecture.md](architecture.md#database-access--defense-in-depth).
To verify grants behave: reads succeed, granted-column updates succeed,
`linkedin_url`/`uid`/`apollo_raw` updates and any INSERT/DELETE are denied
with `InsufficientPrivilege`.

## Reply scanner setup

Two one-time steps, then the Replies card appears on the Contacts tab.

**1. Google OAuth credentials + refresh token**

Reusing an existing project that already has the Gmail API enabled and a
consent screen configured (this is what was actually done — the Job
Application Tracker project) skips straight to step 3; any Desktop-app
OAuth client in that project works, and minting a new refresh token does
not invalidate the other app's tokens.

1. [Google Cloud Console](https://console.cloud.google.com/) → create (or
   reuse) a project → **APIs & Services → Library** → enable **Gmail API**.
2. **APIs & Services → OAuth consent screen**: External, add
   vaughndde@gmail.com as a **test user** (Testing mode is fine — it's a
   single-user app; no verification needed).
3. **Credentials → Create credentials → OAuth client ID → Desktop app.**
   Put the client id/secret in `.env` as `GMAIL_CLIENT_ID` /
   `GMAIL_CLIENT_SECRET`.
4. Run `python backend/gmail_auth.py` — browser consent for **read-only**
   Gmail access → paste the printed `GMAIL_REFRESH_TOKEN` into `.env` and
   the Railway variables (along with the id/secret).

Note: refresh tokens for consent screens left in **Testing** mode expire
after 7 days. To make it permanent, publish the consent screen
(**Publish app** — no verification is required for the app to keep working
with your own account; Google just shows an "unverified" interstitial
during the one-time consent).

**2. The state table** — run once as the database owner (the `icp_lab`
role can't CREATE TABLE): the `CREATE TABLE reply_events` + `GRANT`
statement in [architecture.md](architecture.md#the-reply_events-table-icp-labs-own-state).

Verify: reload the dashboard → Contacts tab → the Replies card shows, and
**Scan Gmail (6 mo)** reports scanned/auto-applied/pending counts.

## Deploying to Railway

Same pattern as outreach-backend: Procfile + requirements.txt + runtime.txt.

1. Push the repo to GitHub (`VDEugenio/icp-lab`, public).
2. Railway → New Service → Deploy from GitHub repo.
3. Set variables: `DATABASE_URL` (icp_lab role), `DASHBOARD_PASSWORD_HASH`,
   `SESSION_SECRET` (fresh one), `ANTHROPIC_API_KEY`, `APOLLO_API_KEY`,
   and the three `GMAIL_*` vars if the reply scanner is set up.
   Do **not** set `DEV_MODE`.
4. Railway injects `PORT`; the Procfile binds to it. Health check: `/health`.

Notes:
- The session cookie is `Secure` in production, so the service must be
  served over https (Railway default).
- The login lockout counter is in-process; a redeploy resets it. Fine for
  single-user.
- The DB pool health-checks connections on checkout, so Neon closing idle
  connections during quiet hours doesn't produce first-request errors.

## External services

### Apollo.io
- The API key is scoped to `mixed_people/api_search` (+ `people/match`).
  `api_search` results are **obfuscated**: `last_name_obfuscated`
  ("Ve***o"), boolean `has_*` flags instead of country/org data, and no
  `linkedin_url`. This is Apollo policy, not a bug.
- **Credit spend happens in exactly one place**: `people/match` behind the
  Reveal buttons (1 credit/person; Reveal-all confirms the total first).
  Search, browsing, and scoring are free.
- The Reveal response includes country and real name → cards re-score and
  re-match against the DB.

### Anthropic
- One `claude-haiku-4-5` call per JD search (forced tool call,
  ~1k output tokens). Model choice mirrors the OutreachAssistant project.

### Gmail (reply scanner)
- Scope is `gmail.readonly` — the app can never send, modify, or delete
  mail. Revoke any time at https://myaccount.google.com/permissions.
- Scans are cheap (one search + metadata fetches for *new* notifications
  only) and idempotent; the free Gmail API quota is far beyond this usage.
- Auto page-load scans are throttled to one per 30 minutes in-process.

### outreach-backend
- `POST /contacts` upserts (dedupe key: `linkedin_url`) and returns
  `{uid, tracking_url}`; links live on `vaughneugenio.com/r/{uid}`.
- `POST /contacts/{uid}/contacted` with `{"channel": "copy"}` stamps
  `contacted_at` (overwrites on re-copy — known pipeline caveat).
- **Never insert contacts directly into the DB** — uid generation must stay
  in outreach-backend (and the icp_lab role can't INSERT anyway).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Contacts tab (or any tab) 500s on load | Historically: pool exhaustion — six parallel page-load calls vs a too-small psycopg2 pool that errors instead of queueing. Fixed by the semaphore gate in `db.py`; if it recurs, check `POOL_MAX` vs the number of page-load requests. |
| `password authentication failed for user ...` | Username/password mismatch in `DATABASE_URL` — check the *user* is `icp_lab` and the password is the role's, not the owner's. |
| First request after idle fails with SSL/connection error | Should not happen (checkout health check discards dead connections). If it does, Neon behavior changed — see `_checkout` in `db.py`. |
| Login always bounces back to `/login` locally | `DEV_MODE` not set → Secure cookie dropped over http. |
| Prospect search returns empty categories | Small company: Apollo may have only a handful of people and none match the generated titles (e.g. a 4-person startup). Not a bug — check the company on Apollo directly. |
| Prospect cards all "Reveal · 1 credit" with no direct links | Expected — Apollo obfuscates free search results; see External services. |
| `ANTHROPIC_API_KEY is not set` / `APOLLO_API_KEY is not set` | Add the keys to `.env` (local) or Railway variables, restart. |
| Replies card doesn't show on the Contacts tab | The three `GMAIL_*` env vars aren't all set — the feature hides itself when unconfigured. |
| Scan fails: "reply_events table missing" | Run the one-time CREATE TABLE + GRANT from architecture.md as the DB owner. |
| Scan fails: "Gmail token refresh failed … invalid_grant" | Refresh token revoked or expired (consent screen left in Testing mode expires tokens after 7 days — publish the app). Re-run `python backend/gmail_auth.py`. |
| A real reply wasn't caught | LinkedIn only emails a notification if you hadn't already read the DM; also invitation-accepted emails deliberately don't count. Tick the checkbox manually — the scanner is an assist, not the source of truth. |
| Copy-message warns about >300 chars | Shorten the editable Role field in the parsed bar — that value feeds the template. |

## Development conventions

- All schema knowledge lives in `queries.py`; the schema itself is owned by
  outreach-backend and **must not be altered from this repo**.
- New breakdown dimensions: add to the `DIMENSIONS` whitelist in
  `queries.py` (SQL expression) and `DIMS` in `app.js` (label) — nothing else.
- New writable columns require *both* a grant on the `icp_lab` role and an
  entry in `ENRICH_COLUMNS` + the Pydantic model; either alone fails closed.
- Frontend: no framework, no build step — keep it that way; escape all
  dynamic HTML via `esc()` or use DOM APIs.
- Commit messages document the why; this docs folder documents the what.
  When behavior changes, update the relevant doc in the same commit.
