# API Reference

All routes are served by `backend/main.py`. Every `/api/*` route except
`/api/login` requires the `icp_session` cookie (401 otherwise). Interactive
docs (`/docs`, `/openapi.json`) are deliberately disabled.

## Pages & health

| Route | Notes |
|---|---|
| `GET /` | Dashboard (redirects to `/login` when unauthenticated) |
| `GET /login` | Sign-in page (redirects to `/` when authenticated) |
| `GET /health` | `{"ok": true, "rev": "<deployed commit sha>"}` — unauthenticated, for Railway checks and deploy verification (`rev` is `"dev"` locally) |
| `GET /static/*` | CSS/JS assets — unauthenticated (no data) |

## Auth

### `POST /api/login`
Body `{"username": "...", "password": "..."}`. Checks the `users` table
(disabled users can't log in); the `OWNER_USERNAME` + `DASHBOARD_PASSWORD_HASH`
break-glass login creates/re-enables the admin account. On success sets the
`icp_session` cookie (carries the user id; HTTP-only, SameSite=Lax, Secure
unless `DEV_MODE`, 30 days) and returns `{"ok": true, "username": "..."}`.
Wrong credentials → 401. 10 failures for a username (or 50 overall) → 429
for 15 minutes.

### `GET /api/me`
`{"username": "vaughn", "is_admin": true, "can_spend": true}` — the frontend
uses it to show the Admin tab and disable spend-gated buttons.

### `POST /api/logout`
Clears the cookie.

## Analytics (read-only)

All five analytics/contacts endpoints accept `?scope=all|job|work`
(default `all`; anything else → 400). `job` = rows where `purpose` is NULL
or ≠ `'work'` (all history predates the column), `work` = `purpose =
'work'` rows created from the Work tab. The frontend threads the topbar
scope toggle through every call.

### `GET /api/stats`
```json
{
  "overall":    {"contacted": 509, "clicked": 99, "responded": 0,
                 "click_rate": 0.1945, "response_rate": 0.0},
  "by_channel": [{"channel": "LinkedIn DM", ...same fields...}]
}
```

### `GET /api/breakdown?dim=<dimension>`
`dim` ∈ `seniority | company_size | industry | connection_degree | country |
target_role | premium | channel` (whitelist; anything else → 400).
Returns `{"dim": ..., "groups": [{"grp": "director", "contacted": 34,
"clicked": 14, "responded": 0, "click_rate": ..., "response_rate": ...}]}`
ordered by group size. NULLs appear as `"Unknown"`.

### `GET /api/timeseries?granularity=week|month`
Buckets by `date_trunc` on `contacted_at` (NULL `contacted_at` rows are
excluded). Returns `{"granularity": ..., "periods": [{"period":
"2026-03-02T00:00:00+00:00", "contacted": ..., "clicked": ...,
"responded": ..., ...rates}]}`.

### `GET /api/icp?dims=a,b,c&min_n=8&metric=click|response`
Groups by the given dimension combination (same whitelist as breakdown;
comma-separated, deduped), drops groups with `n < min_n` (`HAVING`), ranks
by the chosen metric, returns top 50. Response groups carry one key per
requested dimension plus the count/rate fields.

### `GET /api/contacts`
`{"contacts": [...]}` — every row in scope with display fields (including
`purpose`), `visit_count`, and `last_visit`. Ordered by `contacted_at`
desc (nulls last).

### `GET /api/enrich-meta`
Suggestion data for the Enrich tab:
```json
{
  "orgs": [{"company_name": "Glean", "company_size": 1500,
             "company_industry": "...", "n": 44}],
  "seniorities": [...], "industries": [...], "countries": [...], "titles": [...]
}
```
Org size/industry are the `mode()` (most common value) across that company's
rows — the source of the autofill.

## The only DB write path

### `PATCH /api/contacts/{uid}`
Partial update; send only the fields to change. Accepted fields:

- **Outcome recording**: `responded` (bool), `responded_at` (datetime),
  `outcome` (`call|referral|ghost|rejected|other` or null).
  Semantics: `responded: true` stamps `responded_at = now()` unless already
  true or a value is supplied; `responded: false` clears it; `responded_at`
  alone backfills the date.
- **Enrichment** (mirrors the `icp_lab` role's grants): `first_name`,
  `last_name`, `title`, `seniority`, `departments`, `company_name`,
  `company_size` (int), `company_industry`, `city`, `state`, `country`,
  `years_at_company` (float), `email_status`, `premium` (bool),
  `follower_count` (int), `connection_degree` (`1st|2nd|3rd`),
  `target_role`, `target_company`, `channel` (`copy|email`), `contacted_at`.
  Empty/whitespace strings normalize to NULL. Unknown fields are ignored
  (`linkedin_url` can never be written — not in the model, not in the SQL
  whitelist, not in the DB grants).

Every non-empty update also sets `updated_by` (the signed-in user) and
`updated_at = now()`. Returns `{"contact": {...updated row...,
"updated_by_name": "..."}}`. 404 for unknown uid, 400 for invalid enum
values, 422 for type errors.

## Work tab

### `GET /api/work-settings`
The signed-in user's persisted Work-tab settings (admins fall back to the
pre-multi-user shared `work` row) merged over defaults: `{"persona", "locations",
"exclusions", "template", "table_ready"}`. `table_ready: false` means the
one-time `app_settings` SQL hasn't been run — the tab still works, settings
just don't persist.

### `PUT /api/work-settings`
Body: same four fields. Stored per user as one JSONB row (`key =
'work:<user_id>'`, `updated_by` stamped) in `app_settings`. 503 if the
table is missing.

### `POST /api/work-search`
Body: `{"persona": "commercial insurance broker", "locations": ["United
States", "Canada"], "exclusions": ["..."], "per_page": 25, "page": 1,
"titles": null}`. Claude expands the persona into LinkedIn title strings
(forced tool call, same pattern as jd-search), then one page of Apollo
`mixed_people/api_search` — company-less, so no credits. Companies matching
an exclusion term (case-insensitive substring on the unobfuscated org name)
are dropped before display. Load more passes the returned `titles` back to
skip the re-parse. Returns `{"titles", "total_entries", "page", "per_page",
"excluded_count", "people": [{id, name, title, company_name, linkedin_url
(null pre-reveal), linkedin_search_url, country, seniority, known}]}`.
Reveal reuses `POST /api/prospect-reveal`.

## Prospect tab

### `POST /api/jd-search`
Body `{"job_description": "...", "per_category": 15}` (`per_category`
optional, clamped to 1–25 — the max people each Apollo search returns).
Runs the full pipeline (Claude parse → 4× Apollo search → scoring → DB
cross-check). ~8s typical. **No Apollo credits consumed.**
```json
{
  "parsed": {"company_name": ..., "role_title": ..., "department": ...,
              "seniority": ..., "search_titles_peer": [...], ...},
  "company_profile": {"company_size": ..., "size_bucket": "51-200",
              "industry": ..., "overall_click_rate": 0.295,
              "size_history": {"segment": ..., "n": ..., "rate": ...},
              "industry_history": {...}, "fit_lift": 0.86},
  "categories": [{"key": "peer", "label": "Peers", "people": [{
      "id": "...", "name": "Jake Ve.", "title": ..., "linkedin_url": null,
      "linkedin_search_url": "...", "country": null, "seniority": "entry",
      "score": {"expected_click_rate": 0.37, "pct": 37, "tier": "strong",
                 "parts": [{"dim": "seniority", "segment": "entry",
                            "n": 163, "rate": 0.318}, ...]},
      "known": {"uid": "a1x", "name": "...", "fuzzy": true,
                 "clicked": true, "responded": false, "outcome": null} | null
  }]}]
}
```
Note: Apollo's `api_search` returns obfuscated results on this API key
(masked last names, no `linkedin_url`, no org data) — hence the reveal flow.

### `POST /api/prospect-reveal`
Body `{"id": "<apollo person id>"}`. **Spends 1 Apollo credit** — only ever
triggered by the explicit Reveal button. Calls Apollo `people/match` and
returns the card replacement: real `name`, `linkedin_url`, `country`,
re-computed `score`, exact-name `known` match, `"revealed": true`.

## Reply scanner

All hidden (404-free, but empty/`configured: false`) until the Gmail env
vars are set — see [operations.md](operations.md#reply-scanner-setup).
**Admin-only**: non-admin sessions get `configured: false` from `GET
/api/replies` (hiding the card) and 403 from scan/confirm/dismiss.

### `GET /api/replies`
State for the Replies card on the Contacts tab:
```json
{
  "configured": true, "table_ready": true,
  "last_scan": "2026-07-21T18:02:11+00:00",
  "pending": [{"gmail_id": "18c...", "sender_name": "Jake Verano",
      "received_at": "...", "snippet": "Hey Vaughn, thanks for...",
      "candidates": [{"uid": "a1x", "name": "Jake Verano",
                       "company": "Glean", "responded": false}]}],
  "recent_auto": [{"gmail_id": "...", "sender_name": "...",
      "received_at": "...", "matched_uid": "b2y",
      "matched_name": "...", "status": "auto_applied"}]
}
```

### `POST /api/replies/scan`
Body `{"days": 30, "auto": false}` (days clamped to 1–365). Searches Gmail
for LinkedIn message notifications, processes ones not yet in
`reply_events`, and **auto-applies exact single-contact name matches**
(sets `responded = true`, `responded_at` = the email's date; never restamps
an already-responded contact). Ambiguous matches become `pending`.
`auto: true` (the quiet page-load trigger) is throttled server-side to one
scan per 30 minutes and returns `{"throttled": true}` otherwise. Returns:
```json
{"throttled": false, "new_events": 4,
 "auto_applied": [{"sender_name": "Jake Verano", "uid": "a1x"}],
 "pending_new": 1, "no_match_new": 1, "ignored_new": 1}
```
503 with a human-readable reason when Gmail isn't configured, the token is
revoked, or `reply_events` doesn't exist yet.

### `POST /api/replies/{gmail_id}/confirm`
Body `{"uid": "a1x"}`. Applies a pending event to the chosen contact (same
responded/responded_at semantics as the scan) and marks it `confirmed`.
404 if the event isn't pending; 400 for an unknown uid.

### `POST /api/replies/{gmail_id}/dismiss`
Marks a pending event `dismissed` (no contact write). 404 if not pending.

## outreach-backend proxies

Contact creation and contacted-stamping go through outreach-backend so uid
generation stays in one place. These are thin authenticated proxies (the
browser can't call outreach-backend cross-origin).

### `POST /api/outreach-contact`
Body: `{"first_name": "...", "last_name": ..., "linkedin_url": ...,
"target_role": ..., "target_company": ..., "purpose": ...}` (all but
first_name optional; the Work tab sends `purpose: "work"`, which
outreach-backend applies set-if-unset so re-copies never reclassify).
Forwards to `POST {OUTREACH_BACKEND_URL}/contacts` — which **upserts,
deduping by `linkedin_url`** — and returns `{"uid": "vc9",
"tracking_url": "https://vaughneugenio.com/r/vc9"}`.

### `POST /api/outreach-contacted`
Body `{"uid": "vc9"}`. Forwards `{"channel": "copy"}` to
`POST {OUTREACH_BACKEND_URL}/contacts/{uid}/contacted`, which sets
`channel` and stamps `contacted_at` (re-stamps on repeat — a known caveat
of the whole pipeline). Returns `{"ok": true}`.

Both proxies stamp the signed-in user onto the contact after outreach-backend
succeeds: `created_by` (set-if-unset) + `updated_by`/`updated_at` for
`outreach-contact`, `updated_by`/`updated_at` for `outreach-contacted`.

## Spend gating

`POST /api/jd-search`, `POST /api/work-search` and `POST /api/prospect-reveal`
require the user's `can_spend` flag (403 `"Spending not enabled for this
account"` otherwise). Each call logs to `usage_events`: a `claude` row
(tokens + model) when Claude ran, an `apollo_search` row (with the number of
Apollo calls), and for Reveal an `apollo_reveal` row with `credits: 1` —
only when the reveal succeeded.

## Admin (admin sessions only; 403 otherwise)

### `GET /api/admin/users`
`{"users": [{"id", "username", "is_admin", "can_spend", "disabled_at",
"created_at", "last_login_at", "created_by_name", "contacts_created",
"contacts_last_edited"}]}`.

### `POST /api/admin/users`
Body `{"username": "sam", "password": "...", "can_spend": false}`. Username
is lowercased and must match `^[a-z0-9._-]{3,32}$`; password ≥ 10 chars.
409 if the username is taken. Returns `{"user": {...}}`.

### `PATCH /api/admin/users/{id}`
Body any of `{"can_spend": bool, "disabled": bool, "password": "..."}`.
Disabling sets `disabled_at` (the user is signed out on their next
request); you can't disable yourself. Returns `{"user": {...}}`.

### `GET /api/admin/usage?days=30`
`days` ≤ 0 = all time. Returns `{"days": 30, "totals": [{"user_id",
"username", "apollo_credits", "reveals", "searches", "claude_calls",
"input_tokens", "output_tokens", "claude_cost_usd", "last_used"}],
"daily": [{"day", "username", "apollo_credits", "claude_calls",
"searches"}]}`. `claude_cost_usd` is an estimate at Haiku 4.5 list prices
($1 / $5 per million input / output tokens).
