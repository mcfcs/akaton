"""One search per competition mentioned, and a new edition is still a new search."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from akaton.discovery.base import SearchPage
from akaton.discovery.resolver import LeadResolver, rank_results
from akaton.domain.models import CandidateSeed, MentionLead
from akaton.jobs.discovery import DiscoveryJob
from akaton.persistence.database import Database
from akaton.persistence.models import LeadRow
from akaton.persistence.repository import Repository
from akaton.processing.leads import LeadState, is_due, lead_key, unresolved_cooldown

SOURCES = {
    "organizers": [
        {
            "id": "dict",
            "name": "DICT",
            "aliases": ["DICT"],
            "domains": ["dict.gov.ph"],
            "authority": 90,
        },
        {
            "id": "gcash",
            "name": "GCash",
            "aliases": ["GCash"],
            "domains": ["gcash.com"],
            "authority": 85,
        },
    ],
    "platforms": {"gov.ph": 85},
}


def mention(name="eGov hackathon", *, hint=None, normalized=None, platform="facebook"):
    return MentionLead(
        name=name,
        normalized_name=normalized or name.casefold(),
        edition_hint=hint,
        platform=platform,
        mention_kind="question",
        source_url="https://www.facebook.com/groups/philhacks/permalink/1234/",
        query=" ".join(part for part in (name, hint) if part),
    )


def seed(url, title=None, snippet=None):
    return CandidateSeed(
        url=url,
        title=title,
        snippet=snippet,
        discovery_channel="search",
        provider="searxng",
    )


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.close()


class TestLeadIdentity:
    async def test_the_same_name_twice_is_one_lead(self, database):
        """Twenty people asking about eGovPH must cost one search, not twenty."""
        async with database.session() as session:
            repo = Repository(session)
            await repo.record_mention(mention())
            await repo.record_mention(
                mention(name="the eGov Hackathon", normalized="egov hackathon")
            )
        async with database.session() as session:
            rows = list((await session.scalars(select(LeadRow))).all())
        assert len(rows) == 1
        assert rows[0].sightings == 2

    async def test_a_named_edition_is_a_separate_lead(self, database):
        """eGovPH running again in September must not be suppressed by March's cooldown."""
        async with database.session() as session:
            repo = Repository(session)
            await repo.record_mention(mention())
            await repo.record_mention(mention(hint="september"))
        async with database.session() as session:
            count = await session.scalar(select(func.count(LeadRow.id)))
            hints = sorted(
                (hint or "")
                for (hint,) in (await session.execute(select(LeadRow.edition_hint))).all()
            )
        assert count == 2
        assert hints == ["", "september"]

    def test_the_key_folds_whitespace_but_not_editions(self):
        assert lead_key("egov hackathon", None) == lead_key(" egov hackathon ", "")
        assert lead_key("egov hackathon", None) != lead_key("egov hackathon", "september")


class TestCooldown:
    def test_a_lead_never_searched_is_due(self):
        assert is_due(LeadState.NEW, 0, None)

    def test_a_resolved_lead_rests_for_a_month(self):
        now = datetime.now(UTC)
        assert not is_due(LeadState.RESOLVED, 1, now - timedelta(days=29), now=now)
        assert is_due(LeadState.RESOLVED, 1, now - timedelta(days=31), now=now)

    def test_an_unresolved_lead_backs_off(self):
        """A name that cannot resolve once usually cannot resolve at all."""
        assert unresolved_cooldown(1) == timedelta(days=7)
        assert unresolved_cooldown(2) == timedelta(days=14)
        assert unresolved_cooldown(3) == timedelta(days=28)
        assert unresolved_cooldown(9) == timedelta(days=60), "and is capped"

    def test_a_naive_timestamp_from_sqlite_does_not_crash_the_comparison(self):
        assert is_due(LeadState.UNRESOLVED, 1, datetime(2020, 1, 1))

    async def test_a_lead_inside_its_cooldown_is_not_offered_again(self, database):
        async with database.session() as session:
            repo = Repository(session)
            row = await repo.record_mention(mention())
            await repo.mark_lead_searched(row.id, resolved_url="https://dict.gov.ph/egov")
        async with database.session() as session:
            assert await Repository(session).due_leads(5) == []
        async with database.session() as session:
            row = await session.scalar(select(LeadRow))
            assert row.state == LeadState.RESOLVED
            assert row.resolved_url == "https://dict.gov.ph/egov"


