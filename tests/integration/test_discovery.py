from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from akaton.discovery.base import SearchPage
from akaton.domain.enums import FailureCode
from akaton.domain.models import CandidateSeed, FetchResult
from akaton.fetch.http import HttpFetcher
from akaton.fetch.manager import FetchManager
from akaton.fetch.policies import DomainPolicyResolver
from akaton.jobs.discovery import DiscoveryJob
from akaton.persistence.database import Database
from akaton.persistence.models import CandidateRow
from akaton.pipeline import CandidatePipeline

BLOCKED_CONFIG = {
    "default": {"requests_per_minute": 1},
    "domains": {"facebook.com": {"fetch": "disabled", "browser": "disabled"}},
}


class ExplodingHttpFetcher(HttpFetcher):
    async def fetch(self, url, policy, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"blocked domain was fetched over the network: {url}")


async def test_blocked_domain_never_reaches_the_network_or_the_rate_limiter():
    manager = FetchManager(ExplodingHttpFetcher(), DomainPolicyResolver(BLOCKED_CONFIG))
    started = time.monotonic()
    for index in range(5):
        result = await manager.fetch(f"https://www.facebook.com/posts/{index}")
        assert result.failure is FailureCode.FETCH_DISABLED
        assert result.fetch_method == "policy"
    # At 1 request/minute these would serialise into minutes of sleeping if the block
    # were applied after the rate limiter rather than before it.
    assert time.monotonic() - started < 1


class BlockedFetcher:
    async def fetch(self, url, **kwargs):
        return FetchResult(
            requested_url=url, fetch_method="policy", failure=FailureCode.FETCH_DISABLED
        )


async def test_blocked_domain_is_rejected_as_snippet_only_not_fetch_failed(config):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    pipeline = CandidatePipeline(database, config, BlockedFetcher())
    outcome = await pipeline.process(
        CandidateSeed(
            url="https://www.facebook.com/some-org/posts/123",
            title="Hackathon registration now open",
            discovery_channel="search",
            provider="fake",
        )
    )
    assert outcome.state == "REJECTED"
    assert outcome.reason == "SEARCH_SNIPPET_ONLY"
    async with database.session() as session:
        candidate = await session.scalar(select(CandidateRow))
        assert candidate.rejection_reasons == ["SEARCH_SNIPPET_ONLY"]
    await database.close()


class SlowPipeline:
    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.seen: list[str] = []
        self.peak = 0
        self._active = 0

    async def process(self, seed, *, historical_test: bool = False):
        self._active += 1
        self.peak = max(self.peak, self._active)
        try:
            await asyncio.sleep(self.delay)
            self.seen.append(str(seed.url))
        finally:
            self._active -= 1


class FailingPipeline(SlowPipeline):
    async def process(self, seed, *, historical_test: bool = False):
        await asyncio.sleep(0)
        raise RuntimeError("candidate exploded")


class StubProvider:
    name = "stub"

    def __init__(self, seeds: list[CandidateSeed]) -> None:
        self.seeds = seeds

    async def search(self, request):
        return SearchPage(results=self.seeds)


def _seeds(count: int) -> list[CandidateSeed]:
    return [
        CandidateSeed(
            url=f"https://example{index}.test/event",
            discovery_channel="search",
            provider="stub",
        )
        for index in range(count)
    ]


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.close()


async def test_candidates_in_one_page_are_processed_concurrently(config, database):
    seeds = _seeds(12)
    pipeline = SlowPipeline(delay=0.1)
    job = DiscoveryJob(database, config, StubProvider(seeds), pipeline)
    started = time.monotonic()
    counts = await job.run(query_limit=1)
    elapsed = time.monotonic() - started

    assert counts["candidates"] == 12
    assert counts["processed"] == 12
    assert counts["errors"] == 0
    assert len(pipeline.seen) == 12
    assert pipeline.peak > 1, "candidates were processed one at a time"
    assert pipeline.peak <= config.app.discovery_concurrency
    # 12 candidates at 0.1s each would take 1.2s serially.
    assert elapsed < 0.9


async def test_duplicate_urls_in_one_page_are_processed_once(config, database):
    """Candidates are keyed on the normalized URL, so parallel duplicates would race."""
    seeds = [
        CandidateSeed(url=url, discovery_channel="search", provider="stub")
        for url in (
            "https://example.test/event",
            "https://example.test/event/",
            "https://example.test/event?utm_source=fb",
            "https://example.test/other",
        )
    ]
    pipeline = SlowPipeline(delay=0)
    job = DiscoveryJob(database, config, StubProvider(seeds), pipeline)
    counts = await job.run(query_limit=1)
    assert counts["candidates"] == 2
    assert counts["processed"] == 2
    assert len(pipeline.seen) == 2


async def test_one_failing_candidate_does_not_abort_the_page(config, database):
    job = DiscoveryJob(database, config, StubProvider(_seeds(4)), FailingPipeline())
    counts = await job.run(query_limit=1)
    assert counts["candidates"] == 4
    assert counts["processed"] == 0
    assert counts["errors"] == 4


async def test_an_unnormalizable_url_does_not_abort_the_page(config, database):
    """normalize_url IDNA-encodes the host, and pydantic accepts hosts it cannot encode.

    A 70-character label passes HttpUrl validation and raises UnicodeEncodeError, so one
    bad search result used to throw away every other seed on the page with it.
    """
    seeds = [
        CandidateSeed(
            url="https://" + "a" * 70 + ".test/event",
            discovery_channel="search",
            provider="stub",
        ),
        *_seeds(3),
    ]
    pipeline = SlowPipeline(delay=0)
    job = DiscoveryJob(database, config, StubProvider(seeds), pipeline)
    counts = await job.run(query_limit=1)

    assert len(pipeline.seen) == 3, "the usable seeds still ran"
    assert counts["processed"] == 3
    assert counts["errors"] == 1, "the unusable seed is reported, not silently dropped"
