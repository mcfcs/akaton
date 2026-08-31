from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from akaton.domain.enums import CandidateState, NotificationState
from akaton.domain.models import (
    CandidateSeed,
    EventFacts,
    ExtractionEnvelope,
    FetchResult,
    NotificationPayload,
)
from akaton.persistence.models import (
    CandidateRow,
    EventChangeRow,
    EventRow,
    EventSourceRow,
    EventVersionRow,
    NotificationRow,
    SearchRunRow,
    SourceSnapshotRow,
)
from akaton.processing.changes import detect_changes
from akaton.processing.dedup import (
    compare_events,
    content_prefix_hash,
    fingerprint_text,
    is_same_announcement,
)
from akaton.processing.normalize import normalize_url

# A repost can trail the original by weeks. Beyond this the pair is more likely to be a
# recurring event's next run, which the edition check would separate anyway.
CONTENT_MATCH_WINDOW_DAYS = 180


def _edition_compatible(row: EventRow, facts: EventFacts) -> bool:
    """Never merge two runs of an annual series that happen to share their wording."""
    return not (row.edition_year and facts.edition_year and row.edition_year != facts.edition_year)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def material_facts(facts: EventFacts) -> dict[str, Any]:
    value = facts.model_dump(mode="json")
    value.pop("description", None)
    return value


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_candidate(self, seed: CandidateSeed) -> CandidateRow:
        normalized = normalize_url(str(seed.url))
        existing = await self.session.scalar(
            select(CandidateRow).where(CandidateRow.normalized_url == normalized)
        )
        if existing:
            existing.last_seen_at = datetime.now(UTC)
            if seed.snippet:
                existing.snippet = seed.snippet
            return existing
        row = CandidateRow(
            discovered_url=str(seed.url),
            normalized_url=normalized,
            title=seed.title,
            snippet=seed.snippet,
            discovery_channel=seed.discovery_channel,
            provider=seed.provider,
            query=seed.query,
            source_key=seed.source_key,
            state=CandidateState.DISCOVERED.value,
            discovered_at=seed.discovered_at,
            last_seen_at=seed.discovered_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def transition_candidate(
        self,
        candidate: CandidateRow,
        state: CandidateState,
        *,
        detail: dict[str, Any] | None = None,
        rejection_reasons: list[str] | None = None,
    ) -> None:
        candidate.state = state.value
        trace = list(candidate.trace or [])
        trace.append({"at": datetime.now(UTC).isoformat(), "state": state.value, **(detail or {})})
        candidate.trace = trace
        if rejection_reasons is not None:
            candidate.rejection_reasons = rejection_reasons
        await self.session.flush()

    async def add_snapshot(
        self,
        candidate: CandidateRow,
        fetch: FetchResult,
        *,
        extraction_version: str | None = None,
    ) -> SourceSnapshotRow:
        row = SourceSnapshotRow(
            candidate_id=candidate.id,
            event_id=candidate.event_id,
            requested_url=fetch.requested_url,
            final_url=fetch.final_url,
            http_status=fetch.status_code,
            fetch_method=fetch.fetch_method,
            proxy_used=fetch.proxy_used,
            content_hash=fetch.content_hash,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            title=fetch.title,
            extracted_text=fetch.text,
            search_snippet=candidate.snippet,
            metadata_json=fetch.metadata,
            attempts_json=[item.model_dump(mode="json") for item in fetch.attempts],
            extraction_version=extraction_version,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def find_exact_event(self, facts: EventFacts) -> EventRow | None:
        clauses = []
        if facts.canonical_url:
            clauses.append(EventRow.canonical_url == normalize_url(facts.canonical_url))
        if facts.registration_url:
            clauses.append(EventRow.registration_url == normalize_url(facts.registration_url))
        if not clauses:
            return None
        matches = list((await self.session.scalars(select(EventRow).where(or_(*clauses)))).all())
        for found in matches:
            existing = EventFacts.model_validate(found.current_facts)
            if compare_events(existing, facts).action == "MERGE":
                return found
        return None

    async def find_by_content_fingerprint(self, facts: EventFacts) -> EventRow | None:
        """Find an event already stored under a different URL.

        The same announcement reaches a group several times — posted, shared from the
        organiser's page, reposted by a member — and each copy has its own URL, so
        `find_exact_event` cannot see them. A prefix hash catches verbatim reposts with
        an index lookup; a share with an introduction bolted on needs the similarity
        check over recent events.
        """
        text = fingerprint_text(facts)
        digest = content_prefix_hash(text)
        if not digest:
            return None
        matches = list(
            (
                await self.session.scalars(
                    select(EventRow).where(EventRow.content_prefix_hash == digest).limit(20)
                )
            ).all()
        )
        for found in matches:
            if _edition_compatible(found, facts):
                return found

        cutoff = datetime.now(UTC) - timedelta(days=CONTENT_MATCH_WINDOW_DAYS)
        recent = list(
            (
                await self.session.scalars(
                    select(EventRow)
                    .where(EventRow.last_seen_at >= cutoff)
                    .order_by(EventRow.last_seen_at.desc())
                    .limit(500)
                )
            ).all()
        )
        for found in recent:
            if not _edition_compatible(found, facts):
                continue
            stored = EventFacts.model_validate(found.current_facts)
            if is_same_announcement(text, fingerprint_text(stored)):
                return found
        return None

    async def candidate_events(self, facts: EventFacts) -> list[EventRow]:
        query = select(EventRow)
        if facts.organizer_normalized:
            query = query.where(EventRow.organizer_normalized == facts.organizer_normalized)
        elif facts.series_key:
            query = query.where(EventRow.series_key == facts.series_key)
        else:
            query = query.where(EventRow.normalized_title == (facts.normalized_title or ""))
        return list((await self.session.scalars(query.limit(50))).all())

    async def create_event(
        self,
        extraction: ExtractionEnvelope,
        *,
        relevance_score: int,
        snapshot: SourceSnapshotRow,
        authority: int,
    ) -> EventRow:
        facts = extraction.facts
        facts_json = facts.model_dump(mode="json")
        digest = stable_hash(material_facts(facts))
        row = EventRow(
            title=facts.title or "Untitled competition",
            normalized_title=facts.normalized_title or "",
            organizer=facts.organizer,
            organizer_normalized=facts.organizer_normalized,
            category=facts.category.value,
            document_kind=facts.document_kind.value,
            event_phase=facts.event_phase.value,
            registration_state=facts.registration_state.value,
            canonical_url=normalize_url(facts.canonical_url) if facts.canonical_url else None,
            registration_url=normalize_url(facts.registration_url)
            if facts.registration_url
            else None,
            series_key=facts.series_key,
            edition_key=facts.edition_key,
            edition_year=facts.edition_year,
            current_facts=facts_json,
            relevance_score=relevance_score,
            confidence_score=extraction.overall_confidence,
            material_hash=digest,
            content_prefix_hash=content_prefix_hash(fingerprint_text(facts)),
            last_verified_at=datetime.now(UTC),
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add(
            EventVersionRow(
                event_id=row.id,
                version=1,
                facts_json=facts_json,
                evidence_json=[item.model_dump(mode="json") for item in extraction.evidence],
                material_hash=digest,
                extraction_version=extraction.extraction_version,
            )
        )
        self.session.add(
            EventSourceRow(
                event_id=row.id,
                snapshot_id=snapshot.id,
                role="canonical",
                authority=authority,
                is_canonical=True,
            )
        )
        snapshot.event_id = row.id
        await self.session.flush()
        return row

    async def attach_source(
        self,
        event: EventRow,
        snapshot: SourceSnapshotRow,
        *,
        authority: int,
        role: str = "supporting",
    ) -> None:
        exists = await self.session.scalar(
            select(EventSourceRow).where(
                EventSourceRow.event_id == event.id, EventSourceRow.snapshot_id == snapshot.id
            )
        )
        if not exists:
            self.session.add(
                EventSourceRow(
                    event_id=event.id, snapshot_id=snapshot.id, role=role, authority=authority
                )
            )
        snapshot.event_id = event.id
        event.last_seen_at = datetime.now(UTC)
        await self.session.flush()

    async def update_event(
        self,
        event: EventRow,
        extraction: ExtractionEnvelope,
        *,
        relevance_score: int,
        snapshot: SourceSnapshotRow,
        authority: int,
    ) -> list[EventChangeRow]:
        max_authority = await self.session.scalar(
            select(func.coalesce(func.max(EventSourceRow.authority), 0)).where(
                EventSourceRow.event_id == event.id
            )
        )
        facts = extraction.facts
        digest = stable_hash(material_facts(facts))
        await self.attach_source(event, snapshot, authority=authority)
        if digest == event.material_hash or authority < int(max_authority or 0):
            return []
        before = EventFacts.model_validate(event.current_facts)
        changes = detect_changes(before, facts)
        next_version = event.current_version + 1
        facts_json = facts.model_dump(mode="json")
        event.title = facts.title or event.title
        event.normalized_title = facts.normalized_title or event.normalized_title
        event.organizer = facts.organizer
        event.organizer_normalized = facts.organizer_normalized
        event.category = facts.category.value
        event.document_kind = facts.document_kind.value
        event.event_phase = facts.event_phase.value
        event.registration_state = facts.registration_state.value
        event.canonical_url = (
            normalize_url(facts.canonical_url) if facts.canonical_url else event.canonical_url
        )
        event.registration_url = (
            normalize_url(facts.registration_url)
            if facts.registration_url
            else event.registration_url
        )
        event.series_key = facts.series_key
        event.edition_key = facts.edition_key
        event.edition_year = facts.edition_year
        event.current_facts = facts_json
        event.relevance_score = relevance_score
        event.confidence_score = extraction.overall_confidence
        event.current_version = next_version
        event.material_hash = digest
        event.content_prefix_hash = content_prefix_hash(fingerprint_text(facts))
        event.last_verified_at = datetime.now(UTC)
        self.session.add(
            EventVersionRow(
                event_id=event.id,
                version=next_version,
                facts_json=facts_json,
                evidence_json=[item.model_dump(mode="json") for item in extraction.evidence],
                material_hash=digest,
                extraction_version=extraction.extraction_version,
            )
        )
        rows = []
        for change in changes:
            row = EventChangeRow(
                event_id=event.id,
                change_type=change.change_type.value,
                field_name=change.field,
                before_json=json.loads(json.dumps(change.before, default=str)),
                after_json=json.loads(json.dumps(change.after, default=str)),
                notify=change.notify,
            )
            self.session.add(row)
            rows.append(row)
        await self.session.flush()
        return rows

    async def reserve_notification(
        self, payload: NotificationPayload, *, event_change_id: int | None = None
    ) -> NotificationRow | None:
        existing = await self.session.scalar(
            select(NotificationRow).where(NotificationRow.dedupe_key == payload.dedupe_key)
        )
        if existing:
            return None
        value = payload.model_dump(mode="json")
        row = NotificationRow(
            event_id=payload.event_id,
            event_change_id=event_change_id,
            notification_type=payload.notification_type,
            dedupe_key=payload.dedupe_key,
            state=NotificationState.PENDING.value,
            event_version=payload.event_version,
            payload_hash=stable_hash(value),
            payload_json=value,
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return None
        return row

    async def mark_notification_sent(self, row: NotificationRow, message_id: str) -> None:
        row.state = NotificationState.SENT.value
        row.discord_message_id = message_id
        row.sent_at = datetime.now(UTC)
        row.attempts += 1
        await self.session.flush()

    async def mark_notification_failed(self, row: NotificationRow, error: str) -> None:
        row.state = NotificationState.FAILED.value
        row.last_error = error[:2000]
        row.attempts += 1
        await self.session.flush()

    async def pending_notifications(self) -> list[NotificationRow]:
        return list(
            (
                await self.session.scalars(
                    select(NotificationRow).where(
                        NotificationRow.state == NotificationState.PENDING.value
                    )
                )
            ).all()
        )

    async def has_recent_change_notification(
        self, event_id: int, *, now: datetime | None = None
    ) -> bool:
        cutoff = (now or datetime.now(UTC)) - timedelta(minutes=30)
        row = await self.session.scalar(
            select(NotificationRow.id).where(
                NotificationRow.event_id == event_id,
                NotificationRow.notification_type != "NEW_EVENT",
                NotificationRow.created_at >= cutoff,
            )
        )
        return row is not None

    async def record_search_run(
        self, provider: str, group: str, query: str, result_count: int, error: str | None = None
    ) -> None:
        self.session.add(
            SearchRunRow(
                provider=provider,
                query_group=group,
                query=query,
                status="FAILED" if error else "SUCCEEDED",
                result_count=result_count,
                error=error,
                completed_at=datetime.now(UTC),
            )
        )
        await self.session.flush()

    async def monthly_search_requests(self, provider: str, since: datetime) -> int:
        result = await self.session.scalar(
            select(func.coalesce(func.sum(SearchRunRow.request_count), 0)).where(
                SearchRunRow.provider == provider,
                SearchRunRow.started_at >= since,
            )
        )
        return int(result or 0)

    async def search_history(self, provider: str) -> dict[tuple[str, str], datetime]:
        rows = (
            await self.session.execute(
                select(
                    SearchRunRow.query_group, SearchRunRow.query, func.max(SearchRunRow.started_at)
                )
                .where(SearchRunRow.provider == provider)
                .group_by(SearchRunRow.query_group, SearchRunRow.query)
            )
        ).all()
        return {(group, query): last for group, query, last in rows}
