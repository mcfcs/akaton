# ruff: noqa: E501
from __future__ import annotations

import secrets
from datetime import date
from functools import partial
from time import monotonic
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, func, select

from akaton.config import ConfigBundle
from akaton.dashboard.actions import build_manual_payload, record_manual_notification
from akaton.dashboard.runtime import BotController, MonitorController
from akaton.dashboard.settings import (
    SettingsError,
    _describe_change,
    current_settings,
    describe_settings,
    update_settings,
)
from akaton.discord.embeds import displayable_image, organizer_icon, summarise
from akaton.domain.models import EventFacts
from akaton.persistence.database import Database
from akaton.persistence.models import (
    CandidateRow,
    EventRow,
    LeadRow,
    NotificationRow,
    SearchRunRow,
    SourceSnapshotRow,
)
from akaton.persistence.repository import Repository
from akaton.processing.edits import EditError, current_values, parse_edits
from akaton.processing.leads import LeadState, lead_key
from akaton.processing.mentions import canonical_token

# Jobs the dashboard may stop. Named rather than free-form so a typo cannot cancel
# something that is not a job, and so the set is visible in one place.
CANCELLABLE_JOBS = frozenset({"discovery", "refresh", "backfill"})


def llm_host_id(provider) -> str:
    """A short, stable name for a model host: its network location."""
    base = getattr(provider, "base_url", "") or getattr(provider, "name", "llm")
    return urlsplit(base).netloc or str(base)


# Reachability is a network call and the dashboard polls every 8 seconds, so the answer is
# cached. 30s is short enough to notice a laptop waking up and long enough that watching
# the page does not become traffic of its own.
_PROBE_TTL_SECONDS = 30.0
_probe_cache: dict[str, tuple[float, dict]] = {}


async def probe_llm_hosts(providers: list) -> list[dict]:
    tiers = []
    for index, provider in enumerate(providers):
        host = llm_host_id(provider)
        tier = {
            "host": host,
            "model": getattr(provider, "model", None),
            "primary": index == 0,
            "role": "primary" if index == 0 else "escalation",
            **await _probe(provider, host),
        }
        tiers.append(tier)
    return tiers


async def _probe(provider, host: str) -> dict:
    cached = _probe_cache.get(host)
    now = monotonic()
    if cached and now - cached[0] < _PROBE_TTL_SECONDS:
        return cached[1]
    base = getattr(provider, "base_url", None)
    result: dict[str, object] = {"reachable": None, "loaded": []}
    if base:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4, connect=2)) as client:
                response = await client.get(f"{base}/api/ps")
                response.raise_for_status()
                result = {
                    "reachable": True,
                    "loaded": [
                        item.get("name") for item in response.json().get("models", []) or []
                    ],
                }
        except Exception as exc:
            result = {"reachable": False, "loaded": [], "error": type(exc).__name__}
    _probe_cache[host] = (now, result)
    return result


class PrimaryLLMRequest(BaseModel):
    host: str


class EditRequest(BaseModel):
    """A hand correction. Field names are validated against `processing.edits.EDITABLE`."""

    fields: dict[str, object]


class LeadEditRequest(BaseModel):
    """`exclude_unset` is what makes this a patch: an omitted field is left alone."""

    name: str | None = None
    edition_hint: str | None = None
    state: str | None = None
    resolved_url: str | None = None


class SettingsRequest(BaseModel):
    """A change to how the scraper notifies. Keys are checked against `dashboard.settings`."""

    values: dict[str, object]


class BackfillRequest(BaseModel):
    """A backdate asked for from the dashboard."""

    since: date
    # Collectors to run. Empty means search alone, matching `akaton backfill` with no
    # --sources: the structured adapters only publish what is open now, so replaying
    # them against a past date finds nothing.
    sources: list[str] = Field(default_factory=list)
    queries: int = Field(default=16, ge=1, le=100)