class TestRanking:
    def test_the_most_authoritative_page_wins_not_the_first(self):
        """Live, "Hack4Gov Philippines" returned news articles above pia.gov.ph."""
        results = [
            seed("https://news.example.com/hack4gov-recap", "Hack4Gov recap"),
            seed("https://dict.gov.ph/hack4gov", "Hack4Gov registration"),
        ]
        ranked = rank_results(results, SOURCES, "Hack4Gov")
        assert str(ranked[0][1].url) == "https://dict.gov.ph/hack4gov"

    def test_resolving_a_mention_to_another_mention_is_refused(self):
        """One live hit was "Questions about Hack4gov competition : r/PinoyProgrammer"."""
        results = [
            seed("https://www.reddit.com/r/PinoyProgrammer/comments/1/hack4gov/", "Hack4Gov?"),
            seed("https://www.facebook.com/groups/philhacks/permalink/9/", "Hack4Gov"),
        ]
        assert rank_results(results, SOURCES, "Hack4Gov") == []

    def test_an_authoritative_page_about_something_else_is_refused(self):
        """gov.ph makes a host credible, not this page relevant."""
        results = [seed("https://elibrary.judiciary.gov.ph/philippinereports", "Reports")]
        assert rank_results(results, SOURCES, "Hack4Gov") == []

    def test_a_listing_page_is_not_one_competition(self):
        results = [seed("https://dict.gov.ph/events/?search=hackathon", "All events")]
        assert rank_results(results, SOURCES, "Hack4Gov") == []


class StubProvider:
    name = "searxng"

    def __init__(self, results, degraded=False):
        self.results = results
        self.degraded = degraded
        self.queries: list[str] = []

    async def search(self, request):
        self.queries.append(request.query)
        return SearchPage(results=list(self.results), degraded=self.degraded)


class TestResolution:
    async def test_a_resolved_page_carries_why_we_looked(self):
        provider = StubProvider([seed("https://dict.gov.ph/egov-hackathon", "eGov Hackathon")])
        resolved, reason = await LeadResolver(provider, SOURCES).resolve(mention(), lead_id=7)
        assert reason == ""
        assert str(resolved.url) == "https://dict.gov.ph/egov-hackathon"
        # The document is an official page and must keep its own styling and links.
        assert resolved.discovery_channel == "search"
        assert resolved.lead.lead_id == 7
        assert resolved.lead.platform == "facebook"
        assert "philhacks" in resolved.lead.source_url
        assert provider.queries == ["eGov hackathon Philippines"]

    async def test_a_degraded_backend_is_not_a_failed_lead(self):
        """Otherwise an outage would burn the lead's cooldown for nothing."""
        provider = StubProvider([], degraded=True)
        resolved, reason = await LeadResolver(provider, SOURCES).resolve(mention(), lead_id=1)
        assert resolved is None
        assert "unavailable" in reason


class RecordingPipeline:
    def __init__(self):
        self.seen: list[CandidateSeed] = []

    async def process(self, seed, *, historical_test: bool = False):
        self.seen.append(seed)


class TestBudget:
    async def _job(self, database, config, provider, *, per_run=3, limit=9):
        fast = replace(
            config,
            app=config.app.model_copy(
                update={"search_interval_seconds": 0, "mention_leads_per_run": per_run}
            ),
        )
        pipeline = RecordingPipeline()
        job = DiscoveryJob(
            database, fast, provider, pipeline, resolver=LeadResolver(provider, SOURCES)
        )
        return job, pipeline, limit

    async def test_leads_come_out_of_the_run_allocation_not_beside_it(self, database, config):
        async with database.session() as session:
            repo = Repository(session)
            for index in range(5):
                await repo.record_mention(mention(name=f"Hack{index} challenge"))
        provider = StubProvider([seed("https://dict.gov.ph/x", "Hack0 challenge")])
        job, _, limit = await self._job(database, config, provider)

        counts = await job.run(query_limit=limit)

        assert counts["lead_searches"] == 3, "capped by mention_leads_per_run"
        assert counts["queries"] == limit, "leads and scheduled queries share one allocation"

    async def test_the_cap_never_exceeds_a_third_of_the_run(self, database, config):
        async with database.session() as session:
            repo = Repository(session)
            for index in range(5):
                await repo.record_mention(mention(name=f"Hack{index} challenge"))
        provider = StubProvider([])
        job, _, _ = await self._job(database, config, provider, per_run=3)

        counts = await job.run(query_limit=3)

        assert counts["lead_searches"] == 1
        assert counts["queries"] == 3

    async def test_every_request_is_still_spaced(self, database, config, monkeypatch):
        """The pause is what keeps SearXNG's engines alive; leads must not skip it."""
        import akaton.jobs.discovery as discovery_module

        sleeps: list[float] = []

        async def record(seconds):
            sleeps.append(seconds)

        async with database.session() as session:
            await Repository(session).record_mention(mention())
        provider = StubProvider([])
        paced = replace(config, app=config.app.model_copy(update={"search_interval_seconds": 4}))
        monkeypatch.setattr(discovery_module.asyncio, "sleep", record)
        job = DiscoveryJob(
            database,
            paced,
            provider,
            RecordingPipeline(),
            resolver=LeadResolver(provider, SOURCES),
        )

        counts = await job.run(query_limit=6)

        assert counts["queries"] == 6
        assert sleeps == [4] * 5, "one pause between every pair of requests"
