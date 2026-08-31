from __future__ import annotations

import asyncio
import hashlib
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
    ExtractionEnvelope,
    FetchResult,
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
from akaton.processing.dedup import MatchDecision, compare_events
from akaton.processing.deterministic import extract_deterministically
from akaton.processing.llm import (
    LLMProvider,
    merge_extraction,
    should_escalate,
    should_use_llm,
)
from akaton.processing.relevance import is_plausibly_relevant, looks_like_old_news
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
        llm_providers: list[LLMProvider] | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.fetcher = fetcher
        # A ladder, tried in order: the everyday host first, then a better-resourced one
        # only if the first left the extraction thin. `llm=` stays as the one-tier
        # shorthand, which is what most deployments and every test want.
        self.llm_providers = list(llm_providers or ([llm] if llm else []))
        self.notifier = notifier
        # Escalations are counted for the life of this pipeline rather than per document,
        # because the cost being bounded is the fallback host's time, not any one page's.
        self._escalations = 0
        # Ollama serialises requests per model, so letting every parallel candidate fire
        # its own extraction only builds a queue on the server until clients time out.
        self._llm_limit = asyncio.Semaphore(config.app.llm_concurrency)

    async def _extract_with_models(
        self,
        context: DocumentContext,
        extraction: ExtractionEnvelope,
        candidate_id: int,
    ) -> tuple[ExtractionEnvelope, bool]:
        """Walk the model ladder, stopping as soon as the answer is good enough.

        The second host is only asked when the first left the extraction thin, so a clean
        page costs one small-model call and an unreachable host costs one connect timeout.
        Every pass is merged, never substituted, so a later model can fill gaps but cannot
        overwrite what was read directly or assert its own confidence.
        """
        used = False
        threshold = self.config.app.llm_escalation_confidence
        for index, provider in enumerate(self.llm_providers):
            if index and not should_escalate(extraction, threshold):
                break
            if index and self._escalations >= self.config.app.llm_escalations_per_run:
                logger.info(
                    "llm_escalation_budget_spent",
                    extra={"candidate_id": candidate_id, "cap": self._escalations},
                )
                break
            try:
                async with self._llm_limit:
                    completion = await provider.extract(context)
            except Exception as exc:
                logger.warning(
                    "llm_extraction_failed",
                    extra={
                        "candidate_id": candidate_id,
                        "provider": getattr(provider, "name", "?"),
                        "tier": index,
                        "error_type": type(exc).__name__,
                    },
                )
                # Fall through to the next tier. A refused connection here is the
                # sleeping-laptop case, and the point of having a second host.
                continue
            if index:
                self._escalations += 1
            extraction = merge_extraction(extraction, completion, context)
            used = True
        return extraction, used

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
            # The search result's own headline can already say the competition is over.
            # Rejecting here saves a fetch, an extraction and possibly a model call on a
            # document the verifier would reject anyway. A prefetched seed skips this:
            # its "title" is the first line of a social post, not a headline.
            if not seed.content and looks_like_old_news(seed.title, seed.snippet):
                await repo.transition_candidate(
                    candidate,
                    CandidateState.REJECTED,
                    detail={"reason": "stale_headline", "title": seed.title},
                    rejection_reasons=[RejectionCode.RESULTS_ONLY.value],
                )
                return PipelineOutcome(
                    candidate.id,
                    CandidateState.REJECTED.value,
                    candidate.event_id,
                    RejectionCode.RESULTS_ONLY.value,
                )
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

        if seed.content:
            fetch = _prefetched_result(seed)
        else:
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
        # Relevance first, thinness second. `should_use_llm` fires on unknown_category,
        # so without this an off-topic page is guaranteed a model call while a clean
        # event page never gets one.
        if self.llm_providers and is_plausibly_relevant(context) and should_use_llm(extraction):
            extraction, llm_used = await self._extract_with_models(
                context, extraction, candidate_id
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
                str(seed.url),
                fetch.final_url,
                fetch.links,
                fetch.metadata,
                sources=self.config.sources,
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
                # Fall back to the gate warnings before the generic bucket, so a
                # candidate that failed only on unconfirmed registration is countable.
                reasons = (
                    [item.value for item in verification.rejection_codes]
                    or verification.warnings
                    or ["AMBIGUOUS"]
                )
                state = (
                    CandidateState.REJECTED
                    if verification.rejection_codes
                    else CandidateState.AMBIGUOUS
                )
                if seed.lead:
                    # We found a page and it was not for us. DISCARDED rather than
                    # UNRESOLVED so the dashboard tells that apart from never finding one.
                    await repo.attach_lead_event(seed.lead.lead_id, None, kept=False)
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
                # URL identity, then content identity, then fuzzy title/organizer. The
                # middle rung is what catches one announcement reposted under several
                # URLs, which the outer two cannot see.
                event = await repo.find_by_content_fingerprint(extraction.facts)
            if not event:
                # Scan the whole pool before parking anything. Returning on the first
                # POSSIBLE_DUPLICATE abandoned the scan, so a genuine MERGE later in the
                # pool was never reached and the candidate died against a weaker match.
                parked: tuple[EventRow, MatchDecision] | None = None
                for existing in await repo.candidate_events(extraction.facts):
                    match = compare_events(
                        EventFacts.model_validate(existing.current_facts), extraction.facts
                    )
                    if match.action == "MERGE":
                        event = existing
                        break
                    if match.action == "POSSIBLE_DUPLICATE" and parked is None:
                        parked = (existing, match)
                if event is None and parked is not None:
                    existing, match = parked
                    await repo.transition_candidate(
                        candidate,
                        CandidateState.POSSIBLE_DUPLICATE,
                        detail={
                            "event_id": existing.id,
                            "match_score": match.score,
                            # compare_events already works these out and they were being
                            # thrown away, which is why parking here was silent.
                            "reasons": list(match.reasons),
                        },
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
            if seed.lead:
                # Close the loop, so the dashboard can show which mention produced which
                # event rather than a list of names with no outcome attached.
                await repo.attach_lead_event(seed.lead.lead_id, event.id, kept=True)

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
            # The threshold applies to a backfill too. `historical_test` relaxes the
            # past-event and registration-deadline gates — that is its documented job —
            # but it used to skip this check as well, so a backdate notified for every
            # new event at any score. Three of the eight events the live database had
            # stored scored 59, 64 and 64 against a threshold of 65 and alerted anyway.
            # The event is still created, which is what makes a backdate informative;
            # only the alert is suppressed.
            if is_new and score.total < threshold:
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
                    discovery_channel=seed.discovery_channel,
                    source_label=_source_label(seed),
                    source_url=seed.lead.source_url if seed.lead else None,
                    links=list(fetch.links or []),
                    published=seed.published_hint,
                    sources=self.config.sources,
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


PLATFORM_NAMES = {"facebook": "Facebook", "reddit": "Reddit", "shreddit": "Reddit"}


def _source_label(seed: CandidateSeed) -> str | None:
    """Name the place an alert came from, so a group post never reads as an official page."""
    if seed.discovery_channel == "facebook":
        return f"Facebook group · {seed.query}" if seed.query else "Facebook group post"
    if seed.discovery_channel == "reddit":
        return f"Reddit · {seed.query}" if seed.query else "Reddit post"
    if seed.lead:
        # An official page found by resolving a social mention. The document is what it
        # is — it keeps its own channel and its clickable official link — but the reader
        # should still know why it turned up, and where the mention was.
        platform = PLATFORM_NAMES.get(seed.lead.platform, seed.lead.platform.title())
        return f"Found via a {platform} mention"
    return None


def _prefetched_result(seed: CandidateSeed) -> FetchResult:
    """Wrap content an adapter already collected so it enters the pipeline unchanged.

    Every later stage still applies: the same extraction, the same verification gates and
    the same scoring. Only the network request is skipped.
    """
    text = seed.content or ""
    return FetchResult(
        requested_url=str(seed.url),
        final_url=str(seed.url),
        fetch_method="prefetched",
        status_code=200,
        content_type="text/plain",
        title=seed.title,
        text=text,
        links=list(seed.links or []),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        usable=bool(text.strip()),
    )


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