def create_dashboard(
    database: Database,
    controller: MonitorController,
    config: ConfigBundle,
    *,
    bot: BotController | None = None,
    notifier=None,
    reprocess=None,
    llm_providers: list | None = None,
) -> FastAPI:
    app = FastAPI(title="Akaton Monitor", docs_url=None, redoc_url=None)
    bot = bot or BotController()
    llm_providers = llm_providers if llm_providers is not None else []

    async def authorize(x_akaton_token: Annotated[str | None, Header()] = None) -> None:
        expected = config.runtime.dashboard_token
        if expected and not (x_akaton_token and secrets.compare_digest(expected, x_akaton_token)):
            raise HTTPException(status_code=401, detail="Dashboard token required")

    secured = [Depends(authorize)]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return DASHBOARD_HTML

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status", dependencies=secured)
    async def status() -> dict:
        async with database.session() as session:
            candidate_count = int(await session.scalar(select(func.count(CandidateRow.id))) or 0)
            event_count = int(await session.scalar(select(func.count(EventRow.id))) or 0)
            notification_count = int(
                await session.scalar(select(func.count(NotificationRow.id))) or 0
            )
            states = dict(
                (
                    await session.execute(
                        select(CandidateRow.state, func.count(CandidateRow.id)).group_by(
                            CandidateRow.state
                        )
                    )
                ).all()
            )
            last_search = await session.scalar(
                select(SearchRunRow).order_by(SearchRunRow.started_at.desc()).limit(1)
            )
            lead_count = int(await session.scalar(select(func.count(LeadRow.id))) or 0)
        return {
            "counts": {
                "candidates": candidate_count,
                "events": event_count,
                "notifications": notification_count,
            },
            # A sibling of `counts`, not a member of it: test_dashboard asserts exact dict
            # equality on that mapping, and a new key inside it would be a silent break.
            "leads": lead_count,
            "candidate_states": states,
            "last_search": _search_run(last_search),
            "monitor": controller.status(),
            "bot": bot.status(),
            "configuration": {
                "search_provider": config.runtime.search_provider,
                "llm_provider": config.runtime.llm_provider,
                "notifications_enabled": config.app.notifications_enabled,
                "timezone": config.app.timezone,
            },
        }

    @app.get("/api/settings", dependencies=secured)
    async def read_settings() -> dict[str, object]:
        """What governs whether an alert is sent, and how each control is explained."""
        return {
            "values": current_settings(config),
            "controls": describe_settings(),
            # Context the settings alone do not give: a threshold means little without
            # knowing the channel it posts to is actually connected.
            "channel": {
                "configured": bool(config.runtime.discord_channel_id),
                "bot": bot.status()["state"],
            },
        }

    @app.patch("/api/settings", dependencies=secured)
    async def write_settings(request: SettingsRequest) -> dict[str, object]:
        """Change how the scraper notifies, for this process and for the next restart."""
        try:
            outcome = update_settings(request.values, config)
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            # The live change already took effect; say so rather than implying nothing did.
            raise HTTPException(
                status_code=500,
                detail=f"Applied, but could not write to disk: {exc}",
            ) from exc
        return {**outcome, "message": _describe_change(outcome["changed"])}

    @app.get("/api/detections", dependencies=secured)
    async def detections(
        limit: Annotated[int, Query(ge=1, le=60)] = 24,
        tier: Annotated[str | None, Query()] = None,
    ) -> list[dict]:
        """The competitions found, as the alert each one produced.

        The events list answers "what is in the database"; this answers "what did the bot
        find", which is the question someone opening the dashboard actually has. It
        carries the poster, the organizer and the deadline so a detection can be judged
        without opening the source page.
        """
        query = (
            select(EventRow)
            .where(EventRow.archived_at.is_(None))
            .order_by(EventRow.relevance_score.desc(), EventRow.updated_at.desc())
        )
        async with database.session() as session:
            rows = list((await session.scalars(query.limit(limit))).all())
            announced = await _announced_event_ids(session, [row.id for row in rows])
        found = [_detection(row, config, announced) for row in rows]
        if tier:
            found = [item for item in found if item["tier"] == tier.upper()]
        return found

    @app.get("/api/events", dependencies=secured)
    async def events(
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        archived: Annotated[bool, Query()] = False,
    ) -> list[dict]:
        query = select(EventRow).order_by(EventRow.updated_at.desc())
        # Archived events are hidden by default; `?archived=true` is how you find one to
        # restore, so archiving is never a one-way door.
        query = query.where(
            EventRow.archived_at.is_not(None) if archived else EventRow.archived_at.is_(None)
        )
        async with database.session() as session:
            rows = list((await session.scalars(query.limit(limit))).all())
        return [_event(row) for row in rows]

    @app.get("/api/candidates", dependencies=secured)
    async def candidates(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        state: Annotated[str | None, Query()] = None,
        reason: Annotated[str | None, Query()] = None,
    ) -> list[dict]:
        query = select(CandidateRow).order_by(CandidateRow.updated_at.desc())
        if state:
            query = query.where(CandidateRow.state == state.upper())
        if reason:
            # rejection_reasons is a JSON array; match the quoted code inside its text.
            query = query.where(
                CandidateRow.rejection_reasons.cast(String).like(f'%"{reason.upper()}"%')
            )
        async with database.session() as session:
            rows = list((await session.scalars(query.limit(limit))).all())
        return [_candidate(row) for row in rows]

    @app.get("/api/rejections", dependencies=secured)
    async def rejections() -> dict:
        """Why candidates were dropped, so a silent pipeline can be explained."""
        async with database.session() as session:
            rows = list((await session.scalars(select(CandidateRow.rejection_reasons))).all())
        counts: dict[str, int] = {}
        for reasons in rows:
            for code in reasons or []:
                counts[code] = counts.get(code, 0) + 1
        return {
            "counts": dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)),
            "total": sum(counts.values()),
        }

    @app.get("/api/searches", dependencies=secured)
    async def searches(limit: Annotated[int, Query(ge=1, le=100)] = 25) -> list[dict]:
        """Recent search runs. A throttled backend shows up here as FAILED, not as silence."""
        async with database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SearchRunRow).order_by(SearchRunRow.started_at.desc()).limit(limit)
                    )
                ).all()
            )
        return [_search_run(row) for row in rows]

    @app.post("/api/actions/discover", status_code=202, dependencies=secured)
    async def discover() -> dict[str, object]:
        accepted = controller.trigger("discovery")
        return {"accepted": accepted, "message": _action_message(accepted, "discovery")}

    @app.post("/api/actions/refresh", status_code=202, dependencies=secured)
    async def refresh() -> dict[str, object]:
        accepted = controller.trigger("refresh")
        return {"accepted": accepted, "message": _action_message(accepted, "refresh")}

    @app.get("/api/leads", dependencies=secured)
    async def leads(limit: Annotated[int, Query(ge=1, le=200)] = 40) -> list[dict]:
        """Competitions named on Facebook or Reddit without a link, and what came of them."""
        async with database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LeadRow).order_by(LeadRow.last_seen_at.desc()).limit(limit)
                    )
                ).all()
            )
        return [_lead(row) for row in rows]

    @app.post("/api/actions/backfill", status_code=202, dependencies=secured)
    async def backfill(request: BackfillRequest) -> dict[str, object]:
        """Re-run discovery over a past date range.

        Unlike the scheduled pass this names its collectors, which also waives their
        cadence: someone asking to read the Facebook group back to June means now, not
        at the next six-hour boundary.
        """
        if request.since > date.today():
            raise HTTPException(status_code=422, detail="Backdate must not be in the future")
        sources = request.sources or None
        if sources:
            unknown = sorted(set(sources) - set(controller.sources))
            if unknown:
                raise HTTPException(
                    status_code=422, detail=f"Unknown source(s): {', '.join(unknown)}"
                )
        accepted = controller.trigger(
            "backfill",
            partial(
                controller.discovery,
                since=request.since,
                historical_test=True,
                query_limit=request.queries,
                sources=sources,
            ),
        )
        window = f"since {request.since.isoformat()}"
        scope = ", ".join(sources) if sources else "search"
        return {
            "accepted": accepted,
            "message": (
                f"Backdate started ({scope}, {window})"
                if accepted
                else "A backdate is already running"
            ),
        }

    async def _event_or_404(session, event_id: int) -> EventRow:
        row = await session.get(EventRow, event_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No event {event_id}")
        return row

    @app.patch("/api/events/{event_id}", dependencies=secured)
    async def edit_event(event_id: int, request: EditRequest) -> dict[str, object]:
        """Correct an event by hand. Corrected fields are pinned against the next refresh."""
        try:
            edits = parse_edits(request.fields)
        except EditError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        async with database.session() as session:
            row = await _event_or_404(session, event_id)
            changed = await Repository(session).apply_manual_edit(row, edits)
            payload = _event(row)
        return {
            "changed": changed,
            "event": payload,
            "message": (f"Updated {', '.join(changed)}" if changed else "Nothing to change"),
        }

    @app.delete("/api/events/{event_id}", dependencies=secured)
    async def archive_event(event_id: int) -> dict[str, object]:
        """Archive rather than delete: notifications already sent still reference this row."""
        async with database.session() as session:
            row = await _event_or_404(session, event_id)
            await Repository(session).set_archived(row, True)
            title = row.title
        return {"archived": True, "message": f"Archived “{title[:60]}”"}

    @app.post("/api/events/{event_id}/restore", dependencies=secured)
    async def restore_event(event_id: int) -> dict[str, object]:
        async with database.session() as session:
            row = await _event_or_404(session, event_id)
            await Repository(session).set_archived(row, False)
            title = row.title
        return {"archived": False, "message": f"Restored “{title[:60]}”"}

    @app.delete("/api/events/{event_id}/overrides/{field}", dependencies=secured)
    async def release_override(event_id: int, field: str) -> dict[str, object]:
        """Hand one field back to automatic extraction."""
        async with database.session() as session:
            row = await _event_or_404(session, event_id)
            released = await Repository(session).release_override(row, field)
        if not released:
            raise HTTPException(status_code=404, detail=f"{field} is not pinned")
        return {
            "released": field,
            "message": f"{field} follows the source page again from the next refresh",
        }

    @app.patch("/api/leads/{lead_id}", dependencies=secured)
    async def edit_lead(lead_id: int, request: LeadEditRequest) -> dict[str, object]:
        """Fix a badly extracted competition name, which is the common case."""
        async with database.session() as session:
            row = await session.get(LeadRow, lead_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No lead {lead_id}")
            changed = []
            for name, value in request.model_dump(exclude_unset=True).items():
                if getattr(row, name) != value:
                    setattr(row, name, value)
                    changed.append(name)
            if "name" in changed:
                # The key is derived from the name, so correcting the name has to re-key
                # the lead or the cooldown would still be keyed to the wrong spelling.
                row.normalized_name = " ".join(
                    canonical_token(token) for token in (row.name or "").split()
                )
                row.lead_key = lead_key(row.normalized_name, row.edition_hint)
            payload = _lead(row)
        return {"changed": changed, "lead": payload, "message": f"Updated lead {lead_id}"}

    @app.post("/api/leads/{lead_id}/search-now", dependencies=secured)
    async def search_lead_now(lead_id: int) -> dict[str, object]:
        """Clear the cooldown so the next discovery run spends a search on this lead."""
        async with database.session() as session:
            row = await session.get(LeadRow, lead_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No lead {lead_id}")
            row.last_searched_at = None
            row.search_runs = 0
            row.state = LeadState.NEW
            name = row.name
        return {"message": f"“{name[:50]}” will be searched on the next run"}

    @app.delete("/api/leads/{lead_id}", dependencies=secured)
    async def delete_lead(lead_id: int) -> dict[str, object]:
        """A lead is a work item, not a record of delivery, so this really deletes."""
        async with database.session() as session:
            row = await session.get(LeadRow, lead_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No lead {lead_id}")
            name = row.name
            await session.delete(row)
        return {"deleted": lead_id, "message": f"Deleted “{name[:50]}”"}

    @app.delete("/api/candidates/{candidate_id}", dependencies=secured)
    async def delete_candidate(candidate_id: int) -> dict[str, object]:
        async with database.session() as session:
            row = await session.get(CandidateRow, candidate_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No candidate {candidate_id}")
            # Snapshots are owned by the candidate and have no meaning without it.
            for snapshot in await session.scalars(
                select(SourceSnapshotRow).where(SourceSnapshotRow.candidate_id == candidate_id)
            ):
                await session.delete(snapshot)
            await session.delete(row)
        return {"deleted": candidate_id, "message": f"Deleted candidate {candidate_id}"}

    @app.post("/api/candidates/{candidate_id}/retry", status_code=202, dependencies=secured)
    async def retry_candidate(candidate_id: int) -> dict[str, object]:
        """Put a rejected page back through the pipeline, usually after a rule changed."""
        if reprocess is None:
            raise HTTPException(status_code=409, detail="No pipeline is attached")
        async with database.session() as session:
            row = await session.get(CandidateRow, candidate_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No candidate {candidate_id}")
            url, title, snippet = row.discovered_url, row.title, row.snippet
            row.retry_at = None
            row.rejection_reasons = []
        outcome = await reprocess(url, title, snippet)
        return {
            "state": outcome.state,
            "reason": outcome.reason,
            "message": f"Re-ran candidate {candidate_id}: {outcome.state}",
        }

    @app.get("/api/llm", dependencies=secured)
    async def llm_hosts() -> dict[str, object]:
        """The model ladder and whether each host is answering."""
        return {
            "tiers": await probe_llm_hosts(llm_providers),
            "escalation_confidence": config.app.llm_escalation_confidence,
            "escalations_per_run": config.app.llm_escalations_per_run,
        }

    @app.post("/api/actions/llm/primary", dependencies=secured)
    async def set_primary_llm(request: PrimaryLLMRequest) -> dict[str, object]:
        """Reorder the ladder at runtime, so a host can be swapped without a restart."""
        names = [llm_host_id(item) for item in llm_providers]
        if request.host not in names:
            raise HTTPException(
                status_code=404, detail=f"Unknown host; configured: {', '.join(names) or 'none'}"
            )
        index = names.index(request.host)
        # A list mutated in place, because the pipeline holds this exact object.
        llm_providers.insert(0, llm_providers.pop(index))
        return {
            "primary": request.host,
            "message": f"{request.host} is now asked first",
        }

    @app.post("/api/actions/jobs/{name}/cancel", dependencies=secured)
    async def cancel_job(name: str) -> dict[str, object]:
        """Stop a running discovery, refresh or backdate."""
        if name not in CANCELLABLE_JOBS:
            raise HTTPException(status_code=404, detail=f"No job named {name}")
        cancelled = await controller.cancel(name)
        return {
            "cancelled": cancelled,
            "message": f"{name} cancelled" if cancelled else f"{name} was not running",
        }

    @app.post("/api/actions/bot/start", dependencies=secured)
    async def bot_start() -> dict[str, object]:
        if not bot.configured:
            raise HTTPException(status_code=409, detail="Discord is not configured")
        changed = await bot.start()
        return {
            "changed": changed,
            "state": bot.status()["state"],
            "message": "Bot starting" if changed else "Bot is already running",
        }

    @app.post("/api/actions/bot/stop", dependencies=secured)
    async def bot_stop() -> dict[str, object]:
        changed = await bot.stop()
        return {
            "changed": changed,
            "state": bot.status()["state"],
            "message": "Bot stopped" if changed else "Bot was not running",
        }

    @app.post("/api/actions/events/{event_id}/notify", status_code=202, dependencies=secured)
    async def notify_event(event_id: int) -> dict[str, object]:
        """Send an alert for one event on demand.

        Deliberately bypasses the relevance threshold, shadow mode and the
        already-announced check: those govern automatic delivery, and this is someone
        looking at a specific event and asking for it.
        """
        if notifier is None or not bot.running:
            raise HTTPException(
                status_code=409,
                detail="Discord is not connected. Start the bot first.",
            )
        async with database.session() as session:
            row = await session.get(EventRow, event_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Event not found")
            payload = build_manual_payload(row, config)
            title = row.title
        try:
            receipt = await notifier.send(payload)
        except Exception as exc:
            async with database.session() as session:
                session.add(
                    record_manual_notification(
                        payload, message_id=None, error=f"{type(exc).__name__}: {exc}"
                    )
                )
            raise HTTPException(status_code=502, detail=f"Discord refused: {exc}") from exc
        async with database.session() as session:
            session.add(record_manual_notification(payload, message_id=receipt.message_id))
        return {"sent": True, "message": f"Sent “{title[:60]}” to Discord"}

    @app.post("/api/actions/scheduler/start", dependencies=secured)
    async def scheduler_start() -> dict[str, object]:
        changed = controller.start_scheduler()
        return {"changed": changed, "state": controller.status()["scheduler"]}

    @app.post("/api/actions/scheduler/pause", dependencies=secured)
    async def scheduler_pause() -> dict[str, object]:
        changed = controller.pause_scheduler()
        return {"changed": changed, "state": controller.status()["scheduler"]}

    return app


def _lead(row: LeadRow) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "edition_hint": row.edition_hint,
        "platform": row.platform,
        "mention_kind": row.mention_kind,
        "source_url": row.source_url,
        "excerpt": row.mention_excerpt,
        "sightings": row.sightings,
        "state": row.state,
        "search_runs": row.search_runs,
        "resolved_url": row.resolved_url,
        "event_id": row.event_id,
        "last_error": row.last_error,
        "last_searched_at": row.last_searched_at.isoformat() if row.last_searched_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


async def _announced_event_ids(session, event_ids: list[int]) -> set[int]:
    """Which of these events have actually been posted to Discord.

    A detection that alerted and one that is merely stored look identical otherwise, and
    the difference is the single most useful thing to know while notifications are off.
    """
    if not event_ids:
        return set()
    rows = await session.scalars(
        select(NotificationRow.event_id).where(
            NotificationRow.event_id.in_(event_ids),
            NotificationRow.state == "SENT",
        )
    )
    return set(rows.all())


def _detection(row: EventRow, config: ConfigBundle, announced: set[int]) -> dict:
    """One found competition, with everything needed to judge it without leaving the page."""
    facts = EventFacts.model_validate(row.current_facts) if row.current_facts else EventFacts()
    thresholds = config.scoring.get("thresholds", {})
    score = row.relevance_score or 0
    tier = (
        "HIGH_PRIORITY"
        if score >= int(thresholds.get("high", 80))
        else "RECOMMENDED"
        if score >= int(thresholds.get("recommended", 65))
        else "POSSIBLE"
        if score >= int(thresholds.get("possible", 50))
        else "WEAK"
    )
    location = " — ".join(filter(None, (facts.location.city, facts.location.region)))
    if facts.location.location_type.value == "ONLINE":
        location = "Online"
    return {
        "id": row.id,
        "title": row.title,
        "organizer": row.organizer,
        "category": (row.category or "").replace("_", " ").title(),
        "summary": summarise(facts.description, limit=240),
        # Judged by the same host trust the alert applies, so the dashboard cannot show a
        # banner the webhook would have refused.
        "image_url": displayable_image(facts.image_url, config.sources),
        "icon_url": organizer_icon(facts.canonical_url, config.sources),
        "canonical_url": row.canonical_url,
        "registration_url": row.registration_url,
        "location": location or facts.location.location_type.value.title(),
        "deadline": facts.registration_deadline.value.isoformat()
        if facts.registration_deadline.value
        else None,
        "event_start": facts.event_start.value.isoformat() if facts.event_start.value else None,
        "prize": facts.prize_information,
        "score": score,
        "tier": tier,
        "registration": row.registration_state,
        "phase": row.event_phase,
        "announced": row.id in announced,
        "updated_at": row.updated_at.isoformat(),
    }


def _event(row: EventRow) -> dict:
    facts = row.current_facts or {}
    return {
        "id": row.id,
        "title": row.title,
        "category": row.category,
        "organizer": row.organizer,
        "phase": row.event_phase,
        "registration": row.registration_state,
        "score": row.relevance_score,
        "confidence": row.confidence_score,
        "canonical_url": row.canonical_url,
        "registration_url": row.registration_url,
        "location": facts.get("location", {}),
        "deadline": facts.get("registration_deadline", {}).get("value"),
        "event_start": facts.get("event_start", {}).get("value"),
        "updated_at": row.updated_at.isoformat(),
        # Which fields a person corrected, so the edit form can show a pin and offer to
        # release it, and the values the form should start from.
        "pinned": sorted(row.manual_overrides or {}),
        "editable": current_values(EventFacts.model_validate(row.current_facts))
        if row.current_facts
        else {},
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
    }


def _candidate(row: CandidateRow) -> dict:
    trace = row.trace or []
    return {
        "id": row.id,
        "title": row.title,
        "url": row.discovered_url,
        "provider": row.provider,
        "channel": row.discovery_channel,
        "state": row.state,
        "rejection_reasons": row.rejection_reasons or [],
        "event_id": row.event_id,
        "retry_at": row.retry_at.isoformat() if row.retry_at else None,
        "last_trace": trace[-1] if trace else None,
        "updated_at": row.updated_at.isoformat(),
    }


def _search_run(row: SearchRunRow | None) -> dict | None:
    if row is None:
        return None
    return {
        "provider": row.provider,
        "query_group": row.query_group,
        "query": row.query,
        "status": row.status,
        "result_count": row.result_count,
        "error": row.error,
        "started_at": row.started_at.isoformat(),
    }


def _action_message(accepted: bool, name: str) -> str:
    return f"{name.title()} started" if accepted else f"{name.title()} is already running"


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Akaton Signal Room</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
/* Akaton runs on a private tailnet and is expected to work when the box has no route to
   the internet, so everything below is written out rather than pulled from a CSS CDN.
   The webfonts are the one exception and they degrade to the stacks named on each rule. */

:root {
  --void: #070b0f;        /* the page behind everything */
  --deck: #0d141b;        /* panels sitting on it */
  --deck-2: #121c25;      /* raised rows, inputs */
  --edge: #1e2c39;
  --edge-lit: #2b4050;
  --signal: #7df3c0;      /* a detection, a healthy host, anything live */
  --standby: #f5a65b;     /* waiting, paused, degraded */
  --fault: #ff6b6b;
  --link: #79c0ff;
  --text: #e8f1f5;
  --muted: #8fa3b8;
  --faint: #5d7186;
  --display: 'Space Grotesk', 'Segoe UI', system-ui, sans-serif;
  --body: 'Inter', 'Segoe UI', system-ui, sans-serif;
  /* Every number on this page is a reading. Mono keeps columns of them aligned and
     stops a score from being mistaken for prose. */
  --mono: 'JetBrains Mono', ui-monospace, 'Cascadia Mono', Consolas, monospace;
  --rail: 232px;
}

* { box-sizing: border-box; }
[hidden] { display: none !important; }

html { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }
}

body {
  margin: 0; min-height: 100vh; background: var(--void); color: var(--text);
  font-family: var(--body); font-size: 14px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* The one ambient flourish: a faint sweep behind the header, like a receiver's own glow.
   It is behind everything and does not move, so it costs nothing to read past. */
body::before {
  content: ''; position: fixed; inset: 0 0 auto 0; height: 420px; pointer-events: none; z-index: 0;
  background:
    radial-gradient(900px 340px at 22% -12%, rgba(125,243,192,0.10), transparent 70%),
    radial-gradient(700px 300px at 82% -20%, rgba(121,192,255,0.07), transparent 70%);
}

a { color: var(--link); }
h1, h2, h3 { font-family: var(--display); margin: 0; letter-spacing: -0.01em; }
:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; border-radius: 4px; }

/* ---------- shell ---------- */
.shell { display: flex; min-height: 100vh; position: relative; z-index: 1; }

.rail {
  width: var(--rail); flex: none; border-right: 1px solid var(--edge);
  background: rgba(9,14,19,0.72); backdrop-filter: blur(8px);
  padding: 22px 18px; position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand-mark {
  width: 30px; height: 30px; flex: none; border-radius: 8px; display: grid; place-items: center;
  background: linear-gradient(150deg, rgba(125,243,192,0.22), rgba(121,192,255,0.10));
  border: 1px solid var(--edge-lit);
}
.brand-mark span { width: 8px; height: 8px; border-radius: 50%; background: var(--signal); box-shadow: 0 0 10px var(--signal); }
.brand-name { font-family: var(--display); font-weight: 700; font-size: 15px; letter-spacing: 0.01em; }
.brand-sub { font-size: 10px; color: var(--faint); text-transform: uppercase; letter-spacing: 0.16em; margin-top: 1px; }

.rail-nav { margin-top: 26px; display: flex; flex-direction: column; gap: 1px; }
.rail-nav a {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 8px 10px; border-radius: 7px; color: var(--muted); text-decoration: none;
  font-size: 13px; font-weight: 500; transition: background 0.15s, color 0.15s;
}
.rail-nav a:hover { background: var(--deck-2); color: var(--text); }
.rail-nav .tally { font-family: var(--mono); font-size: 11px; color: var(--faint); }

.rail-block { margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--edge); }
.rail-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.16em; color: var(--faint); margin-bottom: 10px; }
.rail-line { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 0; font-size: 12px; }
.rail-line .k { color: var(--muted); }
.rail-line .v { font-family: var(--mono); font-size: 11px; }

.main { flex: 1; min-width: 0; padding: 26px 30px 90px; max-width: 1420px; }

/* ---------- header ---------- */
.masthead { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 18px; margin-bottom: 6px; }
.masthead h1 { font-size: 30px; font-weight: 700; }
.masthead .lede { color: var(--muted); font-size: 13px; margin-top: 5px; max-width: 62ch; }
.token-field {
  width: 210px; background: var(--deck-2); border: 1px solid var(--edge); color: var(--text);
  border-radius: 8px; padding: 8px 11px; font-size: 12.5px; font-family: var(--body);
}
.token-field::placeholder { color: var(--faint); }
.token-field:focus { border-color: var(--edge-lit); outline: none; }

/* ---------- controls ---------- */
.btn {
  border: 1px solid var(--edge-lit); background: var(--deck-2); color: var(--text);
  border-radius: 8px; padding: 7px 13px; font-size: 12.5px; font-weight: 600;
  font-family: var(--body); cursor: pointer; transition: border-color 0.15s, background 0.15s;
}
.btn:hover:not(:disabled) { border-color: var(--signal); background: #16232e; }
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.btn.primary { border-color: rgba(125,243,192,0.5); background: rgba(125,243,192,0.10); color: var(--signal); }
.btn.primary:hover:not(:disabled) { background: rgba(125,243,192,0.18); }
.btn.danger { color: var(--fault); border-color: rgba(255,107,107,0.35); }
.btn.danger:hover:not(:disabled) { border-color: var(--fault); background: rgba(255,107,107,0.10); }
.btn.small { padding: 5px 10px; font-size: 11.5px; }
.btn-row { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; }

/* ---------- panels ---------- */
.panel { background: var(--deck); border: 1px solid var(--edge); border-radius: 13px; padding: 20px; }
.panel + .panel { margin-top: 16px; }
.panel-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
.panel-head h2 { font-size: 16px; font-weight: 700; }
.panel-note { color: var(--muted); font-size: 12.5px; margin: 4px 0 16px; max-width: 76ch; }
.section { scroll-margin-top: 18px; }
.section-rule {
  display: flex; align-items: center; gap: 12px; margin: 34px 0 14px;
  font-family: var(--display); font-size: 11px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.2em; color: var(--faint);
}
.section-rule::after { content: ''; flex: 1; height: 1px; background: var(--edge); }

/* ---------- readings ---------- */
.readings { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-top: 18px; }
.reading { background: var(--deck); border: 1px solid var(--edge); border-radius: 11px; padding: 14px 16px; }
.reading .k { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.13em; color: var(--faint); }
.reading .v { font-family: var(--mono); font-size: 27px; font-weight: 600; margin-top: 7px; line-height: 1; }
.reading .sub { font-size: 11px; color: var(--muted); margin-top: 6px; font-family: var(--mono); }
.v.is-signal { color: var(--signal); }
.v.is-standby { color: var(--standby); }
.v.is-fault { color: var(--fault); }
.v.is-muted { color: var(--faint); }
/* A live counter is a small readout, not a headline number. */
.reading .v.word { font-size: 17px; font-family: var(--display); letter-spacing: 0; }

/* ---------- detection cards: the signature ---------- */
.detections { display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 14px; margin-top: 16px; }
.card {
  position: relative; display: flex; flex-direction: column; overflow: hidden;
  background: var(--deck); border: 1px solid var(--edge); border-radius: 13px;
  transition: border-color 0.18s, transform 0.18s;
}
.card:hover { border-color: var(--edge-lit); transform: translateY(-2px); }
/* The tier stripe: the single strongest cue on the card, read before any text. */
.card::before { content: ''; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--edge-lit); }
.card.tier-HIGH_PRIORITY::before { background: var(--fault); box-shadow: 0 0 14px rgba(255,107,107,0.5); }
.card.tier-RECOMMENDED::before { background: var(--signal); box-shadow: 0 0 14px rgba(125,243,192,0.4); }
.card.tier-POSSIBLE::before { background: var(--standby); }

/* Only a real poster earns a cover band. A placeholder rectangle with the title's
   initials in it is decoration standing in for content, and six of them stacked down a
   column pushed every actual fact below the fold. Cards without one simply start at the
   text, which is what the reader came for. */
.card-cover { height: 124px; background: var(--deck-2); position: relative; overflow: hidden; flex: none; }
.card-cover img { width: 100%; height: 100%; object-fit: cover; display: block; }
.card-cover::after { content: ''; position: absolute; inset: auto 0 0 0; height: 46px;
  background: linear-gradient(transparent, var(--deck)); }

.card-body { padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 9px; flex: 1; }
.card-org { display: flex; align-items: center; gap: 7px; font-size: 11px; color: var(--muted); min-height: 16px; }
.card-org img { width: 15px; height: 15px; border-radius: 3px; flex: none; }
.card-title { font-family: var(--display); font-weight: 700; font-size: 15px; line-height: 1.3; }
.card-title a { color: var(--text); text-decoration: none; }
.card-title a:hover { color: var(--signal); }
.card-summary { font-size: 12.5px; color: var(--muted); line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.card-facts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: auto; padding-top: 4px; }
.card-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px;
  border-top: 1px solid var(--edge); padding-top: 11px; margin-top: 3px; }
.card-score { font-family: var(--mono); font-size: 12px; color: var(--faint); }
.card-score b { color: var(--text); font-size: 14px; font-weight: 600; }
/* The deadline is what the reader is deciding on, so it gets the loudest treatment on
   the card and turns amber, then red, as it closes in. */
.countdown { font-family: var(--mono); font-size: 11.5px; color: var(--muted); }
.countdown.soon { color: var(--standby); }
.countdown.urgent { color: var(--fault); }

/* ---------- chips ---------- */
.chip {
  display: inline-flex; align-items: center; gap: 5px; border-radius: 999px;
  padding: 3px 9px; font-size: 11px; font-weight: 500; white-space: nowrap;
  border: 1px solid var(--edge); color: var(--muted); background: var(--deck-2);
}
.chip.signal { color: var(--signal); border-color: rgba(125,243,192,0.35); background: rgba(125,243,192,0.09); }
.chip.standby { color: var(--standby); border-color: rgba(245,166,91,0.35); background: rgba(245,166,91,0.09); }
.chip.fault { color: var(--fault); border-color: rgba(255,107,107,0.35); background: rgba(255,107,107,0.09); }
.chip.bare { border-color: transparent; background: transparent; padding-left: 0; }
.chip.mono { font-family: var(--mono); font-size: 10.5px; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: none; }
.chip-row { display: flex; flex-wrap: wrap; gap: 7px; }

/* Filter pills for the rejection reasons and the detection tiers. */
.pill {
  border: 1px solid var(--edge); background: var(--deck-2); color: var(--muted);
  border-radius: 999px; padding: 5px 12px; font-size: 11.5px; font-weight: 600;
  cursor: pointer; font-family: var(--body); transition: border-color 0.15s, color 0.15s;
}
.pill:hover { border-color: var(--edge-lit); color: var(--text); }
.pill[aria-pressed="true"] { border-color: var(--signal); color: var(--signal); background: rgba(125,243,192,0.10); }

/* ---------- settings ---------- */
.settings-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
  gap: 14px; margin-top: 16px; align-items: start; }
.setting { background: var(--deck-2); border: 1px solid var(--edge); border-radius: 11px; padding: 15px 16px; }
.setting-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.setting-label { font-size: 13.5px; font-weight: 600; font-family: var(--display); }
.setting-help { font-size: 11.5px; color: var(--muted); margin-top: 7px; line-height: 1.45; }
.setting-value { font-family: var(--mono); font-size: 15px; font-weight: 600; color: var(--signal); min-width: 34px; text-align: right; }

/* A switch, built from a checkbox so it stays keyboard-reachable and announces itself. */
.switch { position: relative; width: 40px; height: 22px; flex: none; }
.switch input { position: absolute; inset: 0; opacity: 0; margin: 0; cursor: pointer; width: 100%; height: 100%; z-index: 1; }
.switch .track { position: absolute; inset: 0; border-radius: 999px; background: var(--edge); border: 1px solid var(--edge-lit); transition: background 0.18s, border-color 0.18s; }
.switch .knob { position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; border-radius: 50%; background: var(--faint); transition: transform 0.18s, background 0.18s; }
.switch input:checked ~ .track { background: rgba(125,243,192,0.24); border-color: var(--signal); }
.switch input:checked ~ .knob { transform: translateX(18px); background: var(--signal); }
.switch input:focus-visible ~ .track { outline: 2px solid var(--signal); outline-offset: 2px; }

.slider { width: 100%; margin-top: 13px; accent-color: #7df3c0; background: transparent; }
.scale { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 10px; color: var(--faint); margin-top: 3px; }
.settings-foot { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 16px; }
.settings-state { font-size: 12px; color: var(--muted); }

/* ---------- tables ---------- */
.scroller { overflow-x: auto; margin-top: 14px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th {
  text-align: left; padding: 0 12px 9px 0; font-size: 10px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.13em; color: var(--faint);
  border-bottom: 1px solid var(--edge); white-space: nowrap;
}
tbody td { padding: 10px 12px 10px 0; border-bottom: 1px solid rgba(30,44,57,0.55); vertical-align: top; }
tbody tr:hover { background: rgba(18,28,37,0.5); }
/* Right-aligned, but still padded: with padding-right:0 the "Seen" count ran straight
   into the next column and read as "5—" rather than as two separate cells. */
td.num { font-family: var(--mono); text-align: right; padding-right: 22px; }
th.num { text-align: right; padding-right: 22px; }
/* The last column owns the row's right edge, so it keeps the flush alignment. */
td.actions, tbody td:last-child.num { padding-right: 0; }
td.actions { text-align: right; white-space: nowrap; padding-right: 0; }
td.actions .btn { margin-left: 5px; }
.cell-link { color: var(--link); text-decoration: none; font-weight: 600; }
.cell-link:hover { text-decoration: underline; }
.cell-dim { color: var(--muted); }
.cell-fault { color: var(--fault); font-size: 12px; }
.empty { color: var(--faint); font-style: italic; }

/* ---------- search health ---------- */
.log { max-height: 300px; overflow-y: auto; padding-right: 4px; display: flex; flex-direction: column; gap: 7px; }
.log-row { border: 1px solid var(--edge); border-radius: 9px; padding: 9px 11px; }
.log-row.failed { border-color: rgba(255,107,107,0.35); background: rgba(255,107,107,0.05); }
.log-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.log-q { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); color: var(--muted); }
.log-err { font-size: 11px; color: rgba(255,107,107,0.85); margin-top: 5px; line-height: 1.4; }

/* ---------- llm tiers ---------- */
.tiers { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 14px; }
.tier { flex: 1; min-width: 230px; border: 1px solid var(--edge); border-radius: 11px; padding: 13px 15px; background: var(--deck-2); }
.tier.primary { border-color: rgba(125,243,192,0.4); background: rgba(125,243,192,0.05); }
.tier-name { font-family: var(--display); font-weight: 700; font-size: 13.5px; }
.tier-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-top: 8px; font-size: 11px; color: var(--faint); font-family: var(--mono); }

/* ---------- backdate ---------- */
.form-row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 14px; margin-top: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field > span { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.13em; color: var(--faint); }
.field input {
  background: var(--deck-2); border: 1px solid var(--edge); color: var(--text);
  border-radius: 8px; padding: 7px 10px; font-size: 13px; font-family: var(--mono);
  color-scheme: dark;
}
.field input:focus { border-color: var(--edge-lit); outline: none; }
.checks { display: flex; flex-wrap: wrap; gap: 7px; }
.check { display: flex; align-items: center; gap: 6px; border: 1px solid var(--edge); background: var(--deck-2);
  border-radius: 999px; padding: 5px 11px; font-size: 11.5px; cursor: pointer; color: var(--muted); }
.check:hover { border-color: var(--edge-lit); color: var(--text); }
.check input { accent-color: #7df3c0; margin: 0; cursor: pointer; }

/* ---------- toast + modal ---------- */
#toast {
  position: fixed; bottom: 22px; right: 22px; z-index: 60; max-width: 380px;
  background: #10202a; border: 1px solid var(--edge-lit); border-left: 3px solid var(--signal);
  border-radius: 9px; padding: 11px 15px; font-size: 13px; box-shadow: 0 14px 40px rgba(0,0,0,0.55);
}
#modal { position: fixed; inset: 0; z-index: 70; display: flex; align-items: center; justify-content: center;
  padding: 20px; background: rgba(3,6,9,0.78); backdrop-filter: blur(3px); }
.modal-card { width: 100%; max-width: 520px; max-height: 86vh; overflow-y: auto;
  background: var(--deck); border: 1px solid var(--edge-lit); border-radius: 13px; padding: 22px; box-shadow: 0 24px 70px rgba(0,0,0,0.6); }
.modal-card h2 { font-size: 16px; }
.modal-hint { font-size: 12px; color: var(--muted); margin: 6px 0 18px; line-height: 1.45; }
.modal-field { display: block; margin-bottom: 13px; }
.modal-field .lab { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--faint);
  text-transform: uppercase; letter-spacing: 0.11em; margin-bottom: 6px; }
.modal-field input { width: 100%; background: var(--deck-2); border: 1px solid var(--edge); color: var(--text);
  border-radius: 8px; padding: 9px 11px; font-size: 13px; font-family: var(--body); color-scheme: dark; }
.modal-field input:focus { border-color: var(--signal); outline: none; }
.modal-foot { display: flex; justify-content: flex-end; gap: 9px; margin-top: 20px; }
.pin { border: none; background: rgba(245,166,91,0.15); color: var(--standby); border-radius: 999px;
  padding: 2px 8px; font-size: 10px; cursor: pointer; font-family: var(--body); letter-spacing: 0; text-transform: none; }
.pin:hover { background: rgba(245,166,91,0.28); }

@media (max-width: 900px) {
  .shell { flex-direction: column; }
  .rail { width: auto; height: auto; position: static; border-right: none; border-bottom: 1px solid var(--edge); }
  .rail-nav { flex-direction: row; flex-wrap: wrap; margin-top: 16px; }
  .rail-block { display: none; }
  .main { padding: 20px 16px 60px; }
  .masthead h1 { font-size: 24px; }
}
</style>
</head>
<body>
<div class="shell">

  <aside class="rail">
    <div class="brand">
      <div class="brand-mark"><span></span></div>
      <div>
        <div class="brand-name">Akaton</div>
        <div class="brand-sub">Signal room</div>
      </div>
    </div>

    <nav class="rail-nav">
      <a href="#detections">Detections <span class="tally" id="nav-detections"></span></a>
      <a href="#settings">Notifications</a>
      <a href="#activity">Activity</a>
      <a href="#mentions">Mentions <span class="tally" id="nav-mentions"></span></a>
      <a href="#events">All events <span class="tally" id="nav-events"></span></a>
      <a href="#rejected">Rejected <span class="tally" id="nav-rejected"></span></a>
    </nav>

    <div class="rail-block">
      <div class="rail-label">Status</div>
      <div class="rail-line"><span class="k">Monitor</span><span class="v" id="rail-scheduler">—</span></div>
      <div class="rail-line"><span class="k">Discord</span><span class="v" id="rail-bot">—</span></div>
      <div class="rail-line"><span class="k">Alerts</span><span class="v" id="rail-alerts">—</span></div>
      <div class="rail-line"><span class="k">Next run</span><span class="v" id="rail-next">—</span></div>
    </div>

    <div class="rail-block">
      <div class="rail-label">Access</div>
      <input id="token" type="password" placeholder="Dashboard token" class="token-field" style="width:100%">
    </div>
  </aside>

  <main class="main">

    <header class="masthead">
      <div>
        <h1>What the bot found</h1>
        <p class="lede" id="subtitle">Loading monitor state…</p>
      </div>
      <div class="btn-row">
        <button class="btn primary" data-act="discover">Run discovery</button>
        <button class="btn danger" data-cancel="discovery" hidden>Stop discovery</button>
        <button class="btn" data-act="refresh">Refresh events</button>
        <button class="btn danger" data-cancel="refresh" hidden>Stop refresh</button>
        <button class="btn" data-sched="start">Start monitor</button>
        <button class="btn" data-sched="pause">Pause</button>
        <button class="btn" data-bot="start">Connect Discord</button>
        <button class="btn" data-bot="stop">Disconnect</button>
      </div>
    </header>

    <section class="readings">
      <div class="reading"><div class="k">Detections</div><div class="v is-signal" id="k-events">—</div><div class="sub" id="k-announced">—</div></div>
      <div class="reading"><div class="k">Pages read</div><div class="v" id="k-candidates">—</div><div class="sub" id="k-states">—</div></div>
      <div class="reading"><div class="k">Alerts sent</div><div class="v" id="k-notifications">—</div><div class="sub" id="k-alerts-mode">—</div></div>
      <div class="reading"><div class="k">Dropped</div><div class="v is-fault" id="k-rejected">—</div><div class="sub">not a match</div></div>
      <div class="reading"><div class="k">Monitor</div><div class="v word" id="k-scheduler">—</div><div class="sub" id="k-next">—</div></div>
      <div class="reading"><div class="k">Discord</div><div class="v word" id="k-bot">—</div><div class="sub" id="k-bot-detail">—</div></div>
    </section>

    <!-- ================= DETECTIONS ================= -->
    <div class="section-rule">Detections</div>
    <section class="panel section" id="detections">
      <div class="panel-head">
        <h2>Competitions found</h2>
        <div class="chip-row" id="tier-filters"></div>
      </div>
      <p class="panel-note">Every competition the pipeline accepted, strongest match first — the same facts, poster and deadline that go out in the Discord alert. A green edge means it cleared the alert score; amber means it was kept but not announced.</p>
      <div class="detections" id="detections-grid"></div>
    </section>

    <!-- ================= SETTINGS ================= -->
    <div class="section-rule">Notifications</div>
    <section class="panel section" id="settings">
      <div class="panel-head">
        <h2>How you get told</h2>
        <span class="chip mono" id="settings-channel">—</span>
      </div>
      <p class="panel-note">These govern what reaches Discord. Changes apply to the running scraper immediately and are written back to your config files, so they survive a restart.</p>
      <div class="settings-grid" id="settings-grid"></div>
      <div class="settings-foot">
        <button class="btn primary" id="settings-save" disabled>Save changes</button>
        <button class="btn" id="settings-reset" disabled>Discard</button>
        <span class="settings-state" id="settings-state">No unsaved changes</span>
      </div>
    </section>

    <!-- ================= ACTIVITY ================= -->
    <div class="section-rule">Activity</div>
    <section class="panel section" id="activity">
      <div class="panel-head"><h2>Search health</h2></div>
      <p class="panel-note">SearXNG scrapes upstream engines on your behalf. A throttled backend shows up here as FAILED rather than as a run that quietly found nothing.</p>
      <div class="log" id="searches"></div>
    </section>

    <section class="panel" id="llm-panel" hidden>
      <div class="panel-head"><h2>Extraction models</h2></div>
      <p class="panel-note" id="llm-hint"></p>
      <div class="tiers" id="llm-tiers"></div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>Read back over a past date</h2></div>
      <p class="panel-note">Re-reads the collectors you pick from a date you choose. Naming a collector waives its cadence, so it starts now. Past-event and deadline checks are relaxed, as with <code>akaton backfill</code>.</p>
      <div class="form-row">
        <label class="field"><span>Since</span><input id="bf-since" type="date"></label>
        <label class="field"><span>Queries</span><input id="bf-queries" type="number" min="1" max="100" value="16" style="width:82px"></label>
        <div class="field"><span>Collectors</span><div class="checks" id="bf-sources"></div></div>
        <button class="btn" id="bf-run">Start read-back</button>
        <button class="btn danger" id="bf-cancel" hidden>Cancel</button>
      </div>
      <div class="chip-row" id="bf-status" style="margin-top:14px"></div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>Pipeline states</h2></div>
      <p class="panel-note">Where everything the scraper has touched currently sits.</p>
      <div class="chip-row" id="states"></div>
      <div class="panel-head" style="margin-top:20px"><h2>Last run</h2></div>
      <div id="lastruns" style="margin-top:10px;font-size:12px;color:var(--muted);font-family:var(--mono)"></div>
    </section>

    <!-- ================= MENTIONS ================= -->
    <div class="section-rule">Mentions</div>
    <section class="panel section" id="mentions">
      <div class="panel-head">
        <h2>Named but not linked</h2>
        <span class="chip mono" id="leads-count"></span>
      </div>
      <p class="panel-note">Competitions someone named on Facebook or Reddit without linking to. Each costs one search to track down; a repeat mention raises the sighting count instead of spending another.</p>
      <div class="scroller"><table>
        <thead><tr><th>Name</th><th>Where</th><th>State</th><th class="num">Seen</th><th>Resolved to</th><th style="text-align:right">Actions</th></tr></thead>
        <tbody id="leads"></tbody></table></div>
    </section>

    <!-- ================= EVENTS ================= -->
    <div class="section-rule">All events</div>
    <section class="panel section" id="events">
      <div class="panel-head">
        <h2>Everything accepted</h2>
        <label class="check"><input id="show-archived" type="checkbox"> Show archived</label>
      </div>
      <p class="panel-note">The full table, including anything you have archived. Corrections you make here are pinned and survive the next refresh of the source page.</p>
      <div class="scroller"><table>
        <thead><tr><th>Competition</th><th>Category</th><th>Location</th><th>Deadline</th><th>Registration</th><th class="num">Score</th><th style="text-align:right">Actions</th></tr></thead>
        <tbody id="events-body"></tbody></table></div>
    </section>

    <!-- ================= REJECTED ================= -->
    <div class="section-rule">Rejected</div>
    <section class="panel section" id="rejected">
      <div class="panel-head">
        <h2>What was dropped, and why</h2>
        <button class="btn small" id="clear-filter" hidden>Clear filter</button>
      </div>
      <p class="panel-note">A quiet pipeline is explained here. Pick a reason to see only the pages it dropped.</p>
      <div class="chip-row" id="reasons"></div>
      <div class="scroller"><table>
        <thead><tr><th>Page</th><th>State</th><th>Reasons</th><th>Last step</th><th style="text-align:right">Actions</th></tr></thead>
        <tbody id="candidates"></tbody></table></div>
    </section>

  </main>
</div>

<div id="toast" hidden></div>

<div id="modal" hidden>
  <div class="modal-card">
    <h2 id="modal-title"></h2>
    <p class="modal-hint" id="modal-hint"></p>
    <div id="modal-body"></div>
    <div class="modal-foot">
      <button class="btn" id="modal-cancel">Cancel</button>
      <button class="btn primary" id="modal-save">Save</button>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const token = $('token');
token.value = localStorage.getItem('akaton-token') || '';
token.onchange = () => { localStorage.setItem('akaton-token', token.value); load(); };
let filter = null;
let tierFilter = null;

const esc = (v) => (v === null || v === undefined) ? '' : String(v);
function headers() { return token.value ? {'X-Akaton-Token': token.value} : {}; }
async function api(path, options = {}) {
  options.headers = {...headers(), ...(options.headers || {})};
  const r = await fetch(path, options);
  if (!r.ok) {
    if (r.status === 401) throw new Error('Dashboard token required');
    // A rejected change says why in `detail`; showing "HTTP 422" instead would leave the
    // reader guessing which part of the form the server disliked.
    const detail = await r.json().then((b) => b.detail).catch(() => null);
    throw new Error(typeof detail === 'string' ? detail : 'HTTP ' + r.status);
  }
  return r.json();
}
let toastTimer = null;
function toast(message) {
  const t = $('toast'); t.textContent = message; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3600);
}
function el(tag, text, cls) { const n = document.createElement(tag); if (text !== null && text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function cell(text, cls) { const td = el('td', esc(text), cls); return td; }
function link(text, href) {
  const td = el('td', null, '');
  const a = el('a', esc(text) || '(untitled)', 'cell-link');
  a.href = href || '#'; a.target = '_blank'; a.rel = 'noreferrer';
  td.append(a); return td;
}
function chip(text, cls) { return el('span', text, 'chip ' + (cls || '')); }
function emptyRow(body, span, text) {
  const tr = el('tr', null, ''); const td = cell(text, 'empty'); td.colSpan = span;
  tr.append(td); body.append(tr);
}

// ---------- shared formatting ----------
const DAY = 86400000;
function relativeDeadline(iso) {
  if (!iso) return null;
  const days = Math.ceil((new Date(iso) - Date.now()) / DAY);
  if (days < 0) return {text: 'closed ' + Math.abs(days) + 'd ago', level: 'urgent'};
  if (days === 0) return {text: 'closes today', level: 'urgent'};
  if (days === 1) return {text: 'closes tomorrow', level: 'urgent'};
  if (days <= 7) return {text: 'closes in ' + days + 'd', level: 'soon'};
  return {text: 'closes in ' + days + 'd', level: ''};
}
function shortDate(iso) { return iso ? new Date(iso).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) : '—'; }

document.querySelectorAll('[data-act]').forEach((b) => b.onclick = async () => {
  try { const d = await api('/api/actions/' + b.dataset.act, {method: 'POST'}); toast(d.message); setTimeout(load, 600); }
  catch (e) { toast(e.message); }
});
document.querySelectorAll('[data-sched]').forEach((b) => b.onclick = async () => {
  try { const d = await api('/api/actions/scheduler/' + b.dataset.sched, {method: 'POST'}); toast('Monitor ' + d.state.toLowerCase()); load(); }
  catch (e) { toast(e.message); }
});
document.querySelectorAll('[data-bot]').forEach((b) => b.onclick = async () => {
  b.disabled = true;
  try { const d = await api('/api/actions/bot/' + b.dataset.bot, {method: 'POST'}); toast(d.message); }
  catch (e) { toast(e.message); }
  finally { b.disabled = false; setTimeout(load, 1200); }
});
$('clear-filter').onclick = () => { filter = null; load(); };
$('show-archived').onchange = () => load();

// ---------- notification settings ----------
// Edits are held here until Save, so a slider being dragged is never half-applied to the
// running scraper and the poll cannot overwrite what is being typed.
let settingsValues = null;
let settingsControls = [];
let settingsDraft = {};

function settingsDirty() { return Object.keys(settingsDraft).length > 0; }

function renderSettings(data) {
  const changed = JSON.stringify(settingsControls) !== JSON.stringify(data.controls);
  settingsControls = data.controls;
  // Never clobber a pending edit with a poll.
  if (!settingsDirty()) settingsValues = data.values;
  const channel = data.channel || {};
  const chipEl = $('settings-channel');
  const live = settingsValues.notifications_enabled;
  chipEl.textContent = !channel.configured ? 'no channel configured'
    : channel.bot === 'RUNNING' ? (live ? 'alerts on · connected' : 'shadow mode · connected')
    : 'Discord not connected';
  chipEl.className = 'chip mono ' + (!channel.configured || channel.bot !== 'RUNNING' ? 'standby'
    : live ? 'signal' : 'standby');
  const grid = $('settings-grid');
  if (changed || !grid.childElementCount) grid.replaceChildren();
  if (!grid.childElementCount) {
    for (const control of settingsControls) grid.append(settingCard(control));
  }
  for (const control of settingsControls) syncSetting(control);
  renderSettingsFoot();
}

function settingCard(control) {
  const card = el('div', null, 'setting');
  const head = el('div', null, 'setting-head');
  head.append(el('div', control.label, 'setting-label'));
  if (control.kind === 'toggle') {
    const sw = el('label', null, 'switch');
    const input = document.createElement('input');
    input.type = 'checkbox'; input.id = 'set-' + control.key;
    input.setAttribute('aria-label', control.label);
    input.onchange = () => stageSetting(control.key, input.checked);
    sw.append(input, el('span', null, 'track'), el('span', null, 'knob'));
    head.append(sw);
  } else {
    head.append(el('div', '—', 'setting-value'));
  }
  card.append(head);
  card.append(el('p', control.help, 'setting-help'));
  if (control.kind !== 'toggle') {
    const slider = document.createElement('input');
    slider.type = 'range'; slider.className = 'slider'; slider.id = 'set-' + control.key;
    slider.min = control.min; slider.max = control.max; slider.step = 1;
    slider.setAttribute('aria-label', control.label);
    slider.oninput = () => stageSetting(control.key, Number(slider.value));
    card.append(slider);
    const scale = el('div', null, 'scale');
    scale.append(el('span', String(control.min), ''));
    if (control.unit) scale.append(el('span', control.unit, ''));
    scale.append(el('span', String(control.max), ''));
    card.append(scale);
  }
  return card;
}

function stageSetting(key, value) {
  if (settingsValues[key] === value) delete settingsDraft[key];
  else settingsDraft[key] = value;
  for (const control of settingsControls) syncSetting(control);
  renderSettingsFoot();
}

function syncSetting(control) {
  const input = $('set-' + control.key);
  if (!input) return;
  const value = control.key in settingsDraft ? settingsDraft[control.key] : settingsValues[control.key];
  if (control.kind === 'toggle') { input.checked = Boolean(value); return; }
  if (document.activeElement !== input) input.value = value;
  const readout = input.closest('.setting').querySelector('.setting-value');
  if (readout) {
    readout.textContent = value;
    readout.style.color = control.key in settingsDraft ? 'var(--standby)' : 'var(--signal)';
  }
}

function renderSettingsFoot() {
  const dirty = settingsDirty();
  $('settings-save').disabled = !dirty;
  $('settings-reset').disabled = !dirty;
  const count = Object.keys(settingsDraft).length;
  $('settings-state').textContent = dirty
    ? count + (count === 1 ? ' unsaved change' : ' unsaved changes')
    : 'No unsaved changes';
}

$('settings-save').onclick = async () => {
  const button = $('settings-save'); button.disabled = true;
  try {
    const d = await api('/api/settings', {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({values: settingsDraft}),
    });
    settingsDraft = {}; settingsValues = d.settings;
    toast(d.written && d.written.length ? d.message + ' · saved to ' + d.written.join(' and ') : d.message);
  } catch (e) { toast(e.message); }
  finally { renderSettingsFoot(); setTimeout(load, 400); }
};
$('settings-reset').onclick = () => {
  settingsDraft = {};
  for (const control of settingsControls) syncSetting(control);
  renderSettingsFoot();
};

// ---------- detections ----------
const TIERS = [
  ['HIGH_PRIORITY', 'High priority'],
  ['RECOMMENDED', 'Recommended'],
  ['POSSIBLE', 'Possible'],
  ['WEAK', 'Weak'],
];
function renderTierFilters(rows) {
  const box = $('tier-filters'); box.replaceChildren();
  const counts = {};
  for (const row of rows) counts[row.tier] = (counts[row.tier] || 0) + 1;
  for (const [key, label] of TIERS) {
    if (!counts[key] && tierFilter !== key) continue;
    const b = el('button', label + ' · ' + (counts[key] || 0), 'pill');
    b.type = 'button';
    b.setAttribute('aria-pressed', String(tierFilter === key));
    b.onclick = () => { tierFilter = tierFilter === key ? null : key; load(); };
    box.append(b);
  }
}

const TIER_CHIP = {HIGH_PRIORITY: 'fault', RECOMMENDED: 'signal', POSSIBLE: 'standby', WEAK: ''};

function detectionCard(row) {
  const card = el('article', null, 'card tier-' + row.tier);

  // Only shown when there is a real poster on a host we would link to. A card without
  // one starts at the title instead of at a placeholder.
  if (row.image_url) {
    const cover = el('div', null, 'card-cover');
    const img = document.createElement('img');
    img.src = row.image_url; img.alt = ''; img.loading = 'lazy';
    // A poster that 404s leaves a blank band, so the cover goes with it.
    img.onerror = () => cover.remove();
    cover.append(img);
    card.append(cover);
  }

  const body = el('div', null, 'card-body');

  const org = el('div', null, 'card-org');
  if (row.icon_url) {
    const icon = document.createElement('img');
    icon.src = row.icon_url; icon.alt = ''; icon.loading = 'lazy';
    icon.onerror = () => icon.remove();
    org.append(icon);
  }
  // The category is already a chip further down; using it as a stand-in for the
  // organizer labelled "Cebu Startup Weekend" as being run by "Hackathon".
  org.append(el('span', row.organizer || 'Organizer not named', ''));
  if (row.announced) org.append(chip('alerted', 'signal bare mono'));
  body.append(org);

  const title = el('h3', null, 'card-title');
  const a = el('a', row.title || '(untitled)', '');
  a.href = row.canonical_url || '#'; a.target = '_blank'; a.rel = 'noreferrer';
  title.append(a); body.append(title);

  if (row.summary) body.append(el('p', row.summary, 'card-summary'));

  const facts = el('div', null, 'card-facts');
  if (row.location) facts.append(chip(row.location));
  if (row.category) facts.append(chip(row.category));
  if (row.prize) facts.append(chip(String(row.prize).slice(0, 34)));
  if (row.registration === 'OPEN') facts.append(chip('registration open', 'signal'));
  body.append(facts);

  const foot = el('div', null, 'card-foot');
  const score = el('div', null, 'card-score');
  score.append(el('b', String(row.score), ''), document.createTextNode(' / 100'));
  foot.append(score);
  const due = relativeDeadline(row.deadline);
  foot.append(el('span', due ? due.text : (row.event_start ? 'starts ' + shortDate(row.event_start) : 'no date'),
    'countdown ' + (due ? due.level : '')));
  body.append(foot);

  card.append(body);
  return card;
}

function renderDetections(rows) {
  renderTierFilters(rows);
  const shown = tierFilter ? rows.filter((r) => r.tier === tierFilter) : rows;
  const grid = $('detections-grid'); grid.replaceChildren();
  $('nav-detections').textContent = rows.length || '';
  if (!shown.length) {
    grid.append(el('p', rows.length
      ? 'Nothing in this tier. Pick another, or clear the filter.'
      : 'No competitions found yet. Run discovery to start the first sweep.', 'empty'));
    return;
  }
  for (const row of shown) grid.append(detectionCard(row));
}

// ---------- backdate ----------
let backfillSources = [];
function renderSources(names) {
  if (JSON.stringify(names) === JSON.stringify(backfillSources)) return;
  backfillSources = names;
  const box = $('bf-sources'); box.replaceChildren();
  for (const name of names) {
    const label = el('label', null, 'check');
    const input = document.createElement('input');
    input.type = 'checkbox'; input.value = name;
    input.checked = name !== 'devpost' && name !== 'kaggle';
    label.append(input, el('span', name, ''));
    box.append(label);
  }
}
(() => {
  const start = new Date(); start.setDate(start.getDate() - 30);
  $('bf-since').value = start.toISOString().slice(0, 10);
  $('bf-since').max = new Date().toISOString().slice(0, 10);
})();

$('bf-run').onclick = async () => {
  const since = $('bf-since').value;
  if (!since) { toast('Pick a date to read back from'); return; }
  const sources = [...$('bf-sources').querySelectorAll('input:checked')].map((i) => i.value);
  const queries = Number($('bf-queries').value) || 16;
  const button = $('bf-run'); button.disabled = true;
  try {
    const d = await api('/api/actions/backfill', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({since, sources, queries}),
    });
    toast(d.message);
  } catch (e) { toast(e.message); button.disabled = false; }
  finally { setTimeout(load, 800); }
};

