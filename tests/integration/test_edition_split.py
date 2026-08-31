"""Two runs of one competition on one landing page must be two events, and two alerts.

Government and university sites reuse a landing page as a matter of course, so this is
the common shape of a recurring competition, not an edge case. Before the edition key
carried a month, `compare_events` merged the September page onto the March row at 100 on
URL identity alone — the second run then never alerted, silently.

`test_pipeline.py::test_reused_canonical_url_does_not_merge_annual_editions` covers the
easier case where the year differs. This covers two runs inside one year.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select

from akaton.domain.models import CandidateSeed, DeliveryReceipt, FetchResult
from akaton.persistence.database import Database
from akaton.persistence.models import EventRow, NotificationRow
from akaton.pipeline import CandidatePipeline

URL = "https://dict.gov.ph/egov-hackathon"


class SeasonalEditionFetcher:
    """Serves one edition at a time from the same URL, as a reused landing page does."""

    def __init__(self) -> None:
        self.month = "March"
        self.day = 20
        self.deadline = "March 5"

    async def fetch(self, url, **kwargs):
        text = (
            "Registration is now open to university students nationwide in the "
            f"Philippines. Registration deadline {self.deadline}, 2026. "
            f"Event date {self.month} {self.day}, 2026 at the DICT office in Manila. "
            "Build AI and software solutions in this hackathon. " * 8
        )
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="eGov Hackathon 2026",
            text=text,
            content_hash=f"{self.month}-{self.day}",
            usable=True,
        )


class CountingNotifier:
    def __init__(self) -> None:
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)
        return DeliveryReceipt(message_id=str(len(self.payloads)))


async def _run(config, tmp_path, name, steps):
    enabled = replace(config, app=config.app.model_copy(update={"notifications_enabled": True}))
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}")
    await database.create_schema()
    fetcher = SeasonalEditionFetcher()
    notifier = CountingNotifier()
    pipeline = CandidatePipeline(database, enabled, fetcher, notifier=notifier)
    seed = CandidateSeed(url=URL, discovery_channel="search", provider="fake")
    for step in steps:
        step(fetcher)
        await pipeline.process(seed, historical_test=True)
    async with database.session() as session:
        events = int(await session.scalar(select(func.count(EventRow.id))) or 0)
        notifications = int(await session.scalar(select(func.count(NotificationRow.id))) or 0)
        keys = sorted(key for (key,) in (await session.execute(select(EventRow.edition_key))).all())
    await database.close()
    return events, notifications, notifier, keys


def _september(fetcher):
    fetcher.month, fetcher.day, fetcher.deadline = "September", 14, "September 1"


def _march(fetcher):
    fetcher.month, fetcher.day, fetcher.deadline = "March", 20, "March 5"


async def test_a_second_run_on_the_same_page_is_a_second_event(config, tmp_path):
    events, notifications, notifier, keys = await _run(
        config, tmp_path, "split.db", [_march, _september]
    )
    assert events == 2, "March and September are two runs, not one page updated"
    assert notifications == 2, "the September edition must alert on its own"
    assert keys == ["2026-03", "2026-09"]
    titles = [payload.title for payload in notifier.payloads]
    assert len(titles) == 2


async def test_seeing_the_same_run_again_does_not_duplicate_it(config, tmp_path):
    """The guard must not turn every re-read of one page into a new event."""
    events, notifications, _, keys = await _run(
        config, tmp_path, "same.db", [_march, _march, _march]
    )
    assert events == 1
    assert notifications == 1
    assert keys == ["2026-03"]
