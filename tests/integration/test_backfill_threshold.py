"""A backdate relaxes the time gates, not the relevance bar.

`historical_test` exists so a replay of past dates is not thrown out for being in the
past — that is what `allow_historical` does to the past-event and registration-deadline
gates. It also used to skip the notification threshold entirely, which is a different
thing and was never intended: three of the eight events the live database had stored
scored 59, 64 and 64 against a threshold of 65 and alerted anyway.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from akaton.domain.models import CandidateSeed, DeliveryReceipt, FetchResult
from akaton.persistence.database import Database
from akaton.persistence.models import EventRow, NotificationRow
from akaton.pipeline import CandidatePipeline

# The real Casiguran tourism contest: a genuine call for entries, on a gov.ph host, that
# is simply not a hackathon or a business case competition. It scored 64.
FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "news_vs_events.json").read_text(
        encoding="utf-8"
    )
)
OFF_TOPIC = next(case for case in FIXTURE if case["id"] == "event-4")


class FixtureFetcher:
    def __init__(self, case: dict) -> None:
        self.case = case

    async def fetch(self, url, **kwargs):
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title=self.case["title"],
            text=self.case["text"],
            content_hash=self.case["id"],
            usable=True,
        )


class CountingNotifier:
    def __init__(self) -> None:
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)
        return DeliveryReceipt(message_id=str(len(self.payloads)))


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.close()


async def _run(database, config, *, historical: bool):
    enabled = replace(config, app=config.app.model_copy(update={"notifications_enabled": True}))
    notifier = CountingNotifier()
    pipeline = CandidatePipeline(database, enabled, FixtureFetcher(OFF_TOPIC), notifier=notifier)
    outcome = await pipeline.process(
        CandidateSeed(url=OFF_TOPIC["url"], discovery_channel="search", provider="fake"),
        historical_test=historical,
    )
    async with database.session() as session:
        events = int(await session.scalar(select(func.count(EventRow.id))) or 0)
        notifications = int(await session.scalar(select(func.count(NotificationRow.id))) or 0)
    return outcome, events, notifications, notifier


async def test_a_backdate_creates_the_event_but_does_not_alert_below_threshold(database, config):
    outcome, events, notifications, notifier = await _run(database, config, historical=True)

    assert events == 1, "the event is still recorded, which is what makes a backdate useful"
    assert outcome.reason == "low_relevance"
    assert notifications == 0, "a below-threshold event must not alert, backdate or not"
    assert notifier.payloads == []


async def test_a_scheduled_run_treats_the_same_page_identically(database, config):
    """The two paths must agree on relevance; only the time gates differ."""
    outcome, _, notifications, _ = await _run(database, config, historical=False)
    assert outcome.reason == "low_relevance"
    assert notifications == 0
