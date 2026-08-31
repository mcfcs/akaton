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
        ["duckduckgo web", "timeout"],
        ["bing", "Suspended: too many requests"],
        ["google", "Suspended: CAPTCHA"],
        ["google cse", "Suspended: CAPTCHA"],
        ["mojeek", "timeout"],
        ["qwant", "Suspended: too many requests"],
    ],
}

# The everyday shape: a few engines suspended while the rest answer. An empty result set
# here is a genuine absence of matches, not a broken backend.
PARTIAL = {
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
    assert page.degraded is False


async def test_some_engines_down_is_not_a_degraded_search():
    """The common case: six engines suspended and the rest answering normally.

    Calling this a failure is what made 28 of 33 recorded searches look broken — a
    `site:` query with no matches is an empty result, not an unreachable backend.
    """
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=PARTIAL))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = SearXNGSearchProvider("http://127.0.0.1:8888", client=client)
        page = await provider.search(SearchRequest(query="site:gcash.com case competition"))
    assert page.unresponsive_engines, "the suspended engines are still reported"
    assert page.degraded is False


async def test_every_answering_engine_down_is_degraded():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=THROTTLED))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = SearXNGSearchProvider("http://127.0.0.1:8888", client=client)
        page = await provider.search(SearchRequest(query="hackathon PH"))
    assert page.degraded is True


class ThrottledProvider:
    name = "searxng"

    async def search(self, request):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=THROTTLED))
        async with httpx.AsyncClient(transport=transport) as client:
            return await SearXNGSearchProvider("http://x:8888", client=client).search(request)


class PartiallyThrottledProvider:
    name = "searxng"

    async def search(self, request):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=PARTIAL))
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


async def test_a_failed_query_does_not_claim_a_cadence_slot(config):
    """A query that never got an answer must be eligible again on the next run."""
    from akaton.persistence.repository import Repository

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    async with database.session() as session:
        repo = Repository(session)
        await repo.record_search_run("searxng", "hot", "eGov hackathon", 0, "HTTP 429")
        await repo.record_search_run("searxng", "hot", "DICT hackathon", 4, None)
    async with database.session() as session:
        history = await Repository(session).search_history("searxng")

    assert ("hot", "DICT hackathon") in history
    assert ("hot", "eGov hackathon") not in history
    await database.close()


async def test_a_second_run_can_read_the_first_run_s_history(config):
    """SQLite stores no timezone, so history came back naive and the rotation raised.

    `choose_due_queries` compares each timestamp against an aware `now`, so once any
    search had been recorded every later scheduled run died with a TypeError before
    issuing a single query. The adapter cadence check reads the same map.
    """
    from dataclasses import replace

    from akaton.persistence.repository import Repository

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    async with database.session() as session:
        await Repository(session).record_search_run("searxng", "hot", "hackathon PH", 3, None)
    async with database.session() as session:
        history = await Repository(session).search_history("searxng")

    assert history, "the successful run is recorded"
    for stamp in history.values():
        assert stamp.tzinfo is not None, "a naive timestamp cannot be compared to now()"

    fast = replace(config, app=config.app.model_copy(update={"search_interval_seconds": 0}))
    job = DiscoveryJob(database, fast, PartiallyThrottledProvider(), UnusedPipeline())
    counts = await job.run(query_limit=2)
    assert counts["queries"] == 2
    await database.close()


async def test_a_query_with_no_matches_is_recorded_as_a_successful_run(config):
    """Zero results while engines are still answering is an answer, not an outage."""
    from dataclasses import replace

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    fast = replace(config, app=config.app.model_copy(update={"search_interval_seconds": 0}))
    job = DiscoveryJob(database, fast, PartiallyThrottledProvider(), UnusedPipeline())

    counts = await job.run(query_limit=2)

    assert counts["queries"] == 2
    assert counts["errors"] == 0
    async with database.session() as session:
        rows = list((await session.scalars(select(SearchRunRow))).all())
    assert rows and all(row.status == "SUCCEEDED" for row in rows)
    assert all(row.error is None for row in rows)
    await database.close()
