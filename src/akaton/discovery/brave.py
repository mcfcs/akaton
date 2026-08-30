from __future__ import annotations

import httpx

from akaton.discovery.base import SearchPage, SearchRequest
from akaton.domain.models import CandidateSeed


class BraveSearchProvider:
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required")
        self.api_key = api_key
        self.client = client

    async def search(self, request: SearchRequest) -> SearchPage:
        params = {
            "q": request.query,
            "country": request.country,
            "search_lang": request.search_lang,
            "count": request.count,
            "offset": request.offset,
            "safesearch": "moderate",
            "spellcheck": "true",
            "result_filter": "web",
            "text_decorations": "false",
        }
        if request.freshness:
            params["freshness"] = request.freshness
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=20)
        try:
            response = await client.get(
                self.endpoint,
                params=params,
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
        finally:
            if own_client:
                await client.aclose()
        results = []
        for item in body.get("web", {}).get("results", []):
            url = item.get("url")
            if not url:
                continue
            try:
                results.append(
                    CandidateSeed(
                        url=url,
                        title=item.get("title"),
                        snippet=item.get("description"),
                        discovery_channel="search",
                        provider=self.name,
                        query=request.query,
                    )
                )
            except ValueError:
                continue
        return SearchPage(results=results)
