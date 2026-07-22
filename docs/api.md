# API Reference

All routes are served by `backend/main.py`. Every `/api/*` route except
`/api/login` requires the `icp_session` cookie (401 otherwise). Interactive
docs (`/docs`, `/openapi.json`) are deliberately disabled.

## Pages & health

| Route | Notes |
|---|---|
| `GET /` | Dashboard (redirects to `/login` when unauthenticated) |
| `GET /login` | Sign-in page (redirects to `/` when authenticated) |
| `GET /health` | `{"ok": true}` — unauthenticated, for Railway checks |
| `GET /static/*` | CSS/JS assets — unauthenticated (no data) |

## Auth

### `POST /api/login`
Body `{"password": "..."}`. On success sets the `icp_session` cookie
(HTTP-only, SameSite=Lax, Secure unless `DEV_MODE`, 30 days) and returns
`{"ok": true}`. Wrong password → 401. After 10 consecutive failures → 429
for 15 minutes.

### `POST /api/logout`
Clears the cookie.

## Analytics (read-only)

### `GET /api/stats`
```json
{
  "overall":    {"contacted": 509, "clicked": 150, "responded": 0,
                 "click_rate": 0.2947, "response_rate": 0.0},
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
`{"contacts": [...]}` — every row with display fields, `visit_count`, and
`last_visit`. Ordered by `contacted_at` desc (nulls last).

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

Returns `{"contact": {...updated row...}}`. 404 for unknown uid, 400 for
invalid enum values, 422 for type errors.

## Prospect tab

### `POST /api/jd-search`
Body `{"job_description": "..."}`. Runs the full pipeline (Claude parse →
4× Apollo search → scoring → DB cross-check). ~8s typical. **No Apollo
credits consumed.**
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
"target_role": ..., "target_company": ...}` (all but first_name optional).
Forwards to `POST {OUTREACH_BACKEND_URL}/contacts` — which **upserts,
deduping by `linkedin_url`** — and returns `{"uid": "vc9",
"tracking_url": "https://vaughneugenio.com/r/vc9"}`.

### `POST /api/outreach-contacted`
Body `{"uid": "vc9"}`. Forwards `{"channel": "copy"}` to
`POST {OUTREACH_BACKEND_URL}/contacts/{uid}/contacted`, which sets
`channel` and stamps `contacted_at` (re-stamps on repeat — a known caveat
of the whole pipeline). Returns `{"ok": true}`.