async function cancelJob(name) {
  try { const d = await api('/api/actions/jobs/' + name + '/cancel', {method: 'POST'}); toast(d.message); }
  catch (e) { toast(e.message); }
  finally { setTimeout(load, 600); }
}
$('bf-cancel').onclick = () => cancelJob('backfill');
document.querySelectorAll('[data-cancel]').forEach((b) => b.onclick = () => cancelJob(b.dataset.cancel));

const STATUS_CHIP = {SUCCEEDED: 'signal', FAILED: 'fault', CANCELLED: 'standby'};
function renderBackfill(monitor) {
  const running = Boolean((monitor.running || {}).backfill);
  const run = (monitor.last_runs || {}).backfill;
  const button = $('bf-run');
  button.disabled = running;
  button.textContent = running ? 'Reading…' : 'Start read-back';
  $('bf-cancel').hidden = !running;
  const box = $('bf-status'); box.replaceChildren();
  if (!run) { box.append(chip('No read-back yet', 'bare')); return; }
  const started = new Date(run.started_at).toLocaleTimeString();
  if (run.status === 'RUNNING') {
    box.append(chip('Running since ' + started, 'standby'));
    box.append(chip('collectors keep working while you watch', 'bare'));
    return;
  }
  box.append(chip(run.status.toLowerCase(), STATUS_CHIP[run.status] || ''));
  box.append(chip('started ' + started, 'bare'));
  if (run.error) box.append(chip(run.error, 'fault'));
  for (const [key, value] of Object.entries(run.result || {})) {
    box.append(chip(key.replaceAll('_', ' ') + ' · ' + value, 'mono'));
  }
}

