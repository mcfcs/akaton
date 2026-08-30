from __future__ import annotations

import asyncio
from datetime import datetime

from akaton.domain.models import CandidateSeed
from akaton.fetch.manager import FetchManager


class DevpostAdapter:
    name = "devpost"
    listing_url = "https://devpost.com/hackathons?status=open"

    def __init__(self, fetcher: FetchManager) -> None:
        self.fetcher = fetcher

    async def discover(
        self, since: datetime | None = None, cursor: str | None = None
    ) -> list[CandidateSeed]:
        result = await self.fetcher.fetch(self.listing_url)
        if not result.usable:
            return []
        seeds: list[CandidateSeed] = []
        for link in result.links:
            if ".devpost.com" not in link or any(
                part in link for part in ("/software", "/updates", "/participants")
            ):
                continue
            try:
                seeds.append(
                    CandidateSeed(
                        url=link,
                        discovery_channel="structured",
                        provider=self.name,
                        source_key=link,
                    )
                )
            except ValueError:
                continue
        return list({str(seed.url): seed for seed in seeds}.values())


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
