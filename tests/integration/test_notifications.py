from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select

from akaton.domain.models import CandidateSeed, DeliveryReceipt, FetchResult
from akaton.persistence.database import Database
from akaton.persistence.models import NotificationRow
from akaton.pipeline import CandidatePipeline


class FakeNotifier:
    def __init__(self):
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)
        return DeliveryReceipt(message_id=str(len(self.payloads)))


class StableFetcher:
    async def fetch(self, url, **kwargs):
        text = (
            "Registration is now open to university students nationwide in the Philippines. "
            "Registration deadline October 5, 2026. Event date October 20, 2026 "
            "at Ateneo de Manila. Build AI software in this hackathon. " * 8
        )
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="Ateneo AI Hackathon 2026",
            text=text,
            links=["https://forms.gle/ateneo2026"],
            content_hash="stable",
            usable=True,
        )


async def test_new_event_notification_is_deduplicated(config):
    enabled = replace(config, app=config.app.model_copy(update={"notifications_enabled": True}))
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    notifier = FakeNotifier()
    pipeline = CandidatePipeline(database, enabled, StableFetcher(), notifier=notifier)
    seed = CandidateSeed(
        url="https://ateneo.edu/events/notify-hackathon-2026",
        discovery_channel="search",
        provider="fake",
    )
    await pipeline.process(seed)
    await pipeline.process(seed)
    assert len(notifier.payloads) == 1
    async with database.session() as session:
        assert await session.scalar(select(func.count(NotificationRow.id))) == 1
        notification = await session.scalar(select(NotificationRow))
        assert notification.state == "SENT"
    await database.close()