function renderSearches(rows) {
  const box = $('searches'); box.replaceChildren();
  if (!rows.length) { box.append(el('p', 'No searches recorded yet.', 'empty')); return; }
  for (const s of rows) {
    const failed = s.status === 'FAILED';
    const row = el('div', null, 'log-row' + (failed ? ' failed' : ''));
    const head = el('div', null, 'log-head');
    head.append(el('span', s.query, 'log-q'));
    head.append(chip(failed ? 'failed' : s.result_count + ' results', failed ? 'fault' : 'signal'));
    row.append(head);
    if (s.error) row.append(el('p', s.error, 'log-err'));
    box.append(row);
  }
}

// ---------- events table ----------
function actionCell(buttons) {
  const td = el('td', null, 'actions');
  for (const b of buttons) td.append(b);
  return td;
}
function rowButton(label, cls, handler, title) {
  const button = el('button', label, 'btn small ' + (cls || ''));
  if (title) button.title = title;
  button.onclick = async () => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = '…';
    try { const d = await handler(); if (d && d.message) toast(d.message); }
    catch (e) { toast(e.message); }
    finally { button.textContent = original; button.disabled = false; setTimeout(load, 500); }
  };
  return button;
}

function eventActions(event) {
  return actionCell([
    rowButton('Send', '', () => api('/api/actions/events/' + event.id + '/notify', {method: 'POST'}),
      'Post this to Discord now, ignoring the alert score'),
    rowButton('Edit', '', async () => { openEventEditor(event); }, 'Correct this event by hand'),
    rowButton(event.archived_at ? 'Restore' : 'Archive', 'danger', () =>
      event.archived_at
        ? api('/api/events/' + event.id + '/restore', {method: 'POST'})
        : api('/api/events/' + event.id, {method: 'DELETE'}),
      'Archived events are hidden and never alert again, but are never deleted'),
  ]);
}

