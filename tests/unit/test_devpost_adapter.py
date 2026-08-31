from __future__ import annotations

import httpx

from akaton.discovery.adapters import DevpostAdapter

OPEN_ONLINE = {
    "id": 1,
    "title": "Global AI Hackathon",
    "url": "https://global-ai.devpost.com/",
    "open_state": "open",
    "displayed_location": {"location": "Online"},
    "submission_period_dates": "Aug 04 - 31, 2026",
    "organization_name": "Example Org",
}
ENDED = {
    "id": 2,
    "title": "StackHack Manila",
    "url": "https://stackhackmanila.devpost.com/",
    "open_state": "ended",
    "displayed_location": {"location": "Manila, Philippines"},
}


async def test_only_open_hackathons_become_candidates():
    def handle(request: httpx.Request) -> httpx.Response:
        # Every query must ask Devpost for open hackathons rather than the whole site.
        assert request.url.params.get("status[]") == "open"
        return httpx.Response(200, json={"hackathons": [OPEN_ONLINE, ENDED]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        seeds = await DevpostAdapter(None, client=client).discover()

    urls = {str(seed.url) for seed in seeds}
    assert urls == {"https://global-ai.devpost.com/"}


async def test_every_query_names_the_country_rather_than_the_whole_catalogue():
    """`challenge_type[]=online` asked for every open online hackathon on earth."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"hackathons": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await DevpostAdapter(None, client=client).discover()

    joined = " ".join(seen).casefold()
    assert "philippines" in joined
    assert "manila" in joined
    assert "challenge_type" not in joined
    assert all(request.casefold().count("search=") == 1 for request in seen)


async def test_metadata_is_carried_into_the_snippet():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hackathons": [OPEN_ONLINE]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        seeds = await DevpostAdapter(None, client=client).discover()

    snippet = seeds[0].snippet or ""
    assert "Online" in snippet
    assert "Aug 04 - 31, 2026" in snippet
    assert seeds[0].title == "Global AI Hackathon"


async def test_a_failing_query_does_not_lose_the_others():
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"hackathons": [OPEN_ONLINE]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        seeds = await DevpostAdapter(None, client=client).discover()

    assert len(seeds) == 1
