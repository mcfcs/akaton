from __future__ import annotations

import httpx
from sqlalchemy import select

from akaton.discovery.base import SearchRequest
from akaton.discovery.searxng import SearXNGSearchProvider
from akaton.jobs.discovery import DiscoveryJob
from akaton.persistence.database import Database
from akaton.persistence.models import SearchRunRow

# SearXNG answers HTTP 200 with an empty result list when every upstream engine has
# throttled it, which is exactly what a genuinely quiet week looks like.
THROTTLED = {
    "results": [],
    "unresponsive_engines": [
        ["brave", "Suspended: too many requests"],
        ["startpage", "Suspended: CAPTCHA"],
        ["duckduckgo", "timeout"],
    ],
}


async def test_provider_reports_unresponsive_engines():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=THROTTLED))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = SearXNGSearchProvider("http://127.0.0.1:8888", client=client)
        page = await provider.search(SearchRequest(query="hackathon PH"))
    assert page.results == []
    assert "brave: Suspended: too many requests" in page.unresponsive_engines


async def test_healthy_empty_result_is_not_reported_as_an_engine_failure():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = SearXNGSearchProvider("http://127.0.0.1:8888", client=client)
        page = await provider.search(SearchRequest(query="hackathon PH"))
    assert page.unresponsive_engines == []


class ThrottledProvider:
    name = "searxng"

    async def search(self, request):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=THROTTLED))
        async with httpx.AsyncClient(transport=transport) as client:
            return await SearXNGSearchProvider("http://x:8888", client=client).search(request)


class UnusedPipeline:
    async def process(self, seed, *, historical_test: bool = False):  # pragma: no cover
        raise AssertionError("no candidates should exist")


async def test_throttled_search_is_recorded_as_failed_not_as_a_quiet_run(config):
    from dataclasses import replace

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    fast = replace(config, app=config.app.model_copy(update={"search_interval_seconds": 0}))
    job = DiscoveryJob(database, fast, ThrottledProvider(), UnusedPipeline())

    counts = await job.run(query_limit=2)

    assert counts["queries"] == 2
    assert counts["candidates"] == 0
    assert counts["errors"] == 2, "a dead search backend must not look like a successful run"
    async with database.session() as session:
        rows = list((await session.scalars(select(SearchRunRow))).all())
    assert rows and all(row.status == "FAILED" for row in rows)
    assert all("Suspended" in (row.error or "") for row in rows)
    await database.close()