function renderEvents(rows) {
  const body = $('events-body'); body.replaceChildren();
  $('nav-events').textContent = rows.length || '';
  if (!rows.length) { emptyRow(body, 7, 'No accepted events yet.'); return; }
  for (const e of rows) {
    const tr = el('tr', null, '');
    tr.append(link(e.title, e.canonical_url));
    tr.append(cell((e.category || '').replaceAll('_', ' '), 'cell-dim'));
    const loc = [e.location && e.location.city, e.location && e.location.region].filter(Boolean).join(' — ');
    tr.append(cell(loc || (e.location && e.location.location_type) || '—', 'cell-dim'));
    tr.append(cell(e.deadline ? shortDate(e.deadline) : '—', 'cell-dim'));
    const rt = el('td', null, '');
    rt.append(chip((e.registration || 'unknown').toLowerCase(), e.registration === 'OPEN' ? 'signal' : ''));
    tr.append(rt);
    tr.append(cell(e.score, 'num'));
    tr.append(eventActions(e));
    body.append(tr);
  }
}

const EVENT_FIELDS = [
  ['title', 'Title', 'text'], ['organizer', 'Organizer', 'text'],
  ['category', 'Category', 'text'], ['city', 'City', 'text'], ['country', 'Country', 'text'],
  ['event_start', 'Event start', 'date'], ['registration_deadline', 'Deadline', 'date'],
  ['canonical_url', 'Official URL', 'url'], ['registration_url', 'Registration URL', 'url'],
];

