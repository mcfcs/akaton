from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from akaton.config import ConfigBundle
from akaton.discovery.base import SearchProvider, SearchRequest, SourceAdapter
from akaton.discovery.queries import choose_due_queries, configured_queries, organizer_queries
from akaton.persistence.database import Database
from akaton.persistence.repository import Repository
from akaton.pipeline import CandidatePipeline

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

    async def run(self) -> dict[str, int]:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self.database.session() as session:
            repo = Repository(session)
            used = await repo.monthly_search_requests(self.provider.name, month_start)
            history = await repo.search_history(self.provider.name)
        budget_remaining = self.config.app.monthly_search_budget - used
        query_count = min(self.config.app.discovery_queries_per_run, max(0, budget_remaining))
        all_queries = [
            *configured_queries(self.config.queries),
            *organizer_queries(self.config.sources),
        ]
        selected = choose_due_queries(all_queries, history, query_count, now=now)
        counts = {"queries": 0, "candidates": 0, "processed": 0, "errors": 0}
        for item in selected:
            try:
                page = await self.provider.search(
                    SearchRequest(query=item.query, freshness=item.freshness)
                )
                error = None
            except httpx.HTTPStatusError as exc:
                page = None
                error = f"HTTP {exc.response.status_code}"
            except Exception as exc:
                page = None
                error = type(exc).__name__
            async with self.database.session() as session:
                await Repository(session).record_search_run(
                    self.provider.name,
                    item.group,
                    item.query,
                    len(page.results) if page else 0,
                    error,
                )
            counts["queries"] += 1
            if error:
                counts["errors"] += 1
                continue
            for seed in page.results:
                counts["candidates"] += 1
                try:
                    await self.pipeline.process(seed)
                    counts["processed"] += 1
                except Exception:
                    counts["errors"] += 1
                    logger.exception("candidate_pipeline_failed", extra={"url": str(seed.url)})

        for adapter in self.adapters:
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
            for seed in seeds:
                counts["candidates"] += 1
                try:
                    await self.pipeline.process(seed)
                    counts["processed"] += 1
                except Exception:
                    counts["errors"] += 1
                    logger.exception("candidate_pipeline_failed", extra={"url": str(seed.url)})
        return counts
