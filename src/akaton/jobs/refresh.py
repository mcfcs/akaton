from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from akaton.domain.models import CandidateSeed
from akaton.persistence.database import Database
from akaton.persistence.models import EventRow
from akaton.pipeline import CandidatePipeline


class RefreshJob:
    def __init__(self, database: Database, pipeline: CandidatePipeline) -> None:
        self.database = database
        self.pipeline = pipeline

    async def run(self) -> dict[str, int]:
        async with self.database.session() as session:
            events = list(
                (
                    await session.scalars(
                        select(EventRow).where(
                            EventRow.event_phase.in_(
                                ["ANNOUNCED", "UPCOMING", "ONGOING", "POSTPONED", "UNKNOWN"]
                            )
                        )
                    )
                ).all()
            )
        processed = errors = 0
        for event in events:
            if not event.canonical_url:
                continue
            if not _refresh_due(event):
                continue
            seed = CandidateSeed(
                url=event.canonical_url,
                title=event.title,
                discovery_channel="refresh",
                provider="known_event_refresh",
                source_key=str(event.id),
            )
            try:
                await self.pipeline.process(seed)
                processed += 1
            except Exception:
                errors += 1
        return {"processed": processed, "errors": errors}


def _refresh_due(event: EventRow, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if event.last_verified_at is None:
        return True
    deadline_text = event.current_facts.get("registration_deadline", {}).get("value")
    deadline = datetime.fromisoformat(deadline_text) if deadline_text else None
    cadence = timedelta(days=1)
    if deadline and deadline - now > timedelta(days=30):
        cadence = timedelta(days=3)
    elif not deadline and event.registration_state != "OPEN":
        cadence = timedelta(days=3)
    last_verified = event.last_verified_at
    if last_verified.tzinfo is None:
        last_verified = last_verified.replace(tzinfo=UTC)
    return last_verified + cadence <= now