function openEventEditor(event) {
  const pinned = new Set(event.pinned || []);
  const values = event.editable || {};
  const form = $('modal-body'); form.replaceChildren();
  const inputs = {};
  for (const [name, label, type] of EVENT_FIELDS) {
    const wrap = el('label', null, 'modal-field');
    const head = el('div', null, 'lab');
    head.append(document.createTextNode(label));
    if (pinned.has(name)) {
      const release = el('button', 'pinned ✕', 'pin');
      release.type = 'button';
      release.title = 'Let this field follow the source page again';
      release.onclick = async () => {
        try { const d = await api('/api/events/' + event.id + '/overrides/' + name, {method: 'DELETE'}); toast(d.message); closeModal(); setTimeout(load, 400); }
        catch (e) { toast(e.message); }
      };
      head.append(release);
    }
    wrap.append(head);
    const input = document.createElement('input');
    input.type = type === 'date' ? 'date' : 'text';
    let value = values[name] == null ? '' : String(values[name]);
    if (type === 'date' && value) value = value.slice(0, 10);
    input.value = value;
    wrap.append(input);
    inputs[name] = {input, original: value};
    form.append(wrap);
  }
  $('modal-title').textContent = 'Correct this event';
  $('modal-hint').textContent = 'Only the fields you change are pinned. A pinned field keeps your value when the source page is read again.';
  $('modal-save').onclick = async () => {
    const fields = {};
    for (const [name, {input, original}] of Object.entries(inputs)) {
      if (input.value !== original) fields[name] = input.value;
    }
    if (!Object.keys(fields).length) { closeModal(); return; }
    try {
      const d = await api('/api/events/' + event.id, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({fields}),
      });
      toast(d.message); closeModal(); setTimeout(load, 400);
    } catch (e) { toast(e.message); }
  };
  $('modal').hidden = false;
}

