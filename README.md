# icp-lab

Single-user analytics + prospecting dashboard for my LinkedIn job-search
outreach. It answers one question — **who actually responds to my outreach?**
— and then uses the answer to find and rank new people to contact.

icp-lab reads the same Neon PostgreSQL database as
[outreach-backend](https://github.com/VDEugenio/outreach-backend) (the FastAPI
service behind my Chrome extension's tracking links). It never creates
contacts itself and can only write three-plus-enrichment columns, enforced
both in code and by a least-privilege Postgres role.

## The tabs

| Tab | What it does |
|---|---|
| **Overview** | Headline stats, contacted → clicked → responded funnel, channel split, response rate over time |
| **Breakdowns** | Click/response rates by seniority, company size, industry, connection degree, country, target role, premium, channel — always with sample sizes |
| **ICP finder** | Pick any combination of dimensions; every combo in the data ranked by click or response rate, low-n groups excluded |
| **Enrich** | Manual data entry for contacts Apollo couldn't enrich — work queue sorted by missing fields, org autofill from companies already in the DB |
| **Prospect** | Paste a job description → Claude extracts the company/role → Apollo finds people in four categories → each person scored against my own click history → reveal + copy outreach message with tracking link |
| **Contacts** | Full table with inline editing of responded / responded-at / outcome |

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System context, folder layout, auth design, database access & the `icp_lab` role, connection pooling |
| [docs/features.md](docs/features.md) | Every tab in depth, the ICP scoring methodology, product decisions and their rationale |
| [docs/api.md](docs/api.md) | Every endpoint: methods, parameters, request/response shapes, write-path rules |
| [docs/operations.md](docs/operations.md) | Local dev setup, environment variables, Railway deploy runbook, external-service notes (Apollo, Anthropic, outreach-backend), troubleshooting |

## Quickstart

```
pip install -r requirements.txt
copy .env.example .env        # fill in — see docs/operations.md
python -m uvicorn backend.main:app --reload
```

Open http://localhost:8000 and sign in with your dashboard password.

## Stack

- **Backend**: FastAPI + psycopg2, Python 3.12 (`backend/`)
- **Frontend**: static HTML/CSS/vanilla JS, no build step (`frontend/`), served by the same app
- **Database**: Neon PostgreSQL (shared with outreach-backend), read via the restricted `icp_lab` role
- **External**: Anthropic API (Claude Haiku 4.5, JD parsing), Apollo.io (people search + reveal), outreach-backend (contact creation + tracking links)
- **Deploy**: one Railway service — Procfile + requirements.txt + runtime.txt
