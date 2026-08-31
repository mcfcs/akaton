from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from akaton.domain.models import CandidateSeed
from akaton.fetch.http import DEFAULT_HEADERS
from akaton.fetch.manager import FetchManager


class DevpostAdapter:
    """Devpost's JSON API, asked for Philippine hackathons specifically.

    The listing page it used to scrape was `/hackathons?status=open`, which is every
    open hackathon on the site regardless of country, so a run mostly produced events
    the profile could never enter.

    `challenge_type[]=online` was the same mistake in a narrower form: every open online
    hackathon in the world, hundreds of them, and the single highest-volume lowest-
    precision producer in a run. An online hackathon a Filipino can actually join still
    arrives here when it names the country, and through the search rotation otherwise.
    """

    name = "devpost"
    endpoint = "https://devpost.com/api/hackathons"
    queries: tuple[dict[str, str], ...] = (
        {"search": "philippines"},
        {"search": "manila"},
        {"search": "filipino"},
    )

    def __init__(self, fetcher: FetchManager, *, client: httpx.AsyncClient | None = None) -> None:
        self.fetcher = fetcher
        self.client = client

    async def discover(
        self, since: datetime | None = None, cursor: str | None = None
    ) -> list[CandidateSeed]:
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=30, headers=DEFAULT_HEADERS)
        seeds: list[CandidateSeed] = []
        try:
            for query in self.queries:
                params = {"status[]": "open", **query}
                try:
                    response = await client.get(self.endpoint, params=params)
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                for item in payload.get("hackathons", []):
                    seed = _devpost_seed(item, self.name)
                    if seed:
                        seeds.append(seed)
        finally:
            if own_client:
                await client.aclose()
        return list({str(seed.url): seed for seed in seeds}.values())


def _devpost_seed(item: dict, provider: str) -> CandidateSeed | None:
    url = item.get("url")
    if not url or item.get("open_state") != "open":
        return None
    location = (item.get("displayed_location") or {}).get("location")
    detail = [
        f"Location: {location}" if location else None,
        f"Submission period: {item['submission_period_dates']}"
        if item.get("submission_period_dates")
        else None,
        f"Organizer: {item['organization_name']}" if item.get("organization_name") else None,
    ]
    try:
        return CandidateSeed(
            url=url,
            title=item.get("title"),
            snippet="; ".join(part for part in detail if part) or None,
            discovery_channel="structured",
            provider=provider,
            source_key=str(item.get("id") or url),
        )
    except ValueError:
        return None


class KaggleAdapter:
    name = "kaggle"

    async def discover(
        self, since: datetime | None = None, cursor: str | None = None
    ) -> list[CandidateSeed]:
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi
        except ImportError:
            return []

        def load() -> list:
            api = KaggleApi()
            api.authenticate()
            return api.competitions_list(sort_by="recentlyCreated")

        competitions = await asyncio.to_thread(load)
        seeds = []
        for item in competitions[:50]:
            ref = getattr(item, "ref", None)
            if not ref:
                continue
            deadline = getattr(item, "deadline", "")
            reward = getattr(item, "reward", "")
            seeds.append(
                CandidateSeed(
                    url=f"https://www.kaggle.com/competitions/{ref}",
                    title=getattr(item, "title", None),
                    snippet=f"Deadline: {deadline}; Reward: {reward}",
                    discovery_channel="structured",
                    provider=self.name,
                    source_key=ref,
                )
            )
        return seeds
