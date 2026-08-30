from __future__ import annotations

from datetime import UTC, datetime, timedelta

from akaton.jobs.maintenance import MaintenanceJob
from akaton.persistence.database import Database
from akaton.persistence.models import CandidateRow, EventRow, SourceSnapshotRow


async def test_snapshot_retention_keeps_latest_and_event_evidence():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    old = datetime.now(UTC) - timedelta(days=120)
    async with database.session() as session:
        rejected = CandidateRow(
            discovered_url="https://example.com/rejected",
            normalized_url="https://example.com/rejected",
            discovery_channel="test",
            provider="test",
        )
        linked = CandidateRow(
            discovered_url="https://example.com/event",
            normalized_url="https://example.com/event",
            discovery_channel="test",
            provider="test",
        )
        event = EventRow(
            title="Event",
            normalized_title="event",
            category="HACKATHON",
            document_kind="EVENT_ANNOUNCEMENT",
            event_phase="UPCOMING",
            registration_state="OPEN",
            current_facts={},
            material_hash="hash",
        )
        session.add_all([rejected, linked, event])
        await session.flush()
        snapshots = [
            SourceSnapshotRow(
                candidate_id=rejected.id,
                requested_url=rejected.discovered_url,
                fetch_method="http",
                extracted_text="delete me",
                retrieved_at=old,
            ),
            SourceSnapshotRow(
                candidate_id=rejected.id,
                requested_url=rejected.discovered_url,
                fetch_method="http",
                extracted_text="latest rejected",
                retrieved_at=old,
            ),
            SourceSnapshotRow(
                candidate_id=linked.id,
                event_id=event.id,
                requested_url=linked.discovered_url,
                fetch_method="http",
                extracted_text="compact me",
                retrieved_at=old,
            ),
            SourceSnapshotRow(
                candidate_id=linked.id,
                event_id=event.id,
                requested_url=linked.discovered_url,
                fetch_method="http",
                extracted_text="latest event",
                retrieved_at=old,
            ),
        ]
        session.add_all(snapshots)
        await session.flush()
        snapshot_ids = [snapshot.id for snapshot in snapshots]

    result = await MaintenanceJob(database, 90).run()

    async with database.session() as session:
        assert await session.get(SourceSnapshotRow, snapshot_ids[0]) is None
        assert (await session.get(SourceSnapshotRow, snapshot_ids[1])).extracted_text == (
            "latest rejected"
        )
        assert (await session.get(SourceSnapshotRow, snapshot_ids[2])).extracted_text is None
        assert (await session.get(SourceSnapshotRow, snapshot_ids[3])).extracted_text == (
            "latest event"
        )
    assert result == {"deleted": 1, "compacted": 1}
    await database.close()
