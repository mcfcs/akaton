"""Not re-reading pages we have already judged.

Search returns the same URLs run after run. On the real database 97 of 364 candidates had
been fetched more than once — 491 fetches for 364 pages — and the most repeated was a
Facebook group URL fetched seven times, rejected identically each time because
`config/domains.yaml` disables fetching that host. No amount of asking changes that answer.

The rule has to be narrow in one specific way: `RefreshJob` and the dashboard's Retry
button both run this same pipeline *in order to* re-read a page. Neither may ever be told
it was checked recently.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from akaton.domain.models import CandidateSeed, FetchResult
from akaton.persistence.database import Database
from akaton.persistence.models import CandidateRow, SourceSnapshotRow
from akaton.pipeline import CandidatePipeline
from akaton.processing.recheck import last_judged_at, recheck_reason

URL = "https://example.ph/some-page"


class CountingFetcher:
    def __init__(self, text: str = "Nothing much here at all.") -> None:
        self.calls: list[str] = []
        self.text = text

    async def fetch(self, url, **kwargs):
        self.calls.append(url)
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="A page",
            text=self.text,
            content_hash=str(len(self.calls)),
            usable=True,
        )


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.close()


def _seed(channel: str = "search") -> CandidateSeed:
    return CandidateSeed(url=URL, title="A page", discovery_channel=channel, provider="fake")


class _Row:
    """The parts of a candidate the rule reads."""

    def __init__(self, state, reasons, judged, event_id=None):
        self.state = state
        self.rejection_reasons = reasons
        self.event_id = event_id
        self.trace = [{"at": judged.isoformat(), "state": state}] if judged else []


NOW = datetime(2026, 9, 2, tzinfo=UTC)


class TestTheRule:
    def test_a_page_never_judged_is_always_processed(self):
        assert recheck_reason(_Row("DISCOVERED", [], None), now=NOW) is None

    def test_a_settled_rejection_rests_for_a_month(self):
        """A blocked host or a results post will not have become a competition."""
        row = _Row("REJECTED", ["SEARCH_SNIPPET_ONLY"], NOW - timedelta(days=20))
        assert recheck_reason(row, now=NOW) == "judged_recently"
        stale = _Row("REJECTED", ["SEARCH_SNIPPET_ONLY"], NOW - timedelta(days=40))
        assert recheck_reason(stale, now=NOW) is None

    def test_a_thin_page_is_looked_at_again_sooner(self):
        """A page can gain a deadline; a blocked domain cannot gain a fetch."""
        row = _Row("AMBIGUOUS", ["REGISTRATION_UNCONFIRMED"], NOW - timedelta(days=3))
        assert recheck_reason(row, now=NOW) == "judged_recently"
        stale = _Row("AMBIGUOUS", ["REGISTRATION_UNCONFIRMED"], NOW - timedelta(days=10))
        assert recheck_reason(stale, now=NOW) is None

    def test_an_event_belongs_to_the_refresh_job(self):
        row = _Row("NOTIFIED", [], NOW - timedelta(days=1), event_id=7)
        assert recheck_reason(row, now=NOW) == "already_an_event"

    def test_the_refresh_job_is_never_deferred(self):
        """It runs this pipeline in order to re-read the page. This is the whole point."""
        row = _Row("NOTIFIED", [], NOW - timedelta(minutes=1), event_id=7)
        assert recheck_reason(row, channel="refresh", now=NOW) is None

    def test_a_manual_retry_is_never_deferred(self):
        row = _Row("REJECTED", ["SEARCH_SNIPPET_ONLY"], NOW - timedelta(minutes=1))
        assert recheck_reason(row, channel="manual", now=NOW) is None

    def test_a_backdate_is_never_deferred(self):
        row = _Row("REJECTED", ["SEARCH_SNIPPET_ONLY"], NOW - timedelta(minutes=1))
        assert recheck_reason(row, historical_test=True, now=NOW) is None

    def test_a_fetch_failure_is_left_to_the_retry_timer(self):
        """`retry_at` already governs those, with its own backoff."""
        row = _Row("FETCH_FAILED", ["FETCH_FAILED"], NOW - timedelta(days=1))
        assert recheck_reason(row, now=NOW) is None

    def test_the_judgement_time_comes_from_the_trace_not_the_row(self):
        """`upsert_candidate` touches the row on every sighting, so `updated_at` would
        be pushed forward by exactly the URLs that keep coming back — and the cooldown
        would never expire for them."""
        judged = NOW - timedelta(days=40)
        assert last_judged_at(_Row("REJECTED", [], judged)) == judged


class TestInThePipeline:
    async def test_the_same_url_is_not_fetched_twice_in_a_row(self, database, config):
        fetcher = CountingFetcher()
        pipeline = CandidatePipeline(database, config, fetcher)
        await pipeline.process(_seed())
        await pipeline.process(_seed())
        await pipeline.process(_seed())
        assert len(fetcher.calls) == 1, "judged once, then skipped"

    async def test_the_sighting_is_still_recorded(self, database, config):
        """Skipping the fetch must not lose the fact that search returned it again."""
        pipeline = CandidatePipeline(database, config, CountingFetcher())
        await pipeline.process(_seed())
        async with database.session() as session:
            first = (await session.scalar(select(CandidateRow))).last_seen_at
        await pipeline.process(_seed())
        async with database.session() as session:
            row = await session.scalar(select(CandidateRow))
        assert row.last_seen_at >= first
        assert int(await _count(database, SourceSnapshotRow)) == 1

    async def test_a_backdate_re_reads_it_anyway(self, database, config):
        fetcher = CountingFetcher()
        pipeline = CandidatePipeline(database, config, fetcher)
        await pipeline.process(_seed())
        await pipeline.process(_seed(), historical_test=True)
        assert len(fetcher.calls) == 2

    async def test_the_refresh_channel_re_reads_it_anyway(self, database, config):
        fetcher = CountingFetcher()
        pipeline = CandidatePipeline(database, config, fetcher)
        await pipeline.process(_seed())
        await pipeline.process(_seed("refresh"))
        assert len(fetcher.calls) == 2

    async def test_the_cooldown_expires(self, database, config):
        """Nothing here is permanent; everything is re-examined eventually."""
        impatient = replace(
            config,
            app=config.app.model_copy(
                update={"candidate_recheck_days": 0, "candidate_settled_recheck_days": 0}
            ),
        )
        fetcher = CountingFetcher()
        pipeline = CandidatePipeline(database, impatient, fetcher)
        await pipeline.process(_seed())
        await pipeline.process(_seed())
        assert len(fetcher.calls) == 2


async def _count(database, model):
    async with database.session() as session:
        return await session.scalar(select(func.count(model.id)))
