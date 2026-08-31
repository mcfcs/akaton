"""A question in a group becomes an alert about the page it was asking about.

End to end: someone asks about a competition without linking to it, the name is
extracted, one search finds the official page, and that page — not the question — goes
through the normal pipeline and alerts only if it would have alerted anyway.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from akaton.discovery.base import SearchPage
from akaton.discovery.resolver import LeadResolver
from akaton.domain.models import CandidateSeed, DeliveryReceipt, FetchResult, MentionLead
from akaton.jobs.discovery import DiscoveryJob
from akaton.persistence.database import Database
from akaton.persistence.models import CandidateRow, EventRow, LeadRow
from akaton.persistence.repository import Repository
from akaton.pipeline import CandidatePipeline
from akaton.processing.leads import LeadState

OFFICIAL = "https://dict.gov.ph/egov-hackathon-2026"
THREAD = "https://www.facebook.com/groups/philhacks/permalink/4125912344210844/"


class OfficialPageFetcher:
    """Serves the announcement, and refuses to be asked for the thread."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def fetch(self, url, **kwargs):
        self.fetched.append(url)
        assert "facebook.com" not in url, "the mention must never become the candidate"
        text = (
            "Registration is now open to university students nationwide in the "
            "Philippines. Registration deadline October 5, 2026. Event date October 20, "
            "2026 at the DICT office in Manila. Build AI and software solutions in this "
            "hackathon. " * 8
        )
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="eGov Hackathon 2026",
            text=text,
            content_hash="egov-2026",
            usable=True,
        )


class CountingNotifier:
    def __init__(self) -> None:
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)
        return DeliveryReceipt(message_id=str(len(self.payloads)))


class OneResultProvider:
    name = "searxng"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, request):
        self.queries.append(request.query)
        if "eGov" not in request.query:
            return SearchPage(results=[])
        return SearchPage(
            results=[
                CandidateSeed(
                    url="https://www.reddit.com/r/PinoyProgrammer/comments/1/egov/",
                    title="Questions about the eGov hackathon",
                    discovery_channel="search",
                    provider="searxng",
                ),
                CandidateSeed(
                    url=OFFICIAL,
                    title="eGov Hackathon 2026 — DICT",
                    snippet="Registration is now open",
                    discovery_channel="search",
                    provider="searxng",
                ),
            ]
        )


class MentioningAdapter:
    """Stands in for the Facebook collector: no seeds, one mention."""

    name = "facebook"

    def __init__(self, mentions) -> None:
        self.last_mentions = list(mentions)
        self.last_error = None

    async def discover(self, since=None, cursor=None):
        return []


SOURCES = {
    "organizers": [
        {
            "id": "dict",
            "name": "DICT",
            "aliases": ["DICT"],
            "domains": ["dict.gov.ph"],
            "authority": 90,
        }
    ],
    "structured_sources": {"facebook": {"enabled": True, "cadence_hours": 0}},
}


def _mention():
    return MentionLead(
        name="eGov hackathon",
        normalized_name="egov hackathon",
        platform="facebook",
        mention_kind="question",
        source_url=THREAD,
        excerpt="pwede po ba manuod if hindi naka register sa egov hackaton?",
        query="eGov hackathon",
    )


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.close()


async def test_a_question_becomes_an_alert_about_the_official_page(database, config):
    enabled = replace(
        config,
        app=config.app.model_copy(
            update={"notifications_enabled": True, "search_interval_seconds": 0}
        ),
        sources={**config.sources, **SOURCES},
    )
    fetcher = OfficialPageFetcher()
    notifier = CountingNotifier()
    pipeline = CandidatePipeline(database, enabled, fetcher, notifier=notifier)
    provider = OneResultProvider()
    job = DiscoveryJob(
        database,
        enabled,
        provider,
        pipeline,
        [MentioningAdapter([_mention()])],
        resolver=LeadResolver(provider, enabled.sources),
    )

    # First run: the collector records the mention. Nothing is searched for it yet — the
    # run's allocation was fixed before the adapter produced it.
    first = await job.run(query_limit=3)
    assert first["leads"] == 1, "the mention is recorded"
    assert first["lead_searches"] == 0, "and not searched in the run that found it"

    # Second run: the lead is due, one search resolves it, the page enters the pipeline.
    second = await job.run(query_limit=3)

    assert second["lead_searches"] == 1, "one search for the lead"
    assert fetcher.fetched == [OFFICIAL], "the official page, never the thread"

    async with database.session() as session:
        events = list((await session.scalars(select(EventRow))).all())
        lead = await session.scalar(select(LeadRow))
        candidates = int(await session.scalar(select(func.count(CandidateRow.id))) or 0)
    assert len(events) == 1
    assert events[0].canonical_url == OFFICIAL
    assert candidates == 1, "the question itself never became a candidate"
    assert lead.state == LeadState.RESOLVED
    assert lead.resolved_url == OFFICIAL
    assert lead.event_id == events[0].id

    assert len(notifier.payloads) == 1
    payload = notifier.payloads[0]
    # The document is official, so it keeps its official styling and clickable link.
    assert payload.source_kind == "official"
    assert payload.official_url_clickable is True
    # And the reader is still told why it turned up, with the thread to check.
    assert payload.source_label == "Found via a Facebook mention"
    assert payload.source_url == THREAD


async def test_a_second_mention_of_the_same_competition_costs_nothing(database, config):
    """The repeat-ping case: more people asking must not mean more searches."""
    enabled = replace(
        config,
        app=config.app.model_copy(update={"search_interval_seconds": 0}),
        sources={**config.sources, **SOURCES},
    )
    provider = OneResultProvider()
    adapter = MentioningAdapter([_mention(), _mention(), _mention()])
    job = DiscoveryJob(
        database,
        enabled,
        provider,
        CandidatePipeline(database, enabled, OfficialPageFetcher()),
        [adapter],
        resolver=LeadResolver(provider, enabled.sources),
    )

    await job.run(query_limit=3)
    async with database.session() as session:
        row = await session.scalar(select(LeadRow))
    assert row.sightings == 3, "three mentions"

    await job.run(query_limit=3)
    async with database.session() as session:
        rows = int(await session.scalar(select(func.count(LeadRow.id))) or 0)
    assert rows == 1
    assert sum(1 for query in provider.queries if "eGov" in query) == 1, "one search, not three"


async def test_a_new_edition_is_searched_even_while_the_old_one_cools(database, config):
    """eGovPH running again in September must not be swallowed by March's cooldown."""
    async with database.session() as session:
        repo = Repository(session)
        march = await repo.record_mention(_mention())
        await repo.mark_lead_searched(march.id, resolved_url=OFFICIAL)
        september = await repo.record_mention(
            _mention().model_copy(update={"edition_hint": "september"})
        )

    async with database.session() as session:
        due = await Repository(session).due_leads(5)

    assert [row.id for row in due] == [september.id]
    assert september.id != march.id
