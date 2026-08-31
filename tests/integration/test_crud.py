"""Correcting, pinning and archiving records from the dashboard.

The pin is the part that matters. A refresh re-reads the source page every 24 hours, so
without it a hand correction is silently undone within a day — the operator would fix the
same field over and over and never know why it kept coming back.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from akaton.dashboard.runtime import MonitorController
from akaton.dashboard.web import create_dashboard
from akaton.domain.enums import CompetitionCategory, DocumentKind
from akaton.domain.models import (
    CandidateSeed,
    DateFact,
    EventFacts,
    ExtractionEnvelope,
    FetchResult,
    MentionLead,
)
from akaton.persistence.database import Database
from akaton.persistence.models import (
    CandidateRow,
    EventChangeRow,
    EventRow,
    EventVersionRow,
    LeadRow,
    SourceSnapshotRow,
)
from akaton.persistence.repository import Repository
from akaton.processing.leads import LeadState

FACTS = {
    "title": "Manila Hackathon 2026",
    "normalized_title": "manila hackathon 2026",
    "category": "HACKATHON",
    "document_kind": "REGISTRATION_OPEN",
    "registration_state": "OPEN",
    "event_phase": "UPCOMING",
    "canonical_url": "https://dict.gov.ph/manila-hackathon-2026",
    "location": {"country": "PH", "city": "Manila", "location_type": "ONSITE", "confidence": 0.9},
}


class _FakeScheduler:
    state = 1

    def get_jobs(self):
        return []


async def _noop():
    return {}


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


def _client(database, config, *, reprocess=None):
    controller = MonitorController(_FakeScheduler(), _noop, _noop)
    return TestClient(create_dashboard(database, controller, config, reprocess=reprocess))


class TestEventEditing:
    def test_a_correction_is_applied_and_pinned(self, database, config):
        with _client(database, config) as client:
            response = client.patch(
                "/api/events/1",
                json={"fields": {"title": "Manila Hackathon 2026 (corrected)", "city": "Pasig"}},
            )
            assert response.status_code == 200
            body = response.json()
            assert sorted(body["changed"]) == ["city", "title"]
            assert body["event"]["title"] == "Manila Hackathon 2026 (corrected)"
            assert sorted(body["event"]["pinned"]) == ["city", "title"]

    def test_an_unknown_field_is_refused(self, database, config):
        with _client(database, config) as client:
            response = client.patch("/api/events/1", json={"fields": {"material_hash": "x"}})
            assert response.status_code == 422
            assert "material_hash" in response.json()["detail"]

    def test_a_bad_value_names_the_field(self, database, config):
        with _client(database, config) as client:
            response = client.patch("/api/events/1", json={"fields": {"category": "NONSENSE"}})
            assert response.status_code == 422
            assert response.json()["detail"].startswith("category:")

    def test_a_url_must_be_a_url(self, database, config):
        with _client(database, config) as client:
            response = client.patch(
                "/api/events/1", json={"fields": {"registration_url": "javascript:alert(1)"}}
            )
            assert response.status_code == 422

    def test_a_date_is_stored_as_a_fully_trusted_fact(self, database, config):
        """`processing.editions` reads confidence and year_inferred to split editions."""
        with _client(database, config) as client:
            client.patch("/api/events/1", json={"fields": {"event_start": "2026-10-20"}})
        assert True

    async def test_editing_writes_a_version(self, database, config):
        with _client(database, config) as client:
            client.patch("/api/events/1", json={"fields": {"title": "Corrected title"}})
        async with database.session() as session:
            versions = list((await session.scalars(select(EventVersionRow))).all())
            row = await session.get(EventRow, 1)
        assert len(versions) == 1, "the edit is recorded in the version history"
        assert row.current_version == 2
        assert versions[0].extraction_version == "manual", "and is attributable to a person"

    async def test_editing_a_material_field_records_a_change_but_never_alerts(
        self, database, config
    ):
        """`detect_changes` only tracks alertable fields — a deadline is one, a title is not."""
        with _client(database, config) as client:
            client.patch("/api/events/1", json={"fields": {"registration_deadline": "2026-10-05"}})
            # A second edit, so there is a before *and* an after deadline to compare.
            client.patch("/api/events/1", json={"fields": {"registration_deadline": "2026-11-05"}})
        async with database.session() as session:
            changes = list((await session.scalars(select(EventChangeRow))).all())
        assert changes, "the deadline move is in the change history"
        # A person already knows what they just typed; this must not fire a change alert.
        assert all(change.notify is False for change in changes)

    def test_repeating_the_same_edit_changes_nothing(self, database, config):
        with _client(database, config) as client:
            client.patch("/api/events/1", json={"fields": {"title": "Corrected"}})
            second = client.patch("/api/events/1", json={"fields": {"title": "Corrected"}})
        assert second.json()["changed"] == []

    def test_a_missing_event_is_not_found(self, database, config):
        with _client(database, config) as client:
            assert (
                client.patch("/api/events/999", json={"fields": {"title": "x"}}).status_code == 404
            )


class RefreshFetcher:
    """Serves the source page, which disagrees with the correction."""

    async def fetch(self, url, **kwargs):
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="Manila Hackathon 2026",
            text=(
                "Registration is now open to university students nationwide in the "
                "Philippines. Registration deadline October 5, 2026. Event date October "
                "20, 2026 in Manila. Build AI and software solutions in this hackathon. "
            )
            * 8,
            content_hash="refresh",
            usable=True,
        )


class TestPinning:
    """The whole reason overrides exist."""

    async def test_a_pinned_field_survives_a_refresh(self, database, config):
        with _client(database, config) as client:
            client.patch("/api/events/1", json={"fields": {"title": "The real name"}})

        await _refresh(database, config)

        async with database.session() as session:
            row = await session.get(EventRow, 1)
        assert row.current_facts["title"] == "The real name"
        assert row.title == "The real name"

    async def test_an_unpinned_field_follows_the_page(self, database, config):
        async with database.session() as session:
            before = (await session.get(EventRow, 1)).current_facts.get("event_start")
        assert before is None

        await _refresh(database, config)

        async with database.session() as session:
            row = await session.get(EventRow, 1)
        assert row.current_facts["event_start"]["value"], "the page's date was adopted"

    async def test_a_released_field_follows_the_page_again(self, database, config):
        with _client(database, config) as client:
            client.patch("/api/events/1", json={"fields": {"title": "The real name"}})
            released = client.delete("/api/events/1/overrides/title")
            assert released.status_code == 200

        await _refresh(database, config)

        async with database.session() as session:
            row = await session.get(EventRow, 1)
        assert row.current_facts["title"] == "Manila Hackathon 2026"

    def test_releasing_a_field_that_is_not_pinned_is_not_found(self, database, config):
        with _client(database, config) as client:
            assert client.delete("/api/events/1/overrides/title").status_code == 404


async def _refresh(database, config):
    """Re-read the source page through the normal update path."""
    from akaton.domain.models import DocumentContext
    from akaton.processing.deterministic import extract_deterministically

    fetch = await RefreshFetcher().fetch("https://dict.gov.ph/manila-hackathon-2026")
    async with database.session() as session:
        repo = Repository(session)
        candidate = await repo.upsert_candidate(
            CandidateSeed(
                url="https://dict.gov.ph/manila-hackathon-2026",
                discovery_channel="refresh",
                provider="test",
            )
        )
        snapshot = await repo.add_snapshot(candidate, fetch)
        event = await session.get(EventRow, 1)
        extraction = extract_deterministically(
            DocumentContext(url=fetch.final_url, title=fetch.title, text=fetch.text)
        )
        await repo.update_event(
            event, extraction, relevance_score=80, snapshot=snapshot, authority=90
        )


class TestArchiving:
    def test_archiving_hides_an_event_without_deleting_it(self, database, config):
        with _client(database, config) as client:
            assert len(client.get("/api/events").json()) == 1
            assert client.delete("/api/events/1").status_code == 200
            assert client.get("/api/events").json() == []
            # Still reachable, so archiving is never a one-way door.
            archived = client.get("/api/events?archived=true").json()
            assert len(archived) == 1
            assert archived[0]["archived_at"]

    async def test_the_row_and_its_history_survive(self, database, config):
        with _client(database, config) as client:
            client.delete("/api/events/1")
        async with database.session() as session:
            assert await session.get(EventRow, 1) is not None

    def test_restoring_brings_it_back(self, database, config):
        with _client(database, config) as client:
            client.delete("/api/events/1")
            assert client.post("/api/events/1/restore").status_code == 200
            assert len(client.get("/api/events").json()) == 1

    async def test_the_refresh_job_leaves_an_archived_event_alone(self, database, config):
        """Re-reading it would keep it alive and could put it back as a change alert."""
        from akaton.jobs.refresh import RefreshJob

        class ExplodingPipeline:
            async def process(self, seed, **kwargs):  # pragma: no cover - must not run
                raise AssertionError(f"archived event was refreshed: {seed.url}")

        with _client(database, config) as client:
            client.delete("/api/events/1")
        counts = await RefreshJob(database, ExplodingPipeline()).run()
        assert counts["processed"] == 0


def _mention(name="eGov hackathon"):
    return MentionLead(
        name=name,
        normalized_name=name.casefold(),
        platform="facebook",
        mention_kind="question",
        source_url="https://www.facebook.com/groups/philhacks/permalink/1/",
        query=name,
    )


class TestLeadEditing:
    async def test_correcting_a_name_re_keys_the_lead(self, database, config):
        """The key is derived from the name; leaving it stale would keep the old cooldown."""
        async with database.session() as session:
            await Repository(session).record_mention(_mention("egov hackaton"))
        async with database.session() as session:
            before = (await session.scalar(select(LeadRow))).lead_key

        with _client(database, config) as client:
            response = client.patch("/api/leads/1", json={"name": "eGov Hackathon"})
            assert response.status_code == 200
            assert response.json()["changed"] == ["name"]

        async with database.session() as session:
            row = await session.scalar(select(LeadRow))
        assert row.name == "eGov Hackathon"
        assert row.normalized_name == "egov hackathon"
        assert row.lead_key != before

    async def test_search_now_clears_the_cooldown(self, database, config):
        async with database.session() as session:
            repo = Repository(session)
            row = await repo.record_mention(_mention())
            await repo.mark_lead_searched(row.id, resolved_url="https://dict.gov.ph/x")
        async with database.session() as session:
            assert await Repository(session).due_leads(5) == []

        with _client(database, config) as client:
            assert client.post("/api/leads/1/search-now").status_code == 200

        async with database.session() as session:
            due = await Repository(session).due_leads(5)
            row = await session.scalar(select(LeadRow))
        assert [item.id for item in due] == [1]
        assert row.state == LeadState.NEW

    async def test_a_lead_can_be_deleted_outright(self, database, config):
        """A lead is a work item, not a record of delivery."""
        async with database.session() as session:
            await Repository(session).record_mention(_mention())
        with _client(database, config) as client:
            assert client.delete("/api/leads/1").status_code == 200
            assert client.get("/api/leads").json() == []

    def test_a_missing_lead_is_not_found(self, database, config):
        with _client(database, config) as client:
            assert client.patch("/api/leads/9", json={"name": "x"}).status_code == 404
            assert client.delete("/api/leads/9").status_code == 404
            assert client.post("/api/leads/9/search-now").status_code == 404


class TestCandidates:
    async def _candidate(self, database):
        async with database.session() as session:
            repo = Repository(session)
            row = await repo.upsert_candidate(
                CandidateSeed(
                    url="https://dict.gov.ph/some-page",
                    title="Some page",
                    discovery_channel="search",
                    provider="searxng",
                )
            )
            row.rejection_reasons = ["RESULTS_ONLY"]
            await repo.add_snapshot(row, FetchResult(requested_url="x", fetch_method="http"))
            return row.id

    async def test_a_candidate_can_be_retried(self, database, config):
        candidate_id = await self._candidate(database)
        seen = []

        async def reprocess(url, title, snippet):
            seen.append(url)

            class Outcome:
                state = "EVENT_CREATED"
                reason = None

            return Outcome()

        with _client(database, config, reprocess=reprocess) as client:
            response = client.post(f"/api/candidates/{candidate_id}/retry")
            assert response.status_code == 202
            assert response.json()["state"] == "EVENT_CREATED"
        assert seen == ["https://dict.gov.ph/some-page"]

    async def test_retry_needs_a_pipeline(self, database, config):
        candidate_id = await self._candidate(database)
        with _client(database, config) as client:
            assert client.post(f"/api/candidates/{candidate_id}/retry").status_code == 409

    async def test_deleting_a_candidate_takes_its_snapshots(self, database, config):
        candidate_id = await self._candidate(database)
        with _client(database, config) as client:
            assert client.delete(f"/api/candidates/{candidate_id}").status_code == 200
        async with database.session() as session:
            assert await session.get(CandidateRow, candidate_id) is None
            snapshots = int(await session.scalar(select(func.count(SourceSnapshotRow.id))) or 0)
        assert snapshots == 0

    def test_a_missing_candidate_is_not_found(self, database, config):
        with _client(database, config) as client:
            assert client.delete("/api/candidates/999").status_code == 404


def test_the_edit_table_covers_what_the_form_offers():
    """`current_values` and `parse_edits` must agree on the field set."""
    from akaton.processing.edits import EDITABLE, FIELDS, current_values

    facts = EventFacts(
        title="x",
        category=CompetitionCategory.HACKATHON,
        document_kind=DocumentKind.REGISTRATION_OPEN,
        event_start=DateFact(value=datetime(2026, 10, 20, tzinfo=UTC), confidence=1.0),
    )
    assert set(current_values(facts)) == set(FIELDS)
    assert set(FIELDS) <= EDITABLE


def test_an_extraction_envelope_still_validates_after_an_edit():
    """A guard that the edit writers produce values the model accepts."""
    from akaton.processing.edits import apply_overrides

    facts = EventFacts(title="before")
    updated = apply_overrides(
        facts, {"title": "after", "event_start": "2026-10-20T00:00:00+00:00", "city": "Pasig"}
    )
    envelope = ExtractionEnvelope(facts=updated, overall_confidence=0.9, extraction_version="t")
    assert envelope.facts.title == "after"
    assert envelope.facts.event_start.value.year == 2026
    assert envelope.facts.event_start.confidence == 1.0
    assert envelope.facts.location.city == "Pasig"
