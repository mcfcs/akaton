from __future__ import annotations

from sqlalchemy import func, select

from akaton.domain.enums import FailureCode
from akaton.domain.models import CandidateSeed, FetchResult
from akaton.persistence.database import Database
from akaton.persistence.models import CandidateRow, EventChangeRow, EventRow, EventVersionRow
from akaton.pipeline import CandidatePipeline


class FakeFetcher:
    async def fetch(self, url, **kwargs):
        text = (
            "Registration is now open to university students nationwide in the Philippines. "
            "Registration deadline October 5, 2026. Event date October 20, 2026 "
            "at Ateneo de Manila. "
            "Build AI and software solutions in this hackathon. " * 8
        )
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="Ateneo AI Hackathon 2026",
            text=text,
            links=["https://forms.gle/ateneo2026"],
            content_hash="abc",
            usable=True,
        )


async def test_pipeline_is_idempotent_in_shadow_mode(config):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    pipeline = CandidatePipeline(database, config, FakeFetcher())
    seed = CandidateSeed(
        url="https://ateneo.edu/events/ai-hackathon-2026",
        title="Ateneo AI Hackathon 2026",
        discovery_channel="search",
        provider="fake",
    )
    first = await pipeline.process(seed)
    second = await pipeline.process(seed)
    assert first.event_id == second.event_id
    async with database.session() as session:
        assert await session.scalar(select(func.count(EventRow.id))) == 1
        assert await session.scalar(select(func.count(EventVersionRow.id))) == 1
        assert await session.scalar(select(func.count(CandidateRow.id))) == 1
    await database.close()


class DeadlineSequenceFetcher:
    def __init__(self):
        self.deadline = "October 5, 2026"

    async def fetch(self, url, **kwargs):
        text = (
            "Registration is now open to university students nationwide in the Philippines. "
            f"Registration deadline {self.deadline}. Event date October 20, 2026 "
            "at Ateneo de Manila. Build AI software in this hackathon. " * 8
        )
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="Ateneo Update Hackathon 2026",
            text=text,
            links=["https://forms.gle/ateneo-update-2026"],
            content_hash=self.deadline,
            usable=True,
        )


async def test_authoritative_deadline_extension_creates_event_version(config):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    fetcher = DeadlineSequenceFetcher()
    pipeline = CandidatePipeline(database, config, fetcher)
    seed = CandidateSeed(
        url="https://ateneo.edu/events/update-hackathon-2026",
        discovery_channel="refresh",
        provider="fake",
    )
    await pipeline.process(seed)
    fetcher.deadline = "October 12, 2026"
    await pipeline.process(seed)
    async with database.session() as session:
        assert await session.scalar(select(func.count(EventVersionRow.id))) == 2
        change = await session.scalar(select(EventChangeRow))
        assert change.change_type == "DEADLINE_EXTENDED"
        assert change.notify is True
    await database.close()


class RateLimitedFetcher:
    def __init__(self):
        self.calls = 0

    async def fetch(self, url, **kwargs):
        self.calls += 1
        return FetchResult(
            requested_url=url,
            fetch_method="http",
            status_code=429,
            headers={"retry-after": "3600"},
            failure=FailureCode.HTTP_429,
        )


async def test_rate_limit_is_deferred_without_immediate_refetch(config):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    fetcher = RateLimitedFetcher()
    pipeline = CandidatePipeline(database, config, fetcher)
    seed = CandidateSeed(
        url="https://example.com/rate-limited-event",
        discovery_channel="search",
        provider="fake",
    )
    first = await pipeline.process(seed)
    second = await pipeline.process(seed)
    assert first.state == "FETCH_DEFERRED"
    assert second.reason == "retry_not_due"
    assert fetcher.calls == 1
    async with database.session() as session:
        candidate = await session.scalar(select(CandidateRow))
        assert candidate.retry_at is not None
    await database.close()


class AnnualEditionFetcher:
    def __init__(self):
        self.year = 2026

    async def fetch(self, url, **kwargs):
        text = (
            "Registration is now open to university students nationwide in the Philippines. "
            f"Registration deadline October 5, {self.year}. "
            f"Event date October 20, {self.year} at Ateneo de Manila. "
            "Build AI and software solutions in this hackathon. " * 8
        )
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title=f"Ateneo Annual Hackathon {self.year}",
            text=text,
            content_hash=str(self.year),
            usable=True,
        )


async def test_reused_canonical_url_does_not_merge_annual_editions(config):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    fetcher = AnnualEditionFetcher()
    pipeline = CandidatePipeline(database, config, fetcher)
    seed = CandidateSeed(
        url="https://ateneo.edu/events/annual-hackathon",
        discovery_channel="refresh",
        provider="fake",
    )
    await pipeline.process(seed)
    fetcher.year = 2027
    await pipeline.process(seed)
    async with database.session() as session:
        assert await session.scalar(select(func.count(EventRow.id))) == 2
    await database.close()