function closeModal() { $('modal').hidden = true; }
$('modal-cancel').onclick = closeModal;
$('modal').onclick = (e) => { if (e.target === $('modal')) closeModal(); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

// ---------- leads ----------
const LEAD_CHIP = {RESOLVED: 'signal', UNRESOLVED: 'standby', DISCARDED: ''};
function renderLeads(rows) {
  const body = $('leads'); body.replaceChildren();
  $('leads-count').textContent = rows.length ? rows.length + ' tracked' : '';
  $('nav-mentions').textContent = rows.length || '';
  if (!rows.length) { emptyRow(body, 6, 'No mentions recorded yet.'); return; }
  for (const lead of rows) {
    const tr = el('tr', null, '');
    const name = lead.name + (lead.edition_hint ? ' · ' + lead.edition_hint : '');
    tr.append(lead.source_url ? link(name, lead.source_url) : cell(name, ''));
    tr.append(cell(lead.platform + ' · ' + lead.mention_kind, 'cell-dim'));
    const state = el('td', null, '');
    state.append(chip((lead.state || '').toLowerCase(), LEAD_CHIP[lead.state] || ''));
    if (lead.last_error) { state.append(el('div', lead.last_error.slice(0, 60), 'cell-fault')); }
    tr.append(state);
    tr.append(cell(lead.sightings, 'num'));
    const shown = lead.resolved_url ? lead.resolved_url.replace(/^https?:[/][/]/, '') : '';
    tr.append(lead.resolved_url ? link(shown.slice(0, 48), lead.resolved_url) : cell('—', 'cell-dim'));
    tr.append(actionCell([
      rowButton('Rename', '', async () => { openLeadEditor(lead); },
        'Fix a badly read name; the lead is re-keyed so its cooldown follows'),
      rowButton('Search now', '', () => api('/api/leads/' + lead.id + '/search-now', {method: 'POST'}),
        'Clear the cooldown so the next run spends a search on this'),
      rowButton('Delete', 'danger', () => api('/api/leads/' + lead.id, {method: 'DELETE'})),
    ]));
    body.append(tr);
  }
}

function openLeadEditor(lead) {
  const form = $('modal-body'); form.replaceChildren();
  const inputs = {};
  for (const [name, label] of [['name', 'Name'], ['edition_hint', 'Edition hint']]) {
    const wrap = el('label', null, 'modal-field');
    wrap.append(el('div', label, 'lab'));
    const input = document.createElement('input');
    input.type = 'text';
    input.value = lead[name] == null ? '' : String(lead[name]);
    wrap.append(input); inputs[name] = input; form.append(wrap);
  }
  $('modal-title').textContent = 'Rename this mention';
  $('modal-hint').textContent = 'Renaming re-keys the lead, so its cooldown follows the corrected name rather than the misspelling.';
  $('modal-save').onclick = async () => {
    const body = {};
    for (const [name, input] of Object.entries(inputs)) body[name] = input.value || null;
    try {
      const d = await api('/api/leads/' + lead.id, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      });
      toast(d.message); closeModal(); setTimeout(load, 400);
    } catch (e) { toast(e.message); }
  };
  $('modal').hidden = false;
}

