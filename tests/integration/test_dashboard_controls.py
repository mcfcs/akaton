from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from akaton.dashboard.runtime import BotController, MonitorController
from akaton.dashboard.web import create_dashboard
from akaton.domain.models import DeliveryReceipt
from akaton.persistence.database import Database
from akaton.persistence.models import EventRow, NotificationRow

FACTS = {
    "title": "Manila Hackathon 2026",
    "category": "HACKATHON",
    "document_kind": "REGISTRATION_OPEN",
    "registration_state": "OPEN",
    "event_phase": "UPCOMING",
    "description": "Registration is now open to students in Manila, Philippines.",
    "canonical_url": "https://dict.gov.ph/manila-hackathon-2026",
    "location": {"country": "PH", "city": "Manila", "location_type": "ONSITE", "confidence": 0.9},
}


class FakeBot:
    def __init__(self) -> None:
        self.user = "Akaton#5201"
        self.closed = False
        self._stop = asyncio.Event()

    async def start(self, token: str) -> None:
        await self._stop.wait()

    async def close(self) -> None:
        self.closed = True
        self._stop.set()


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)
        return DeliveryReceipt(message_id=str(len(self.sent)))


class RefusingNotifier:
    async def send(self, payload):
        raise RuntimeError("channel is gone")


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    async with db.session() as session:
        session.add(
            EventRow(
                title="Manila Hackathon 2026",
                normalized_title="manila hackathon 2026",
                category="HACKATHON",
                document_kind="REGISTRATION_OPEN",
                event_phase="UPCOMING",
                registration_state="OPEN",
                canonical_url="https://dict.gov.ph/manila-hackathon-2026",
                current_facts=FACTS,
                relevance_score=82,
                confidence_score=0.9,
                material_hash="x",
                last_verified_at=datetime.now(UTC),
            )
        )
    yield db
    await db.close()


def _client(database, *, bot=None, notifier=None, config=None, controller=None):
    controller = controller or MonitorController(_FakeScheduler(), _noop, _noop)
    app = create_dashboard(database, controller, config, bot=bot, notifier=notifier)
    return TestClient(app)


async def _noop():
    return {}


class _FakeScheduler:
    state = 1

    def get_jobs(self):
        return []


async def test_bot_can_be_started_and_stopped_from_the_dashboard(database, config):
    created = []

    def factory():
        bot = FakeBot()
        created.append(bot)
        return bot

    controller = BotController(factory, "token")
    with _client(database, bot=controller, config=config) as client:
        assert client.get("/api/status").json()["bot"]["state"] == "STOPPED"

        started = client.post("/api/actions/bot/start").json()
        assert started["changed"] is True
        assert client.get("/api/status").json()["bot"]["state"] == "RUNNING"

        # A second start is refused rather than launching a duplicate connection.
        assert client.post("/api/actions/bot/start").json()["changed"] is False

        stopped = client.post("/api/actions/bot/stop").json()
        assert stopped["changed"] is True
        assert created[0].closed is True
        assert client.get("/api/status").json()["bot"]["state"] == "STOPPED"

        # Restarting builds a fresh client, because discord.py cannot reuse a closed one.
        client.post("/api/actions/bot/start")
        assert len(created) == 2
        client.post("/api/actions/bot/stop")


async def test_the_dashboard_reports_when_discord_is_not_configured(database, config):
    with _client(database, bot=BotController(), config=config) as client:
        assert client.get("/api/status").json()["bot"]["state"] == "NOT_CONFIGURED"
        assert client.post("/api/actions/bot/start").status_code == 409


async def test_forcing_an_alert_sends_it_and_records_it(database, config):
    controller = BotController(FakeBot, "token")
    notifier = RecordingNotifier()
    with _client(database, bot=controller, notifier=notifier, config=config) as client:
        client.post("/api/actions/bot/start")
        response = client.post("/api/actions/events/1/notify")
        assert response.status_code == 202
        assert "Manila Hackathon" in response.json()["message"]
        client.post("/api/actions/bot/stop")

    assert len(notifier.sent) == 1
    payload = notifier.sent[0]
    assert payload.title == "Manila Hackathon 2026"
    # A manual send must not collide with the automatic key or be blocked by it.
    assert payload.dedupe_key.startswith("manual:1:")
    assert payload.notification_type == "MANUAL_SEND"

    async with database.session() as session:
        rows = await session.scalar(select(func.count(NotificationRow.id)))
        row = await session.scalar(select(NotificationRow))
    assert rows == 1
    assert row.state == "SENT"


