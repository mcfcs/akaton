from __future__ import annotations

import httpx

from akaton.discovery.base import SearchRequest
from akaton.discovery.searxng import SearXNGSearchProvider


async def test_searxng_provider_maps_json_results_and_freshness():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        assert request.url.params["time_range"] == "week"
        assert request.url.params["language"] == "en-PH"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.edu.ph/hackathon",
                        "title": "AI &amp; Data Hackathon",
                        "content": "Registration is open.",
                        "publishedDate": "2026-08-30T08:00:00Z",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = SearXNGSearchProvider("http://127.0.0.1:8888", client=client)
        page = await provider.search(SearchRequest(query="hackathon PH", freshness="pw"))
    assert len(page.results) == 1
    assert page.results[0].title == "AI & Data Hackathon"
    assert page.results[0].provider == "searxng"
