from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from akaton.domain.enums import CandidateState, NotificationState
from akaton.domain.models import (
    CandidateSeed,
    DateFact,
    EventFacts,
    ExtractionEnvelope,
    FetchResult,
    MentionLead,
    NotificationPayload,
)
from akaton.persistence.models import (
    CandidateRow,
    EventChangeRow,
    EventRow,
    EventSourceRow,
    EventVersionRow,
    LeadRow,
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
from akaton.processing.editions import dates_contradict, editions_conflict
from akaton.processing.edits import FIELDS, ROW_FIELDS, apply_overrides
from akaton.processing.leads import LeadState, lead_key
from akaton.processing.leads import is_due as is_lead_due
from akaton.processing.normalize import normalize_url

# A repost can trail the original by weeks. Beyond this the pair is more likely to be a
# recurring event's next run, which the edition check would separate anyway.
CONTENT_MATCH_WINDOW_DAYS = 180


def _edition_compatible(row: EventRow, facts: EventFacts) -> bool:
    """Never merge two runs of a recurring series that happen to share their wording.

    The fingerprint rungs match on text alone, so without this a September announcement
    worded like March's collapses onto it and never alerts. `current_facts` is already
    JSON on the row, so reading the stored start costs no extra query.
    """
    if row.edition_year and facts.edition_year and row.edition_year != facts.edition_year:
        return False
    if editions_conflict(row.edition_key, facts.edition_key):
        return False
    return not dates_contradict(_stored_start(row), facts.event_start)


def _stored_start(row: EventRow) -> DateFact | None:
    value = (row.current_facts or {}).get("event_start")
    if not isinstance(value, dict):
        return None
    try:
        return DateFact.model_validate(value)
    except ValidationError:
        return None


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a timestamp read back from SQLite, which stores no offset."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def material_facts(facts: EventFacts) -> dict[str, Any]:
    """The facts a change is judged on. Presentation is excluded.

    `description` and `image_url` are how the event is shown, not what it is. Hashing
    them would version every event whenever a site reworded its blurb or swapped its
    banner, and each of those versions would be checked for change notifications.
    """
    value = facts.model_dump(mode="json")
    value.pop("description", None)
    value.pop("image_url", None)
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
        # Pinned corrections go back on before anything else looks at the facts, so the
        # material hash reflects what will actually be stored. A page edit that only
        # touches a pinned field therefore produces no new version and no change alert,
        # which is what "pinned" has to mean.
        facts = apply_overrides(extraction.facts, event.manual_overrides or {})
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

    async def apply_manual_edit(self, event: EventRow, edits: dict[str, Any]) -> list[str]:
        """Correct an event by hand and pin what was corrected.

        Goes through the same versioning path as an automatic update — a new
        `EventVersionRow`, `detect_changes` into `EventChangeRow` — so the history stays
        complete and shows that a person did this. Returns the field names that actually
        changed, which is empty when the submitted values already matched.
        """
        before = EventFacts.model_validate(event.current_facts)
        facts = before.model_copy(deep=True)
        overrides = dict(event.manual_overrides or {})
        touched: list[str] = []

        for name, value in edits.items():
            if name in ROW_FIELDS:
                if getattr(event, name) != value:
                    setattr(event, name, value)
                    touched.append(name)
                continue
            spec = FIELDS[name]
            if spec.read(facts) == value and name in overrides:
                continue
            spec.write(facts, value)
            overrides[name] = value
            touched.append(name)

        if not touched:
            return []

        digest = stable_hash(material_facts(facts))
        changes = detect_changes(before, facts)
        event.manual_overrides = overrides
        event.current_facts = facts.model_dump(mode="json")
        event.title = facts.title or event.title
        event.normalized_title = facts.normalized_title or event.normalized_title
        event.organizer = facts.organizer
        event.organizer_normalized = facts.organizer_normalized
        event.category = facts.category.value
        event.canonical_url = (
            normalize_url(facts.canonical_url) if facts.canonical_url else event.canonical_url
        )
        event.registration_url = (
            normalize_url(facts.registration_url)
            if facts.registration_url
            else event.registration_url
        )
        event.edition_key = facts.edition_key
        event.edition_year = facts.edition_year
        event.material_hash = digest
        event.current_version += 1
        self.session.add(
            EventVersionRow(
                event_id=event.id,
                version=event.current_version,
                facts_json=event.current_facts,
                evidence_json=[],
                material_hash=digest,
                extraction_version="manual",
            )
        )
        for change in changes:
            self.session.add(
                EventChangeRow(
                    event_id=event.id,
                    change_type=change.change_type.value,
                    field_name=change.field,
                    before_json=json.loads(json.dumps(change.before, default=str)),
                    after_json=json.loads(json.dumps(change.after, default=str)),
                    # A person already knows what they just typed.
                    notify=False,
                )
            )
        await self.session.flush()
        return touched

    async def release_override(self, event: EventRow, field: str) -> bool:
        """Hand a field back to automatic extraction. The value stays until next refresh."""
        overrides = dict(event.manual_overrides or {})
        if field not in overrides:
            return False
        overrides.pop(field)
        event.manual_overrides = overrides
        await self.session.flush()
        return True

    async def set_archived(self, event: EventRow, archived: bool) -> None:
        event.archived_at = datetime.now(UTC) if archived else None
        await self.session.flush()

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

    async def record_mention(self, mention: MentionLead) -> LeadRow:
        """Record that someone named a competition, without spending a search on it.

        A second sighting of the same name only increments a counter: twenty people
        asking about eGovPH is one competition and must cost one search, not twenty. A
        mention that names an *edition* — a year or month beside the name — has a
        different key, so a genuinely new run is a new lead rather than a suppressed one.
        """
        key = lead_key(mention.normalized_name, mention.edition_hint)
        existing = await self.session.scalar(select(LeadRow).where(LeadRow.lead_key == key))
        if existing:
            existing.sightings += 1
            existing.last_seen_at = datetime.now(UTC)
            return existing
        row = LeadRow(
            lead_key=key,
            name=mention.name,
            normalized_name=mention.normalized_name,
            edition_hint=mention.edition_hint,
            platform=mention.platform,
            mention_kind=mention.mention_kind,
            source_url=mention.source_url,
            source_key=mention.source_key,
            mention_excerpt=(mention.excerpt or "")[:500] or None,
            state=LeadState.NEW,
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # Two collectors saw the same name in one run.
            await self.session.rollback()
            return await self.session.scalar(select(LeadRow).where(LeadRow.lead_key == key))
        return row

    async def due_leads(self, limit: int, *, now: datetime | None = None) -> list[LeadRow]:
        """Leads that have earned a search request, most recently seen first.

        The cooldown reads the lead's own `last_searched_at` rather than `search_history`
        so a lead deferred for budget is never mistaken for one already tried.
        """
        if limit <= 0:
            return []
        rows = list(
            (
                await self.session.scalars(
                    select(LeadRow)
                    .order_by(LeadRow.last_searched_at.is_(None).desc(), LeadRow.sightings.desc())
                    .limit(limit * 8)
                )
            ).all()
        )
        due = [
            row
            for row in rows
            if is_lead_due(row.state, row.search_runs, row.last_searched_at, now=now)
        ]
        return due[:limit]

    async def mark_lead_searched(
        self, lead_id: int, *, resolved_url: str | None, error: str | None = None
    ) -> None:
        """Record the outcome of a lead's search and start its cooldown."""
        row = await self.session.get(LeadRow, lead_id)
        if row is None:
            return
        row.search_runs += 1
        row.last_searched_at = datetime.now(UTC)
        row.resolved_url = resolved_url or row.resolved_url
        row.last_error = error
        row.state = LeadState.RESOLVED if resolved_url else LeadState.UNRESOLVED
        await self.session.flush()

    async def attach_lead_event(self, lead_id: int, event_id: int | None, *, kept: bool) -> None:
        """Say what became of a resolved page.

        DISCARDED is kept distinct from UNRESOLVED so the dashboard can tell "we never
        found it" from "we found it and it was not for us" — two different problems.
        """
        row = await self.session.get(LeadRow, lead_id)
        if row is None:
            return
        row.event_id = event_id
        row.state = LeadState.RESOLVED if kept else LeadState.DISCARDED
        await self.session.flush()

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
        """When each query last actually ran, for the cadence rotation.

        Only successful runs count. A query that failed did not get its answer, so
        letting it record a cadence slot would park it for another 6 to 72 hours over
        an outage it had no part in — the queries hit hardest by throttling being
        exactly the ones then waiting longest to be retried.
        """
        rows = (
            await self.session.execute(
                select(
                    SearchRunRow.query_group, SearchRunRow.query, func.max(SearchRunRow.started_at)
                )
                .where(
                    SearchRunRow.provider == provider,
                    SearchRunRow.status == "SUCCEEDED",
                )
                .group_by(SearchRunRow.query_group, SearchRunRow.query)
            )
        ).all()
        # SQLite has no timezone type: `DateTime(timezone=True)` round-trips through a
        # string with no offset, so these come back naive. Both callers compare them
        # against an aware `now` — `choose_due_queries` and the adapter cadence check —
        # and a naive/aware comparison raises TypeError, which took out every scheduled
        # discovery run after the first search was ever recorded.
        return {(group, query): as_utc(last) for group, query, last in rows if last}
