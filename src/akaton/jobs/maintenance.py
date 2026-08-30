from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update

from akaton.persistence.database import Database
from akaton.persistence.models import SourceSnapshotRow


class MaintenanceJob:
    """Apply retention without removing each candidate's latest fetch record."""

    def __init__(self, database: Database, retention_days: int) -> None:
        self.database = database
        self.retention_days = retention_days

    async def run(self) -> dict[str, int]:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        latest_ids = select(func.max(SourceSnapshotRow.id)).group_by(SourceSnapshotRow.candidate_id)
        async with self.database.session() as session:
            deleted = await session.execute(
                delete(SourceSnapshotRow).where(
                    SourceSnapshotRow.retrieved_at < cutoff,
                    SourceSnapshotRow.event_id.is_(None),
                    SourceSnapshotRow.id.not_in(latest_ids),
                )
            )
            compacted = await session.execute(
                update(SourceSnapshotRow)
                .where(
                    SourceSnapshotRow.retrieved_at < cutoff,
                    SourceSnapshotRow.event_id.is_not(None),
                    SourceSnapshotRow.id.not_in(latest_ids),
                    SourceSnapshotRow.extracted_text.is_not(None),
                )
                .values(extracted_text=None)
            )
        return {
            "deleted": int(deleted.rowcount or 0),
            "compacted": int(compacted.rowcount or 0),
        }
