"""Work-tab prospect finder: expand a persona ("commercial insurance broker")
into Apollo title strings with Claude, run a company-less people search, and
filter out excluded companies (existing clients) before anything costs a
credit.

Differences from jd_finder (the job-search flow): no company scope, no four
categories, no fit scoring (deliberately off until there's enough work-row
history to train on — see docs/features.md), and pagination for Load more.
Reveal reuses jd_finder.reveal_person via the same /api/prospect-reveal
endpoint.

Env vars: ANTHROPIC_API_KEY, APOLLO_API_KEY. Search costs no Apollo credits.
"""
import asyncio

import httpx
from anthropic import APIError
from fastapi import HTTPException

from jd_finder import (
    APOLLO_BASE,
    APOLLO_TIMEOUT,
    CLAUDE_MODEL,
    _apollo_headers,
    _claude,
    _display_name,
    _linkedin_search_url,
    infer_seniority,
    known_contacts_index,
    match_known,
)

MAX_PER_PAGE = 50

PERSONA_TOOL = {
    "name": "expand_persona",
    "description": "Turn a prospect persona into Apollo people-search title strings.",
    "input_schema": {
        "type": "object",
        "properties": {
            "search_titles": {
                "type": "array", "items": {"type": "string"},
                "description": "4-8 LinkedIn title strings people matching this persona would actually put on their profile, including realistic variants (e.g. for 'commercial insurance broker': 'Commercial Insurance Broker', 'Commercial Lines Producer', 'Commercial Lines Account Executive'). Titles are OR-matched; keep them specific enough to exclude adjacent roles the persona doesn't cover.",
            },
        },
        "required": ["search_titles"],
    },
}

PERSONA_SYSTEM = """You are a B2B prospecting assistant. Given a short persona describing who the user wants to reach on LinkedIn, generate realistic LinkedIn title strings for an Apollo people search. Think about what these people actually put in their profile headline, not generic catch-alls. If the persona includes qualifiers (like an industry segment or specialty), bake them into the titles where people would genuinely include them, rather than dropping them. Always call the expand_persona tool."""


async def expand_persona(persona: str) -> list:
    try:
        resp = await _claude().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=PERSONA_SYSTEM,
            tools=[PERSONA_TOOL],
            tool_choice={"type": "tool", "name": "expand_persona"},
            messages=[{"role": "user", "content": f"Persona:\n\n{persona}"}],
        )
    except APIError as e:
        raise HTTPException(502, f"Claude API error: {getattr(e, 'message', str(e))}")
    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise HTTPException(502, "Claude did not return the expected tool call")
    titles = [t.strip() for t in tool_use.input.get("search_titles", []) if t and t.strip()]
    if not titles:
        raise HTTPException(502, "Claude returned no search titles for this persona")
    return titles


def _excluded(org_name: str, exclusions: list) -> bool:
    """Case-insensitive substring match: 'USI' excludes 'USI Insurance
    Services'. Org names come back unobfuscated in the free search, so this
    runs before any credit is spent."""
    hay = (org_name or "").strip().lower()
    if not hay:
        return False
    return any(term.strip().lower() in hay for term in exclusions if term.strip())


async def search(persona: str, locations: list, exclusions: list,
                 per_page: int, page: int, titles: list | None = None) -> dict:
    """One page of work prospects. titles=None on the first call (Claude
    expands the persona); Load more passes the parsed titles back to skip
    the re-parse."""
    if titles is None:
        titles = await expand_persona(persona)

    payload = {
        "person_titles": titles,
        "per_page": per_page,
        "page": page,
    }
    if locations:
        payload["person_locations"] = locations

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"{APOLLO_BASE}/mixed_people/api_search",
                json=payload, headers=_apollo_headers(), timeout=APOLLO_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Apollo network error: {type(e).__name__}")
    if r.status_code >= 400:
        raise HTTPException(502, f"Apollo search failed ({r.status_code}): {r.text[:200]}")
    data = r.json()
    people = data.get("people") or []

    from starlette.concurrency import run_in_threadpool

    known = await run_in_threadpool(known_contacts_index)

    cards = []
    excluded_count = 0
    for p in people:
        org = (p.get("organization") or {}).get("name") or ""
        if _excluded(org, exclusions):
            excluded_count += 1
            continue
        match, fuzzy = match_known(p, known, org)
        cards.append({
            "id": p.get("id"),
            "name": _display_name(p),
            "title": p.get("title"),
            "company_name": org or None,
            "linkedin_url": p.get("linkedin_url"),  # always None pre-reveal
            "linkedin_search_url": _linkedin_search_url(p, org),
            "country": p.get("country"),
            "seniority": infer_seniority(p),
            "known": {
                "uid": match["uid"],
                "name": f"{match['first_name']} {match['last_name'] or ''}".strip(),
                "fuzzy": fuzzy,
                "responded": match["responded"] is True,
                "outcome": match["outcome"],
            } if match else None,
        })

    return {
        "titles": titles,
        "total_entries": data.get("total_entries"),
        "page": page,
        "per_page": per_page,
        "excluded_count": excluded_count,
        "people": cards,
    }
