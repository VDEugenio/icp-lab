# Features & Methodology

## Definitions used everywhere

- **Contacted** — every row in `contacts` counts (decision: no filtering of
  manual links or never-contacted rows; the n= labels make dilution visible).
- **Clicked** — the contact has ≥ 1 row in `visits` (a tracking-link click).
  Raw counts; own/test clicks are *not* filtered (decision).
- **Responded** — `responded IS TRUE` (recorded manually in this app).
- **Unknown** — NULL/empty dimension values group into an explicit "Unknown"
  bucket rather than disappearing.
- **Low n** — groups under n=8 get a "low n" badge and muted styling in
  Breakdowns; the ICP finder excludes them entirely (threshold adjustable).

Data caveats inherited from the pipeline: old rows are sparse (columns were
added over time), `contacted_at` is overwritten if a message is re-copied for
the same person, and some rows are manual links with no LinkedIn URL.

## Overview tab

- **KPI tiles**: total contacts, click rate, response rate, clicked→responded.
- **Funnel**: contacted → clicked → responded, with per-stage conversion.
- **By channel**: `copy` = LinkedIn DM, `email` = Gmail; NULL = Unknown.
- **Over time**: response-rate line + contact-volume bars, weekly/monthly
  toggle, keyed on `contacted_at` (rows without it are excluded here only).

## Breakdowns tab

Response and click rates by one dimension at a time: seniority, company size
(buckets 1-10 / 11-50 / 51-200 / 201-1000 / 1001-5000 / 5000+), industry,
connection degree, country, target role, premium, channel. Bars share one
scale within the table so lengths are comparable; every row shows n.

## ICP finder tab

Group by **any combination** of the dimensions; every combination present in
the data is ranked by click rate (or response rate — toggle). Groups below
the min-sample-size threshold (default 8) are excluded via SQL `HAVING`, so
noise can't masquerade as signal. Top 50 combinations shown.

## Enrich tab

Manual data entry for what Apollo couldn't provide.

- **Work queue** (left): contacts sorted by how many key fields are missing
  (title, seniority, company name/size/industry, country), most incomplete
  first. Filter to "needs enrichment" or all; search by name/company.
- **Editor** (right): all enrichment-granted fields. Prominent
  **Open LinkedIn** button (opens a 1250×950 window for side-by-side entry).
  Prev/Next walk the queue. Saves are diff-based — only changed fields PATCH.
- **Org autofill**: the Company field autocompletes against every company in
  the DB; when the typed name matches (case-insensitive), size + industry
  fill from that org's most common existing values (`mode()` aggregate),
  the spelling snaps to the DB's canonical form on blur, and a hint line
  reports what happened. First manual enrichment of a company teaches all
  later contacts from it.
- **Dropdowns with a Custom escape hatch** for seniority and country;
  type-ahead suggestions for title (344 distinct values) and industry.

## Prospect tab (JD → scored people)

Pipeline (modeled on the OutreachAssistant project, upgraded with scoring):

1. **Parse** — the JD goes to Claude (`claude-haiku-4-5`) with a forced tool
   call that extracts company, role, department, seniority, and realistic
   LinkedIn-title lists for four categories: peers, hiring managers,
   adjacent leaders (e.g. sales directors for an SE role), recruiters.
2. **Search** — four parallel Apollo `mixed_people/api_search` calls
   (free, no credits), deduped across categories
   (peer > manager > adjacent > recruiter priority). The **max per
   category** control (1–25, default 15) caps each search.
3. **Score** — every person gets an estimated click rate (see below).
4. **Cross-check** — results are matched against existing contacts:
   exact full-name match when Apollo returns real names, otherwise fuzzy
   (first name + company + obfuscated last-name pattern, labeled
   "Likely in your DB"). Matches show their click/response history.
5. **Reveal** (1 Apollo credit each, explicit button per card or
   Reveal-all per column with a confirm dialog) — `people/match` returns the
   real LinkedIn URL, full name, and country; the card re-scores with
   country filled in. The **auto-reveal toggle** next to the search button
   reveals every result right after the search with no further prompts —
   the toggle itself is the consent, and its label/tooltip state the cost
   (1 credit per person, up to 4 × max-per-category per search). Default off,
   resets on page reload.
6. **Copy msg + LinkedIn** — one click on a revealed card: create/dedupe the
   contact via outreach-backend (returns uid + tracking link), build the
   outreach message, copy to clipboard, open the profile window, stamp the
   contact as contacted (`channel=copy`, sets `contacted_at`).

### ICP scoring methodology

The score is an **estimated click rate**, built only from this database:

```
expected = p0 × lift(seniority) × lift(country)        clamped to [2%, 95%]

where  p0            = overall click rate (~29.5%)
       rate(seg)     = (clicks + k·p0) / (n + k)       k = 15  (shrinkage)
       lift(seg)     = rate(seg) / p0                  clamped to [0.5, 2.0]
```