// ---------- llm ----------
function renderLlm(data) {
  const tiers = (data && data.tiers) || [];
  $('llm-panel').hidden = tiers.length < 1;
  if (!tiers.length) return;
  $('llm-hint').textContent = tiers.length > 1
    ? 'The first host reads every page that needs a model. The second is asked only when the first leaves confidence below '
      + data.escalation_confidence + ', at most ' + data.escalations_per_run + ' times a run.'
    : 'One host configured. Reading is deterministic first; the model only fills the gaps.';
  const box = $('llm-tiers'); box.replaceChildren();
  for (const tier of tiers) {
    const card = el('div', null, 'tier' + (tier.primary ? ' primary' : ''));
    const head = el('div', null, 'setting-head');
    head.append(el('span', tier.model || '—', 'tier-name'));
    head.append(chip(tier.role, tier.primary ? 'signal' : ''));
    card.append(head);
    const meta = el('div', null, 'tier-meta');
    meta.append(document.createTextNode(tier.host));
    meta.append(chip(tier.reachable === true ? 'reachable' : tier.reachable === false ? 'unreachable' : 'unknown',
      tier.reachable === true ? 'signal' : tier.reachable === false ? 'fault' : ''));
    if ((tier.loaded || []).length) meta.append(chip('loaded: ' + tier.loaded.join(', '), 'mono'));
    card.append(meta);
    if (!tier.primary) {
      const promote = rowButton('Ask this one first', '', () => api('/api/actions/llm/primary', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({host: tier.host}),
      }), 'Move this host to the front of the ladder');
      promote.style.marginTop = '10px';
      card.append(promote);
    }
    box.append(card);
  }
}

// ---------- rejections ----------
function renderReasons(counts) {
  const box = $('reasons'); box.replaceChildren();
  const entries = Object.entries(counts || {});
  if (!entries.length) { box.append(chip('Nothing rejected yet', 'bare')); return; }
  for (const [code, n] of entries) {
    const b = el('button', code.replaceAll('_', ' ').toLowerCase() + ' · ' + n, 'pill');
    b.type = 'button';
    b.setAttribute('aria-pressed', String(filter === code));
    b.onclick = () => { filter = (filter === code ? null : code); load(); };
    box.append(b);
  }
}

function renderCandidates(rows) {
  const body = $('candidates'); body.replaceChildren();
  $('clear-filter').hidden = !filter;
  if (!rows.length) { emptyRow(body, 5, 'Nothing recorded yet.'); return; }
  for (const c of rows) {
    const tr = el('tr', null, '');
    tr.append(link(c.title || c.url, c.url));
    const st = el('td', null, '');
    st.append(chip((c.state || '').toLowerCase(), c.state === 'REJECTED' ? 'fault' : ''));
    tr.append(st);
    tr.append(cell((c.rejection_reasons || []).join(', ').replaceAll('_', ' ').toLowerCase() || '—', 'cell-fault'));
    const step = c.last_trace ? (c.last_trace.state + (c.last_trace.failure ? ' · ' + c.last_trace.failure : '')) : '—';
    tr.append(cell(step, 'cell-dim'));
    tr.append(actionCell([
      rowButton('Retry', '', () => api('/api/candidates/' + c.id + '/retry', {method: 'POST'}),
        'Put this page back through the pipeline, usually after a rule changed'),
      rowButton('Delete', 'danger', () => api('/api/candidates/' + c.id, {method: 'DELETE'})),
    ]));
    body.append(tr);
  }
}

// ---------- poll ----------
async function load() {
  try {
    const candidateUrl = '/api/candidates?limit=60' + (filter ? '&reason=' + encodeURIComponent(filter) : '');
    const eventsUrl = '/api/events' + ($('show-archived').checked ? '?archived=true' : '');
    const [s, ev, cand, rej, searches, leads, llm, found, settings] = await Promise.all([
      api('/api/status'), api(eventsUrl), api(candidateUrl), api('/api/rejections'),
      api('/api/searches'), api('/api/leads'), api('/api/llm'),
      api('/api/detections?limit=48'), api('/api/settings'),
    ]);
    renderSettings(settings);
    renderDetections(found);
    renderLlm(llm);
    renderLeads(leads);

    const announced = found.filter((d) => d.announced).length;
    $('k-candidates').textContent = s.counts.candidates;
    $('k-events').textContent = s.counts.events;
    $('k-notifications').textContent = s.counts.notifications;
    $('k-rejected').textContent = rej.total;
    $('k-announced').textContent = found.length ? announced + ' of ' + found.length + ' alerted' : 'none yet';
    const pending = Object.entries(s.candidate_states)
      .filter(([k]) => k !== 'NOTIFIED' && k !== 'SUPPRESSED' && k !== 'REJECTED')
      .reduce((n, [, v]) => n + v, 0);
    $('k-states').textContent = pending ? pending + ' still moving' : 'all settled';
    $('k-alerts-mode').textContent = s.configuration.notifications_enabled ? 'alerts on' : 'shadow mode';
    $('nav-rejected').textContent = rej.total || '';

    const scheduler = s.monitor.scheduler;
    $('k-scheduler').textContent = scheduler.toLowerCase();
    $('k-scheduler').className = 'v word ' + (scheduler === 'RUNNING' ? 'is-signal' : scheduler === 'PAUSED' ? 'is-standby' : 'is-muted');
    const bot = s.bot || {state: 'UNKNOWN'};
    $('k-bot').textContent = bot.state.replaceAll('_', ' ').toLowerCase();
    $('k-bot').className = 'v word ' + (bot.state === 'RUNNING' ? 'is-signal' : bot.state === 'STOPPED' ? 'is-standby' : 'is-muted');
    $('k-bot-detail').textContent = bot.last_error ? bot.last_error
      : bot.user ? 'as ' + bot.user
      : bot.state === 'NOT_CONFIGURED' ? 'no token configured' : 'not connected';

    $('rail-scheduler').textContent = scheduler.toLowerCase();
    $('rail-bot').textContent = bot.state.replaceAll('_', ' ').toLowerCase();
    $('rail-alerts').textContent = s.configuration.notifications_enabled ? 'on' : 'shadow';
    renderSources(s.monitor.sources || ['search']);
    renderBackfill(s.monitor);
    for (const b of document.querySelectorAll('[data-cancel]')) {
      b.hidden = !(s.monitor.running || {})[b.dataset.cancel];
    }
    const next = (s.monitor.jobs || []).map((j) => j.next_run_at).filter(Boolean).sort()[0];
    const nextText = next ? new Date(next).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : 'none scheduled';
    $('k-next').textContent = next ? 'next ' + nextText : 'no job scheduled';
    $('rail-next').textContent = nextText;
    $('subtitle').textContent = s.configuration.search_provider + ' search · ' + s.configuration.llm_provider +
      ' extraction · ' + s.configuration.timezone;

    const st = $('states'); st.replaceChildren();
    for (const [k, v] of Object.entries(s.candidate_states)) st.append(chip(k.replaceAll('_', ' ').toLowerCase() + ' · ' + v, 'mono'));

    const lr = $('lastruns'); lr.replaceChildren();
    const runs = Object.entries(s.monitor.last_runs || {});
    if (!runs.length) lr.append(el('p', 'No manual run yet.', 'empty'));
    for (const [name, r] of runs) {
      lr.append(el('p', name + ': ' + r.status + (r.result ? ' — ' + JSON.stringify(r.result) : '') + (r.error ? ' — ' + r.error : ''), ''));
    }
    renderSearches(searches); renderEvents(ev); renderReasons(rej.counts); renderCandidates(cand);
  } catch (e) { toast(e.message); }
}
load(); setInterval(load, 8000);
</script>
</body></html>
"""
