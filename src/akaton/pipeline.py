from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import select

from akaton.config import ConfigBundle
from akaton.discord.embeds import build_change_payload, build_new_event_payload
from akaton.domain.enums import CandidateState, FailureCode, RejectionCode
from akaton.domain.models import (
    CandidateSeed,
    DocumentContext,
    EventFacts,
    NotificationPayload,
)
from akaton.fetch.manager import FetchManager
from akaton.persistence.database import Database
from akaton.persistence.models import (
    CandidateRow,
    EventRow,
    NotificationRow,
    SourceSnapshotRow,
)
from akaton.persistence.repository import Repository
from akaton.processing.authority import authority_for_url
from akaton.processing.canonical import choose_urls
from akaton.processing.dedup import compare_events
from akaton.processing.deterministic import extract_deterministically
from akaton.processing.llm import LLMProvider, should_use_llm
from akaton.processing.scorer import score_event
from akaton.processing.verifier import verify_event

logger = logging.getLogger(__name__)


class Notifier:
    async def send(
        self, payload: NotificationPayload
    ):  # pragma: no cover - protocol-like runtime base
        raise NotImplementedError


@dataclass(frozen=True)
class PipelineOutcome:
    candidate_id: int
    state: str
    event_id: int | None = None
    reason: str | None = None