- **Empirical-Bayes shrinkage** (`k = 15` pseudo-observations): a segment
  with n=3 barely moves its rate away from the average; n=300 dominates it.
  Prevents a fluky 2-for-2 segment from producing a 100% score.
- **Lift clamping** keeps any single dimension from swinging a score more
  than 2× either way.
- **Tiers**: expected ≥ 1.25×p0 → *strong*; ≤ 0.75×p0 → *weak*; else
  *average*. The badge tooltip shows each contributing segment, its shrunk
  rate, and its n — the score is never a black box.
- **Company-level fit** (size bucket + industry vs history) is reported once
  in the header, not per person — it's identical for everyone at the company.
  Apollo hides org data in free search, so it falls back to what the DB
  already knows about that company.
- Scoring currently uses **click rate** because `responded` data is still
  accumulating; the SQL supports response-rate scoring when there's enough.
- Seniority comes from Apollo's `seniority` field (same vocabulary the DB
  stores) with a title-keyword fallback (`VP of Sales` → vp, etc.).

### The outreach message

Template (exact, from the Chrome extension — do not reword):

```
Hi {firstName}!

I'm very interested in the {roleName} opening at {company} and wanted to
reach out. I'd love to hear about your experience with the company and get
any insight you're willing to share.

Would you be open to a quick chat?

Talk soon,
Vaughn
{trackingLink}
```

Rules:
- **Hard limit 300 characters** (LinkedIn connection-note ceiling). The
  parsed bar shows a live estimate; the copy-time check uses the real
  message and warns (confirm dialog) if over.
- **Role is editable** in the parsed bar — abbreviate it to fit; the edited
  value goes into the message and into `target_role` on the contact.
- Empty role or company at copy time → warning dialog, never a silent
  placeholder-filled message.
- Tracking links come back as `https://vaughneugenio.com/r/{uid}` (~31 chars).

## Reply scanner (Replies card on the Contacts tab)

`responded` used to be 100% manual. The scanner semi-automates it from the
one reliable, API-accessible trace a LinkedIn DM reply leaves: LinkedIn's
**"X sent you a message" notification emails in Gmail** (read-only scope).

How a scan works (`backend/replies.py`):

1. Gmail search, restricted by sender + subject phrases to message
   notifications only (invites, job alerts, and promos never enter the
   pipeline). Every processed message id is recorded in `reply_events`, so
   scans are idempotent.
2. The sender's name is parsed from the `"X via LinkedIn"` From header or
   the subject line. Unparseable notifications are stored as `ignored`.
3. The name is matched against contacts — normalized for case, whitespace,
   and accents (so "Núñez" matches "Nunez"), and stripped of the
   decorations LinkedIn display names carry but contact names don't:
   credential suffixes ("Amanpreet Kaur, Ph.D.", "John Smith MBA"),
   pronoun parentheticals ("(She/Her)"), and emoji. Verified against the
   real backfill — 125/125 notifications parsed:
   - **Exactly one exact full-name match → auto-applied**: `responded =
     true`, `responded_at` = the email's date. An already-responded contact
     is never restamped, so re-scans and second messages are harmless.
   - **Multiple exact matches, or near-misses only** (same first name +
     last initial, or same last name + first initial) → a **pending** row
     in the review queue with the candidates attached. A first-name-only
     parse (some subjects say just "Cole just messaged you", and digest
     Froms don't always carry the full name) never auto-applies — every
     contact sharing that first name is offered as a candidate instead.
   - **Nothing close → `no_match`**, stored so it isn't re-fetched. Every
     scan re-evaluates stored no-matches against the *current* contacts
     (locally, no Gmail calls) — so fixing a mangled name in the Enrich tab
     retroactively catches that person's old reply notifications.
4. Pending events appear in the Replies card: sender, date, snippet, a
   candidate picker, and Confirm/Dismiss. Confirm applies the same
   responded semantics; Dismiss records the decision without writing.

Scans trigger two ways: quietly on dashboard load (throttled to one per
30 minutes server-side) and via the **Scan Gmail (6 mo)** button, which
looks back 180 days for the initial backfill.

Honest limitations: a reply only gets caught if LinkedIn emailed a
notification for it (if you read the DM first, LinkedIn may not email);
invitation acceptances are deliberately *not* counted as responses; and
email-channel replies aren't detected (the DB stores no email addresses).
Those still need the manual checkbox — the scanner narrows the manual
work, it doesn't replace the write path or the human judgment.

## Contacts tab

Everyone in the DB: LinkedIn-linked names, title/company/target, channel
chip, contacted date, visit count, and the three inline-editable outcome
fields:

- **responded** checkbox — flipping on stamps `responded_at` with the current
  time (unless a date is supplied); flipping off clears it; re-saving an
  already-responded contact does *not* restamp.
- **responded at** date input — editable for backfilling late-logged replies.
- **outcome** select — `call / referral / ghost / rejected / other` (or
  clear). Legacy free-text values from before the enum display as read-only
  "(legacy)" options.

Search filters across name, company, title, target, and outcome.
