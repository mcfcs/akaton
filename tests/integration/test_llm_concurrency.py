from __future__ import annotations

import asyncio

from akaton.domain.models import CandidateSeed, DocumentContext, ExtractionEnvelope, FetchResult
from akaton.persistence.database import Database
from akaton.pipeline import CandidatePipeline
from akaton.processing.deterministic import extract_deterministically

SPARSE = "A competition is being organised. Details to follow. " * 12


class SparseFetcher:
    async def fetch(self, url, **kwargs):
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="Some competition",
            text=SPARSE,
            content_hash=url,
            usable=True,
        )


class ConcurrencyRecordingLLM:
    name = "recorder"

    def __init__(self) -> None:
        self.peak = 0
        self.calls = 0
        self._active = 0

    async def extract(self, context: DocumentContext) -> ExtractionEnvelope:
        self.calls += 1
        self._active += 1
        self.peak = max(self.peak, self._active)
        try:
            await asyncio.sleep(0.05)
            return extract_deterministically(context)
        finally:
            self._active -= 1


async def test_extractions_do_not_run_more_than_llm_concurrency_at_once(config):
    """Ollama serialises per model; firing six at once just queues them into timeouts."""
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    llm = ConcurrencyRecordingLLM()
    pipeline = CandidatePipeline(database, config, SparseFetcher(), llm=llm)

    seeds = [
        CandidateSeed(
            url=f"https://example{index}.ph/events/thing",
            discovery_channel="search",
            provider="fake",
        )
        for index in range(8)
    ]
    await asyncio.gather(*(pipeline.process(seed) for seed in seeds))

    assert llm.calls == 8
    assert llm.peak <= config.app.llm_concurrency
    await database.close()