async def test_forcing_the_same_alert_twice_is_allowed(database, config):
    """The threshold and already-announced checks govern automatic delivery, not this."""
    controller = BotController(FakeBot, "token")
    notifier = RecordingNotifier()
    with _client(database, bot=controller, notifier=notifier, config=config) as client:
        client.post("/api/actions/bot/start")
        assert client.post("/api/actions/events/1/notify").status_code == 202
        assert client.post("/api/actions/events/1/notify").status_code == 202
        client.post("/api/actions/bot/stop")
    assert len(notifier.sent) == 2
    assert notifier.sent[0].dedupe_key != notifier.sent[1].dedupe_key


async def test_forcing_an_alert_needs_a_connected_bot(database, config):
    with _client(database, bot=BotController(), notifier=RecordingNotifier(), config=config) as c:
        assert c.post("/api/actions/events/1/notify").status_code == 409


async def test_a_missing_event_is_not_found(database, config):
    controller = BotController(FakeBot, "token")
    with _client(database, bot=controller, notifier=RecordingNotifier(), config=config) as client:
        client.post("/api/actions/bot/start")
        assert client.post("/api/actions/events/999/notify").status_code == 404
        client.post("/api/actions/bot/stop")


class RecordingDiscovery:
    """Stands in for DiscoveryJob.run, capturing the arguments a backdate sends it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.gate = asyncio.Event()

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        await self.gate.wait()
        return {"queries": 0}


async def test_a_backdate_reaches_the_named_collectors(database, config):
    discovery = RecordingDiscovery()
    controller = MonitorController(
        _FakeScheduler(), discovery, _noop, sources=["search", "facebook", "reddit"]
    )
    with _client(database, config=config, controller=controller) as client:
        assert client.get("/api/status").json()["monitor"]["sources"] == [
            "search",
            "facebook",
            "reddit",
        ]
        response = client.post(
            "/api/actions/backfill",
            json={"since": "2026-06-01", "sources": ["facebook", "reddit"]},
        )
        assert response.status_code == 202
        assert response.json()["accepted"] is True

        # Single-flight: a second backdate is refused while the first is still running.
        again = client.post("/api/actions/backfill", json={"since": "2026-06-01"})
        assert again.json()["accepted"] is False
        discovery.gate.set()

    assert len(discovery.calls) == 1
    call = discovery.calls[0]
    assert call["since"] == date(2026, 6, 1)
    assert call["sources"] == ["facebook", "reddit"]
    assert call["historical_test"] is True


async def test_a_backdate_refuses_an_unknown_collector(database, config):
    controller = MonitorController(_FakeScheduler(), _noop, _noop, sources=["search", "facebook"])
    with _client(database, config=config, controller=controller) as client:
        response = client.post(
            "/api/actions/backfill", json={"since": "2026-06-01", "sources": ["twitter"]}
        )
        assert response.status_code == 422
        assert "twitter" in response.json()["detail"]


async def test_a_backdate_refuses_a_future_date(database, config):
    with _client(database, config=config) as client:
        response = client.post("/api/actions/backfill", json={"since": "2099-01-01"})
        assert response.status_code == 422


async def test_a_backdate_with_no_collectors_runs_search_alone(database, config):
    """Matching `akaton backfill` with no --sources: the adapters have no history to replay."""
    discovery = RecordingDiscovery()
    discovery.gate.set()
    controller = MonitorController(_FakeScheduler(), discovery, _noop, sources=["search"])
    with _client(database, config=config, controller=controller) as client:
        response = client.post("/api/actions/backfill", json={"since": "2026-06-01"})
        assert response.status_code == 202
        assert "search" in response.json()["message"]
    assert discovery.calls[0]["sources"] is None


async def test_a_refused_delivery_is_reported_and_recorded(database, config):
    controller = BotController(FakeBot, "token")
    with _client(database, bot=controller, notifier=RefusingNotifier(), config=config) as client:
        client.post("/api/actions/bot/start")
        response = client.post("/api/actions/events/1/notify")
        assert response.status_code == 502
        client.post("/api/actions/bot/stop")
    async with database.session() as session:
        row = await session.scalar(select(NotificationRow))
    assert row is not None and row.state == "FAILED"
    assert "channel is gone" in row.last_error
