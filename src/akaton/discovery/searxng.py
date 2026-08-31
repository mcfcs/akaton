from __future__ import annotations

from html import unescape

import httpx

from akaton.discovery.base import SearchPage, SearchRequest
from akaton.domain.models import CandidateSeed

# Engines that actually carry a result set. SearXNG lists a dozen more, but the rest are
# dictionaries, wikis and niche indexes that return nothing for a query like this — so
# their silence says nothing about whether the search ran.
PRIMARY_ENGINES = frozenset(
    {
        "bing",
        "brave",
        "duckduckgo",
        "duckduckgo web",
        "google",
        "google cse",
        "mojeek",
        "qwant",
        "startpage",
    }
)


class SearXNGSearchProvider:
    """Search through a private SearXNG instance without a paid API key."""

    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("SEARXNG_BASE_URL must be an HTTP(S) URL")
        self.endpoint = f"{base_url.rstrip('/')}/search"
        self.client = client

    async def search(self, request: SearchRequest) -> SearchPage:
        params = {
            "q": request.query,
            "format": "json",
            "language": f"{request.search_lang}-{request.country}",
            "safesearch": 1,
            "pageno": request.offset // request.count + 1,
        }
        time_range = _time_range(request.freshness)
        if time_range:
            params["time_range"] = time_range
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=30)
        try:
            response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
            body = response.json()
        finally:
            if own_client:
                await client.aclose()

        results: list[CandidateSeed] = []
        for item in body.get("results", [])[: request.count]:
            url = item.get("url")
            if not url:
                continue
            try:
                results.append(
                    CandidateSeed(
                        url=url,
                        title=_clean(item.get("title")),
                        snippet=_clean(item.get("content")),
                        discovery_channel="search",
                        provider=self.name,
                        query=request.query,
                        published_hint=item.get("publishedDate") or item.get("published_date"),
                    )
                )
            except (TypeError, ValueError):
                continue
        unresponsive = _unresponsive(body)
        return SearchPage(
            results=results,
            unresponsive_engines=unresponsive,
            degraded=_degraded(unresponsive),
        )


def _engine_name(entry: str) -> str:
    return entry.split(":", 1)[0].strip().casefold()


def _degraded(unresponsive: list[str]) -> bool:
    """True only when no engine capable of answering did.

    Some engines are almost always suspended — a run routinely reports six unresponsive
    while still returning fifty results from the rest. Treating "any engine down" as a
    failure reported a query that genuinely has no matches as a broken backend, which is
    what made 28 of 33 recorded searches look failed.
    """
    down = {_engine_name(entry) for entry in unresponsive}
    return bool(PRIMARY_ENGINES) and PRIMARY_ENGINES <= down


def _unresponsive(body: dict) -> list[str]:
    entries = []
    for entry in body.get("unresponsive_engines") or []:
        if isinstance(entry, (list, tuple)):
            entries.append(": ".join(str(part) for part in entry if part))
        elif entry:
            entries.append(str(entry))
    return entries


def _time_range(freshness: str | None) -> str | None:
    return {
        "pd": "day",
        "pw": "week",
        "pm": "month",
        "py": "year",
    }.get(freshness or "")


def _clean(value: object) -> str | None:
    return unescape(str(value)).strip() if value else None
