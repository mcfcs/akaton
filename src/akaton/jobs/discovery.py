from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

import httpx

from akaton.config import ConfigBundle
from akaton.discovery.base import SearchProvider, SearchRequest, SourceAdapter
from akaton.discovery.queries import choose_due_queries, configured_queries, organizer_queries
from akaton.domain.models import CandidateSeed
from akaton.persistence.database import Database
from akaton.persistence.repository import Repository
from akaton.pipeline import CandidatePipeline
from akaton.processing.normalize import normalize_url

logger = logging.getLogger(__name__)


class DiscoveryJob:
    def __init__(
        self,
        database: Database,
        config: ConfigBundle,
        provider: SearchProvider,
        pipeline: CandidatePipeline,
        adapters: list[SourceAdapter] | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.provider = provider
        self.pipeline = pipeline
        self.adapters = adapters or []

    async def run(
        self,
        *,
        since: date | None = None,
        historical_test: bool = False,
        query_limit: int | None = None,
    ) -> dict[str, int]:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self.database.session() as session:
            repo = Repository(session)
            used = await repo.monthly_search_requests(self.provider.name, month_start)
            history = {} if historical_test else await repo.search_history(self.provider.name)
        budget_remaining = self.config.app.monthly_search_budget - used
        requested = query_limit or self.config.app.discovery_queries_per_run
        query_count = min(requested, max(0, budget_remaining))
        all_queries = [
            *configured_queries(self.config.queries),
            *organizer_queries(self.config.sources),
        ]
        selected = choose_due_queries(all_queries, history, query_count, now=now)
        counts = {"queries": 0, "candidates": 0, "processed": 0, "errors": 0}
        for item in selected:
            query = item.query
            freshness = item.freshness
            if since:
                query = f"{query} after:{since.isoformat()}"
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
            if page is not None and not page.results and page.unresponsive_engines:
                # Record the throttled engines rather than an empty success, so a dead
                # search backend is visible instead of looking like a quiet week.
                error = "no engine responded: " + "; ".join(page.unresponsive_engines)
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
                continue
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

        for adapter in [] if historical_test else self.adapters:
            adapter_settings = self.config.sources.get("structured_sources", {}).get(
                adapter.name, {}
            )
            cadence = int(adapter_settings.get("cadence_hours", 24))
            async with self.database.session() as session:
                adapter_history = await Repository(session).search_history(adapter.name)
            last_adapter_run = adapter_history.get(("structured", adapter.name))
            if last_adapter_run and last_adapter_run + timedelta(hours=cadence) > now:
                continue
            try:
                seeds = await adapter.discover()
                adapter_error = None
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
            if adapter_error:
                continue
            discovered, processed, failed = await self._process_seeds(seeds)
            counts["candidates"] += discovered
            counts["processed"] += processed
            counts["errors"] += failed
        return counts

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
        for seed in seeds:
            unique.setdefault(normalize_url(str(seed.url)), seed)
        seeds = list(unique.values())
        if not seeds:
            return 0, 0, 0
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
        return len(outcomes), processed, len(outcomes) - processed


def _freshness_for_since(since: date, today: date) -> str:
    days = max(0, (today - since).days)
    if days <= 1:
        return "pd"
    if days <= 7:
        return "pw"
    if days <= 31:
        return "pm"
    return "py"