class CandidatePipeline:
    def __init__(
        self,
        database: Database,
        config: ConfigBundle,
        fetcher: FetchManager,
        *,
        llm: LLMProvider | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.fetcher = fetcher
        self.llm = llm
        self.notifier = notifier
        # Ollama serialises requests per model, so letting every parallel candidate fire
        # its own extraction only builds a queue on the server until clients time out.
        self._llm_limit = asyncio.Semaphore(config.app.llm_concurrency)

    async def process(
        self, seed: CandidateSeed, *, historical_test: bool = False
    ) -> PipelineOutcome:
        async with self.database.session() as session:
            repo = Repository(session)
            candidate = await repo.upsert_candidate(seed)
            if candidate.retry_at and _as_aware(candidate.retry_at) > datetime.now(UTC):
                return PipelineOutcome(
                    candidate.id,
                    CandidateState.FETCH_DEFERRED.value,
                    candidate.event_id,
                    "retry_not_due",
                )
            candidate.retry_at = None
            await repo.transition_candidate(candidate, CandidateState.NORMALIZED)
            if historical_test:
                await repo.transition_candidate(
                    candidate,
                    CandidateState.PREFILTERED,
                    detail={"mode": "historical_test", "time_gates_bypassed": True},
                )
            candidate_id = candidate.id
            previous = await session.scalar(
                select(SourceSnapshotRow)
                .where(SourceSnapshotRow.candidate_id == candidate_id)
                .order_by(SourceSnapshotRow.retrieved_at.desc())
                .limit(1)
            )
            etag = previous.etag if previous else None
            last_modified = previous.last_modified if previous else None

        fetch = await self.fetcher.fetch(str(seed.url), etag=etag, last_modified=last_modified)
        async with self.database.session() as session:
            repo = Repository(session)
            candidate = await session.get(CandidateRow, candidate_id)
            assert candidate is not None
            snapshot = await repo.add_snapshot(candidate, fetch)
            snapshot_id = snapshot.id
            if fetch.unchanged:
                if candidate.event_id:
                    event = await session.get(EventRow, candidate.event_id)
                    if event:
                        event.last_seen_at = datetime.now(UTC)
                await repo.transition_candidate(
                    candidate, CandidateState.SUPPRESSED, detail={"reason": "unchanged"}
                )
                return PipelineOutcome(
                    candidate_id, CandidateState.SUPPRESSED.value, candidate.event_id, "unchanged"
                )
            if fetch.failure in {FailureCode.HTTP_429, FailureCode.RATE_LIMITED}:
                candidate.retry_at = _retry_at(fetch.headers.get("retry-after"))
                await repo.transition_candidate(
                    candidate,
                    CandidateState.FETCH_DEFERRED,
                    detail={
                        "failure": fetch.failure.value,
                        "retry_at": candidate.retry_at.isoformat(),
                    },
                )
                return PipelineOutcome(
                    candidate_id,
                    CandidateState.FETCH_DEFERRED.value,
                    candidate.event_id,
                    fetch.failure.value,
                )
            if fetch.failure is FailureCode.FETCH_DISABLED:
                # A configured domain block is a policy decision, not a fetch failure.
                # The search snippet is all this source will ever give us.
                await repo.transition_candidate(
                    candidate,
                    CandidateState.REJECTED,
                    detail={"reason": "fetch_disabled_domain"},
                    rejection_reasons=[RejectionCode.SEARCH_SNIPPET_ONLY.value],
                )
                return PipelineOutcome(
                    candidate_id,
                    CandidateState.REJECTED.value,
                    candidate.event_id,
                    RejectionCode.SEARCH_SNIPPET_ONLY.value,
                )
            if not fetch.usable:
                await repo.transition_candidate(
                    candidate,
                    CandidateState.FETCH_FAILED,
                    detail={"failure": fetch.failure.value if fetch.failure else "unknown"},
                    rejection_reasons=["FETCH_FAILED"],
                )
                return PipelineOutcome(
                    candidate_id, CandidateState.FETCH_FAILED.value, reason=str(fetch.failure)
                )
            await repo.transition_candidate(
                candidate, CandidateState.FETCHED, detail={"method": fetch.fetch_method}
            )

        # Extraction runs outside any session. An LLM call takes tens of seconds, and
        # holding a SQLite write transaction across it stalls every other candidate
        # processed in parallel until they fail on a locked database.
        context = DocumentContext(
            url=fetch.final_url or str(seed.url),
            title=fetch.title or seed.title,
            text=fetch.text or "",
            snippet=seed.snippet,
            metadata=fetch.metadata,
            links=fetch.links,
        )
        extraction = extract_deterministically(context, published=seed.published_hint)
        llm_used = False
        if self.llm and should_use_llm(extraction):
            try:
                async with self._llm_limit:
                    extraction = await self.llm.extract(context)
                llm_used = True
            except Exception as exc:
                logger.warning(
                    "llm_extraction_failed",
                    extra={"candidate_id": candidate_id, "error_type": type(exc).__name__},
                )

        async with self.database.session() as session:
            repo = Repository(session)
            candidate = await session.get(CandidateRow, candidate_id)
            snapshot = await session.get(SourceSnapshotRow, snapshot_id)
            assert candidate is not None and snapshot is not None
            await repo.transition_candidate(
                candidate,
                CandidateState.EXTRACTED,
                detail={
                    "document_kind": extraction.facts.document_kind.value,
                    "confidence": extraction.overall_confidence,
                    "llm_used": llm_used,
                },
            )
            canonical, registration = choose_urls(
                str(seed.url), fetch.final_url, fetch.links, fetch.metadata
            )
            extraction.facts.canonical_url = canonical
            extraction.facts.registration_url = registration or extraction.facts.registration_url
            authority = authority_for_url(
                canonical, self.config.sources, discovery_channel=seed.discovery_channel
            )
            verification = verify_event(
                extraction,
                self.config.profile,
                source_authority=authority,
                allow_historical=historical_test,
            )
            if not verification.accepted:
                reasons = [item.value for item in verification.rejection_codes] or ["AMBIGUOUS"]
                state = (
                    CandidateState.REJECTED
                    if verification.rejection_codes
                    else CandidateState.AMBIGUOUS
                )
                await repo.transition_candidate(
                    candidate,
                    state,
                    rejection_reasons=reasons,
                    detail={"gates": verification.gate_results},
                )
                return PipelineOutcome(candidate_id, state.value, reason=",".join(reasons))

            await repo.transition_candidate(candidate, CandidateState.VERIFIED)
            score = score_event(
                extraction.facts,
                self.config.profile,
                self.config.scoring,
                source_authority=authority,
            )
            await repo.transition_candidate(
                candidate,
                CandidateState.SCORED,
                detail={"score": score.total, "tier": score.tier},
            )

            event = await repo.find_exact_event(extraction.facts)
            if not event:
                possible = await repo.candidate_events(extraction.facts)
                for existing in possible:
                    match = compare_events(
                        EventFacts.model_validate(existing.current_facts), extraction.facts
                    )
                    if match.action == "MERGE":
                        event = existing
                        break
                    if match.action == "POSSIBLE_DUPLICATE":
                        await repo.transition_candidate(
                            candidate,
                            CandidateState.POSSIBLE_DUPLICATE,
                            detail={"event_id": existing.id, "match_score": match.score},
                            rejection_reasons=["POSSIBLE_DUPLICATE"],
                        )
                        return PipelineOutcome(
                            candidate_id, CandidateState.POSSIBLE_DUPLICATE.value, existing.id
                        )

            is_new = event is None
            if is_new:
                changes = []
                event = await repo.create_event(
                    extraction, relevance_score=score.total, snapshot=snapshot, authority=authority
                )
                await repo.transition_candidate(
                    candidate, CandidateState.EVENT_CREATED, detail={"event_id": event.id}
                )
            else:
                changes = await repo.update_event(
                    event,
                    extraction,
                    relevance_score=score.total,
                    snapshot=snapshot,
                    authority=authority,
                )
                await repo.transition_candidate(
                    candidate, CandidateState.EVENT_MATCHED, detail={"event_id": event.id}
                )
            candidate.event_id = event.id

            threshold = int(self.config.scoring.get("thresholds", {}).get("recommended", 65))
            notify_changes = [] if is_new else [change for change in changes if change.notify]
            if not is_new and not notify_changes:
                await repo.transition_candidate(
                    candidate,
                    CandidateState.SUPPRESSED,
                    detail={"reason": "existing_event", "score": score.total},
                )
                return PipelineOutcome(
                    candidate_id,
                    CandidateState.SUPPRESSED.value,
                    event.id,
                    "existing_event",
                )
            if is_new and score.total < threshold and not historical_test:
                await repo.transition_candidate(
                    candidate,
                    CandidateState.SUPPRESSED,
                    detail={"reason": "low_relevance", "score": score.total},
                )
                return PipelineOutcome(
                    candidate_id,
                    CandidateState.SUPPRESSED.value,
                    event.id,
                    "low_relevance",
                )
            if is_new:
                payload = build_new_event_payload(
                    event.id,
                    event.current_version,
                    extraction.facts,
                    score,
                    extraction.overall_confidence,
                )
                change_id = None
            else:
                if await repo.has_recent_change_notification(event.id):
                    await repo.transition_candidate(
                        candidate,
                        CandidateState.SUPPRESSED,
                        detail={"reason": "change_debounce"},
                    )
                    return PipelineOutcome(
                        candidate_id,
                        CandidateState.SUPPRESSED.value,
                        event.id,
                        "change_debounce",
                    )
                payload = build_change_payload(
                    event.id, event.current_version, extraction.facts, notify_changes
                )
                change_id = notify_changes[0].id
            if not self.config.app.notifications_enabled or not self.notifier:
                await repo.transition_candidate(
                    candidate,
                    CandidateState.SUPPRESSED,
                    detail={"reason": "shadow_mode", "score": score.total},
                )
                return PipelineOutcome(
                    candidate_id, CandidateState.SUPPRESSED.value, event.id, "shadow_mode"
                )
            notification = await repo.reserve_notification(payload, event_change_id=change_id)
            if notification is None:
                await repo.transition_candidate(
                    candidate,
                    CandidateState.SUPPRESSED,
                    detail={"reason": "notification_duplicate"},
                )
                return PipelineOutcome(
                    candidate_id,
                    CandidateState.SUPPRESSED.value,
                    event.id,
                    "notification_duplicate",
                )
            notification_id = notification.id
            await repo.transition_candidate(candidate, CandidateState.NOTIFICATION_PENDING)

        try:
            receipt = await self.notifier.send(payload)
        except Exception as exc:
            async with self.database.session() as session:
                repo = Repository(session)
                notification = await session.get(NotificationRow, notification_id)
                candidate = await session.get(CandidateRow, candidate_id)
                if notification:
                    await repo.mark_notification_failed(
                        notification, f"{type(exc).__name__}: {exc}"
                    )
                if candidate:
                    await repo.transition_candidate(
                        candidate,
                        CandidateState.NOTIFICATION_PENDING,
                        detail={"delivery_error": type(exc).__name__},
                    )
            return PipelineOutcome(
                candidate_id,
                CandidateState.NOTIFICATION_PENDING.value,
                payload.event_id,
                "delivery_failed",
            )

        async with self.database.session() as session:
            repo = Repository(session)
            notification = await session.get(NotificationRow, notification_id)
            candidate = await session.get(CandidateRow, candidate_id)
            if notification:
                await repo.mark_notification_sent(notification, receipt.message_id)
            if candidate:
                await repo.transition_candidate(candidate, CandidateState.NOTIFIED)
        return PipelineOutcome(candidate_id, CandidateState.NOTIFIED.value, payload.event_id)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _retry_at(value: str | None, *, now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    if not value:
        return current + timedelta(hours=1)
    try:
        seconds = max(1, min(int(value), 24 * 60 * 60))
        return current + timedelta(seconds=seconds)
    except ValueError:
        try:
            parsed = _as_aware(parsedate_to_datetime(value))
            return max(current + timedelta(seconds=1), parsed)
        except (TypeError, ValueError, OverflowError):
            return current + timedelta(hours=1)
