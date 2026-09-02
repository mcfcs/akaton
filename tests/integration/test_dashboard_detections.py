"""The competitions the dashboard shows, as the reader sees them.

This is the answer to "what did the bot find", which is a different question from the
events table's "what is in the database": it carries the poster, the organizer and the
deadline so a detection can be judged without opening the source page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from akaton.dashboard.runtime import BotController
from akaton.dashboard.web import create_dashboard
from akaton.domain.enums import NotificationState
from akaton.persistence.database import Database
from akaton.persistence.models import EventRow, NotificationRow


class FakeController:
    sources = ["search"]

    def status(self) -> dict:
        return {"scheduler": "STOPPED", "jobs": [], "running": {}, "last_runs": {}, "sources": []}

    def trigger(self, name, job=None) -> bool:
        return True


def _facts(**overrides) -> dict:
    facts = {
        "title": "eGov Hackathon 2026",
        "category": "HACKATHON",
        "document_kind": "REGISTRATION_OPEN",
        "registration_state": "OPEN",
        "event_phase": "UPCOMING",
        "description": (
            "Registration is open for the eGov Hackathon, a competition for students "
            "across the Philippines to build public-service software. Teams of three."
        ),
        "canonical_url": "https://dict.gov.ph/egov-hackathon-2026",
        "image_url": "https://dict.gov.ph/media/banner.jpg",
        "location": {"country": "PH", "city": "Manila", "location_type": "ONSITE"},
        "registration_deadline": {"value": "2099-10-05T00:00:00+00:00", "precision": "DATE"},
    }
    facts.update(overrides)
    return facts


def _event(score: int = 82, **overrides) -> EventRow:
    fields = {
        "title": "eGov Hackathon 2026",
        "normalized_title": "egov hackathon 2026",
        "category": "HACKATHON",
        "document_kind": "REGISTRATION_OPEN",
        "event_phase": "UPCOMING",
        "registration_state": "OPEN",
        "canonical_url": "https://dict.gov.ph/egov-hackathon-2026",
        "current_facts": _facts(),
        "relevance_score": score,
        "confidence_score": 0.9,
        "material_hash": f"hash-{score}",
    }
    fields.update(overrides)
    return EventRow(**fields)


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.close()


def _app(database, config):
    return create_dashboard(database, FakeController(), config, bot=BotController())


async def _get(database, config, path="/api/detections"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(database, config)), base_url="http://dashboard"
    ) as http:
        response = await http.get(path)
    assert response.status_code == 200
    return response.json()


async def test_a_detection_carries_what_the_alert_carries(database, config):
    async with database.session() as session:
        session.add(_event())
    [found] = await _get(database, config)
    assert found["title"] == "eGov Hackathon 2026"
    assert found["location"] == "Manila"
    assert found["score"] == 82
    assert found["registration"] == "OPEN"
    assert found["deadline"].startswith("2099-10-05")
    # The description is summarised the same way the Discord alert summarises it.
    assert "eGov Hackathon" in found["summary"]


async def test_the_poster_is_judged_by_the_same_trust_the_alert_uses(database, config):
    """The dashboard must not show a banner the webhook would have refused."""
    async with database.session() as session:
        session.add(_event())
        session.add(
            _event(
                score=70,
                title="Sketchy Hack",
                normalized_title="sketchy hack",
                canonical_url="https://random-blog.example/post",
                current_facts=_facts(
                    title="Sketchy Hack",
                    canonical_url="https://random-blog.example/post",
                    image_url="https://random-blog.example/banner.jpg",
                ),
                material_hash="hash-sketchy",
            )
        )
    found = {row["title"]: row for row in await _get(database, config)}
    assert found["eGov Hackathon 2026"]["image_url"] == "https://dict.gov.ph/media/banner.jpg"
    assert found["Sketchy Hack"]["image_url"] is None


async def test_detections_are_tiered_by_the_configured_thresholds(database, config):
    async with database.session() as session:
        session.add(_event(score=90, material_hash="a"))
        session.add(_event(score=70, title="B", normalized_title="b", material_hash="b"))
        session.add(_event(score=55, title="C", normalized_title="c", material_hash="c"))
        session.add(_event(score=20, title="D", normalized_title="d", material_hash="d"))
    tiers = [row["tier"] for row in await _get(database, config)]
    assert tiers == ["HIGH_PRIORITY", "RECOMMENDED", "POSSIBLE", "WEAK"]


async def test_the_strongest_match_is_shown_first(database, config):
    async with database.session() as session:
        session.add(_event(score=51, title="Weak", normalized_title="weak", material_hash="w"))
        session.add(_event(score=95, title="Strong", normalized_title="strong", material_hash="s"))
    assert [row["title"] for row in await _get(database, config)] == ["Strong", "Weak"]


async def test_a_detection_says_whether_it_was_actually_announced(database, config):
    """While alerts are off, "found" and "told you about" are entirely different things."""
    async with database.session() as session:
        session.add(_event())
        await session.flush()
        session.add(
            NotificationRow(
                event_id=1,
                notification_type="NEW_EVENT",
                dedupe_key="new:1",
                state=NotificationState.SENT.value,
                event_version=1,
                payload_hash="x",
                payload_json={},
                discord_message_id="123",
                sent_at=datetime.now(UTC),
            )
        )
        session.add(
            _event(score=70, title="Quiet", normalized_title="quiet", material_hash="quiet")
        )
    found = {row["title"]: row for row in await _get(database, config)}
    assert found["eGov Hackathon 2026"]["announced"] is True
    assert found["Quiet"]["announced"] is False


async def test_a_pending_alert_does_not_count_as_announced(database, config):
    async with database.session() as session:
        session.add(_event())
        await session.flush()
        session.add(
            NotificationRow(
                event_id=1,
                notification_type="NEW_EVENT",
                dedupe_key="new:1",
                state=NotificationState.PENDING.value,
                event_version=1,
                payload_hash="x",
                payload_json={},
            )
        )
    [found] = await _get(database, config)
    assert found["announced"] is False


async def test_archived_events_are_not_shown_as_detections(database, config):
    async with database.session() as session:
        session.add(_event(archived_at=datetime.now(UTC) - timedelta(days=1)))
    assert await _get(database, config) == []


async def test_detections_can_be_filtered_to_one_tier(database, config):
    async with database.session() as session:
        session.add(_event(score=90, material_hash="a"))
        session.add(_event(score=55, title="C", normalized_title="c", material_hash="c"))
    found = await _get(database, config, "/api/detections?tier=high_priority")
    assert [row["score"] for row in found] == [90]


async def test_an_event_with_no_facts_still_renders(database, config):
    """A row written before a field existed must not take the whole page down."""
    async with database.session() as session:
        session.add(_event(current_facts={}, material_hash="bare"))
    [found] = await _get(database, config)
    assert found["title"] == "eGov Hackathon 2026"
    assert found["image_url"] is None
    assert found["deadline"] is None
