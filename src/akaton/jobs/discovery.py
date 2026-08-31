from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from akaton.config import ConfigBundle
from akaton.discovery.base import SearchProvider, SearchRequest, SourceAdapter
from akaton.discovery.queries import (
    ScheduledQuery,
    choose_due_queries,
    configured_queries,
    organizer_queries,
)
from akaton.discovery.resolver import LeadResolver
from akaton.domain.models import CandidateSeed, MentionLead
from akaton.persistence.database import Database
from akaton.persistence.repository import Repository
from akaton.pipeline import CandidatePipeline
from akaton.processing.normalize import normalize_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LeadTask:
    """One lead due for its search, detached from the session that produced it."""

    lead_id: int
    mention: MentionLead


def _interleave(
    queries: list[ScheduledQuery], leads: list[_LeadTask]
) -> list[ScheduledQuery | _LeadTask]:
    """Round-robin the two kinds of work through one paced loop.

    Not concatenation: putting all the leads first or last would cluster requests of one
    shape, and a run cut short partway through would systematically starve whichever kind
    came second.
    """
    merged: list[ScheduledQuery | _LeadTask] = []
    step = max(1, len(queries) // (len(leads) + 1)) if leads else 1
    remaining = list(leads)
    for index, query in enumerate(queries):
        if remaining and index and index % step == 0:
            merged.append(remaining.pop(0))
        merged.append(query)
    merged.extend(remaining)
    return merged


class DiscoveryJob:
    def __init__(
        self,
        database: Database,
        config: ConfigBundle,
        provider: SearchProvider,
        pipeline: CandidatePipeline,
        adapters: list[SourceAdapter] | None = None,
        resolver: LeadResolver | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.provider = provider
        self.pipeline = pipeline
        self.adapters = adapters or []
        # Without a resolver, mentions are still recorded — they are the record of what
        # the group is talking about — but nothing is searched for them.
        self.resolver = resolver

    async def run(
        self,
        *,
        since: date | None = None,
        historical_test: bool = False,
        query_limit: int | None = None,
        sources: Collection[str] | None = None,
    ) -> dict[str, int]:
        """Run one discovery pass.

        `sources` names which collectors to use — "search" for the query rotation, or an
        adapter's own name. None means whatever this mode normally runs, which for a
        historical backfill is search alone.
        """
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self.database.session() as session:
            repo = Repository(session)
            used = await repo.monthly_search_requests(self.provider.name, month_start)
            history = {} if historical_test else await repo.search_history(self.provider.name)
        budget_remaining = self.config.app.monthly_search_budget - used
        requested = query_limit or self.config.app.discovery_queries_per_run
        query_count = min(requested, max(0, budget_remaining))
        if query_count < requested:
            # Otherwise a run cut short by the monthly budget is indistinguishable from a
            # week with nothing to find: no error, no candidates, no explanation.
            logger.warning(
                "discovery_budget_exhausted",
                extra={
                    "requested": requested,
                    "granted": query_count,
                    "used": used,
                    "budget": self.config.app.monthly_search_budget,
                },
            )
        all_queries = [
            *configured_queries(self.config.queries),
            *organizer_queries(self.config.sources),
        ]
        # "leads" counts mentions recorded this run; "lead_searches" counts the requests
        # spent resolving them. They are deliberately separate: a busy week in the group
        # raises the first without touching the second, which is the whole point.
        counts = {
            "queries": 0,
            "candidates": 0,
            "processed": 0,
            "errors": 0,
            "leads": 0,
            "lead_searches": 0,
        }

        # Leads live inside the same allocation as the scheduled queries, not beside it.
        # `query_count` is fixed before anything runs, and the pause that keeps SearXNG's
        # engines alive exists only in this one loop — a second sub-loop for leads would
        # both overshoot the budget and reintroduce the unpaced burst.
        lead_share = 0
        if not historical_test and (sources is None or "search" in sources):
            lead_share = min(self.config.app.mention_leads_per_run, query_count // 3)
        leads = await self._due_leads(lead_share)
        if sources is not None and "search" not in sources:
            selected = []
        else:
            selected = choose_due_queries(all_queries, history, query_count - len(leads), now=now)
        pause = self.config.app.search_interval_seconds
        for index, task in enumerate(_interleave(selected, leads)):
            if index and pause:
                # Space the queries out. SearXNG scrapes consumer search pages, and a
                # back-to-back burst gets its engines suspended for the whole run.
                await asyncio.sleep(pause)
            if isinstance(task, _LeadTask):
                await self._run_lead(task, counts)
            else:
                await self._run_query(
                    task, counts, since=since, now=now, historical_test=historical_test
                )

        for adapter in self._adapters_for(sources, historical_test=historical_test):
            adapter_settings = self.config.sources.get("structured_sources", {}).get(
                adapter.name, {}
            )
            cadence = int(adapter_settings.get("cadence_hours", 24))
            async with self.database.session() as session:
                adapter_history = await Repository(session).search_history(adapter.name)
            last_adapter_run = adapter_history.get(("structured", adapter.name))
            due = not (last_adapter_run and last_adapter_run + timedelta(hours=cadence) > now)
            # Naming a source is an explicit instruction, so the cadence does not apply:
            # someone asking to backdate Facebook to June means now, not in six hours.
            if not due and sources is None:
                continue
            try:
                seeds = await adapter.discover(since=_as_datetime(since))
                # A collector can come back empty because it never ran: no login, no
                # browser, a challenge page. It reports that itself, because only it can
                # tell the difference between that and a week with nothing posted.
                adapter_error = getattr(adapter, "last_error", None)
                if adapter_error:
                    counts["errors"] += 1
            except Exception:
                seeds = []
                adapter_error = "adapter_failed"
                counts["errors"] += 1
                logger.exception("source_adapter_failed", extra={"adapter": adapter.name})
            async with self.database.session() as session:
                await Repository(session).record_search_run(
                    adapter.name,
                    "structured",
                    adapter.name,
                    len(seeds),
                    adapter_error,
                )
            # Mentions are recorded whether or not the adapter also produced seeds, and
            # even on an error: a thread that names a competition is evidence regardless
            # of whether the rest of the run went well. They are searched in the *next*
            # run, which is deliberate — the allocation for this one was fixed before the
            # adapters ran, and spending past it is what the pause exists to prevent.
            counts["leads"] += await self._record_mentions(adapter)
            if adapter_error:
                continue
            discovered, processed, failed = await self._process_seeds(seeds)
            counts["candidates"] += discovered
            counts["processed"] += processed
            counts["errors"] += failed
        return counts

    async def _record_mentions(self, adapter: SourceAdapter) -> int:
        mentions = list(getattr(adapter, "last_mentions", []) or [])
        if not mentions:
            return 0
        async with self.database.session() as session:
            repo = Repository(session)
            for mention in mentions:
                await repo.record_mention(mention)
        logger.info("mentions_recorded", extra={"adapter": adapter.name, "mentions": len(mentions)})
        return len(mentions)

    async def _run_query(
        self,
        item: ScheduledQuery,
        counts: dict[str, int],
        *,
        since: date | None,
        now: datetime,
        historical_test: bool,
    ) -> None:
        query = item.query
        freshness = item.freshness
        if since:
            # Date range is expressed through freshness/time_range, which every engine
            # understands. An `after:` operator is Google and Brave syntax: Mojeek,
            # Bing and the rest return nothing for it, so it silently emptied backfill
            # runs whenever the big engines were throttled. Seeds older than `since`
            # are filtered out below by published_hint instead.
            freshness = _freshness_for_since(since, now.date())
        try:
            page = await self.provider.search(SearchRequest(query=query, freshness=freshness))
            error = None
        except httpx.HTTPStatusError as exc:
            page = None
            error = f"HTTP {exc.response.status_code}"
        except Exception as exc:
            page = None
            error = type(exc).__name__
        if page is not None and not page.results and page.degraded:
            # Zero results while engines are unavailable is not evidence of absence:
            # record it so a throttled backend is visible instead of looking like a
            # quiet week. Engines that answered with nothing are not listed here, so
            # this names the ones that were actually unreachable.
            error = "no results; engines unavailable: " + "; ".join(page.unresponsive_engines)
        async with self.database.session() as session:
            await Repository(session).record_search_run(
                self.provider.name,
                f"backfill:{item.group}" if historical_test else item.group,
                query,
                len(page.results) if page else 0,
                error,
            )
        counts["queries"] += 1
        if error:
            counts["errors"] += 1
            return
        seeds = [
            seed
            for seed in page.results
            if not (since and seed.published_hint and seed.published_hint.date() < since)
        ]
        discovered, processed, failed = await self._process_seeds(
            seeds, historical_test=historical_test
        )
        counts["candidates"] += discovered
        counts["processed"] += processed
        counts["errors"] += failed

    async def _due_leads(self, limit: int) -> list[_LeadTask]:
        """Leads worth a search this run, read out of the session before it closes."""
        if limit <= 0 or self.resolver is None:
            return []
        async with self.database.session() as session:
            rows = await Repository(session).due_leads(limit)
            return [
                _LeadTask(
                    lead_id=row.id,
                    mention=MentionLead(
                        name=row.name,
                        normalized_name=row.normalized_name,
                        edition_hint=row.edition_hint,
                        platform=row.platform,
                        mention_kind=row.mention_kind,
                        source_url=row.source_url,
                        source_key=row.source_key,
                        excerpt=row.mention_excerpt,
                        # Rebuilt rather than stored: the hint belongs in the query, and
                        # keeping one canonical way to compose it avoids two of them.
                        query=" ".join(part for part in (row.name, row.edition_hint) if part),
                    ),
                )
                for row in rows
            ]

    async def _run_lead(self, task: _LeadTask, counts: dict[str, int]) -> None:
        """Spend one search on a mention, and put whatever it finds through the pipeline.

        The search is also recorded as a normal search run, so budget accounting and the
        dashboard's Search-health panel need no special case for leads.
        """
        mention = task.mention
        try:
            seed, reason = await self.resolver.resolve(mention, task.lead_id)
            error = None if seed else reason
        except Exception as exc:
            seed, error = None, type(exc).__name__
            logger.exception("lead_resolution_failed", extra={"lead": mention.name})
        async with self.database.session() as session:
            repo = Repository(session)
            await repo.record_search_run(
                self.provider.name,
                "mention",
                self.resolver.query_for(mention),
                1 if seed else 0,
                error,
            )
            await repo.mark_lead_searched(
                task.lead_id,
                resolved_url=str(seed.url) if seed else None,
                error=error,
            )
        counts["queries"] += 1
        counts["lead_searches"] += 1
        if not seed:
            return
        discovered, processed, failed = await self._process_seeds([seed])
        counts["candidates"] += discovered
        counts["processed"] += processed
        counts["errors"] += failed

    def _adapters_for(
        self, sources: Collection[str] | None, *, historical_test: bool
    ) -> list[SourceAdapter]:
        if sources is not None:
            return [adapter for adapter in self.adapters if adapter.name in sources]
        # A historical run defaults to search alone: the structured adapters only publish
        # what is open right now, so replaying them against a past date finds nothing.
        return [] if historical_test else self.adapters

    async def _process_seeds(
        self, seeds: list[CandidateSeed], *, historical_test: bool = False
    ) -> tuple[int, int, int]:
        """Process one page of seeds with bounded concurrency.

        FetchManager already enforces per-host rate limits and concurrency, so running
        candidates in parallel only overlaps work across different domains. Seeds are
        deduplicated first because candidates are keyed on the normalized URL, and two
        parallel inserts of the same URL would race on that unique index.
        """
        unique: dict[str, CandidateSeed] = {}
        rejected = 0
        for seed in seeds:
            try:
                key = normalize_url(str(seed.url))
            except Exception:
                # normalize_url IDNA-encodes the hostname, which raises on a label over 63
                # characters or a trailing dot — both of which pydantic's HttpUrl accepts,
                # so a single malformed search result used to abort the whole page.
                logger.warning("seed_url_unusable", extra={"url": str(seed.url)})
                rejected += 1
                continue
            unique.setdefault(key, seed)
        seeds = list(unique.values())
        if not seeds:
            return rejected, 0, rejected
        limit = asyncio.Semaphore(self.config.app.discovery_concurrency)

        async def run(seed: CandidateSeed) -> bool:
            async with limit:
                try:
                    await self.pipeline.process(seed, historical_test=historical_test)
                    return True
                except Exception:
                    logger.exception("candidate_pipeline_failed", extra={"url": str(seed.url)})
                    return False

        outcomes = await asyncio.gather(*(run(seed) for seed in seeds))
        processed = sum(outcomes)
        return len(outcomes) + rejected, processed, len(outcomes) - processed + rejected


def _as_datetime(since: date | None) -> datetime | None:
    """Adapters compare `since` against a tz-aware cutoff, so a bare date would raise."""
    if since is None:
        return None
    if isinstance(since, datetime):
        return since if since.tzinfo else since.replace(tzinfo=UTC)
    return datetime.combine(since, datetime.min.time(), tzinfo=UTC)


def _freshness_for_since(since: date, today: date) -> str:
    days = max(0, (today - since).days)
    if days <= 1:
        return "pd"
    if days <= 7:
        return "pw"
    if days <= 31:
        return "pm"
    return "py"
