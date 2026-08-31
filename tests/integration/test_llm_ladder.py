"""Two model hosts: a small one for every day, a bigger one only when needed.

The everyday host runs a model that fits in 8GB of VRAM. The fallback is a shared box
with more, where a model load alone was measured at 5.8, 16.1 and 39.9 seconds — so it is
worth asking only when the small model left the extraction thin, and never more than a
few times a run.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from akaton.domain.enums import CompetitionCategory, DocumentKind
from akaton.domain.models import (
    CandidateSeed,
    EventFacts,
    ExtractionEnvelope,
    FetchResult,
)
from akaton.persistence.database import Database
from akaton.persistence.models import CandidateRow
from akaton.pipeline import CandidatePipeline
from akaton.processing.llm import OllamaLLMProvider, should_escalate

THIN = "Hackathon. Details to follow."
RICH = (
    "Registration is now open to university students nationwide in the Philippines. "
    "Registration deadline October 5, 2026. Event date October 20, 2026 at the DICT "
    "office in Manila. Build AI and software solutions in this hackathon. "
) * 8


class Fetcher:
    def __init__(self, text: str) -> None:
        self.text = text

    async def fetch(self, url, **kwargs):
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="Manila Hackathon 2026",
            text=self.text,
            content_hash=str(hash(self.text)),
            usable=True,
        )


class StubModel:
    """A host that answers with a fixed envelope, or refuses to be reached."""

    def __init__(
        self, name: str, *, facts: EventFacts | None = None, error: Exception | None = None
    ):
        self.name = name
        self.facts = facts
        self.error = error
        self.calls = 0

    async def extract(self, context):
        self.calls += 1
        if self.error:
            raise self.error
        return ExtractionEnvelope(
            facts=self.facts or EventFacts(),
            overall_confidence=0.9,
            extraction_version=self.name,
        )


@pytest.fixture
async def database():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.close()


def _pipeline(database, config, providers, text=THIN, **app):
    tuned = replace(config, app=config.app.model_copy(update=app)) if app else config
    return CandidatePipeline(database, tuned, Fetcher(text), llm_providers=providers)


def _seed(url="https://dict.gov.ph/manila-hackathon-2026"):
    return CandidateSeed(url=url, discovery_channel="search", provider="fake")


class TestEscalationRule:
    def test_a_confident_extraction_is_not_escalated(self):
        envelope = ExtractionEnvelope(
            facts=EventFacts(title="x"), overall_confidence=0.9, extraction_version="t"
        )
        assert not should_escalate(envelope)

    def test_a_thin_extraction_is(self):
        envelope = ExtractionEnvelope(
            facts=EventFacts(), overall_confidence=0.4, extraction_version="t"
        )
        assert should_escalate(envelope)

    def test_a_surviving_critical_ambiguity_is_enough_on_its_own(self):
        """Confidence can look fine while the title is still missing."""
        envelope = ExtractionEnvelope(
            facts=EventFacts(),
            overall_confidence=0.95,
            ambiguities=["missing_title"],
            extraction_version="t",
        )
        assert should_escalate(envelope)


class TestLadder:
    async def test_a_good_first_answer_never_reaches_the_second_host(self, database, config):
        """The fallback is slow and shared; a clean page must not pay for it."""
        good = EventFacts(
            title="Manila Hackathon 2026",
            category=CompetitionCategory.HACKATHON,
            document_kind=DocumentKind.REGISTRATION_OPEN,
        )
        small = StubModel("small", facts=good)
        big = StubModel("big", facts=good)
        await _pipeline(database, config, [small, big], text=RICH).process(_seed())
        assert small.calls + big.calls <= 1, "a rich page may not need a model at all"
        assert big.calls == 0

    async def test_a_thin_first_answer_escalates(self, database, config):
        small = StubModel("small", facts=EventFacts())
        big = StubModel("big", facts=EventFacts(title="Manila Hackathon 2026"))
        await _pipeline(database, config, [small, big]).process(_seed())
        assert small.calls == 1
        assert big.calls == 1, "the small model left it thin, so the big one was asked"

    async def test_an_unreachable_first_host_fails_over(self, database, config):
        """The sleeping-laptop case: the ladder moves on rather than giving up."""
        small = StubModel("small", error=httpx.ConnectError("nobody home"))
        big = StubModel("big", facts=EventFacts(title="Manila Hackathon 2026"))
        await _pipeline(database, config, [small, big]).process(_seed())
        assert small.calls == 1
        assert big.calls == 1

    async def test_every_host_failing_still_produces_a_candidate(self, database, config):
        """Extraction is deterministic first; the model only ever fills gaps."""
        small = StubModel("small", error=httpx.ConnectError("nobody home"))
        big = StubModel("big", error=RuntimeError("also down"))
        outcome = await _pipeline(database, config, [small, big]).process(_seed())
        assert outcome.candidate_id
        async with database.session() as session:
            row = await session.scalar(select(CandidateRow))
        assert row is not None, "the pipeline kept going without any model"

    async def test_the_escalation_budget_is_spent_once(self, database, config):
        small = StubModel("small", facts=EventFacts())
        big = StubModel("big", facts=EventFacts())
        pipeline = _pipeline(database, config, [small, big], llm_escalations_per_run=1)
        for index in range(3):
            await pipeline.process(_seed(f"https://dict.gov.ph/hackathon-{index}"))
        assert small.calls == 3, "the everyday host is always asked"
        assert big.calls == 1, "the shared host is capped"

    async def test_one_configured_host_still_works(self, database, config):
        small = StubModel("small", facts=EventFacts())
        await _pipeline(database, config, [small]).process(_seed())
        assert small.calls == 1

    async def test_no_configured_host_is_not_an_error(self, database, config):
        outcome = await _pipeline(database, config, []).process(_seed())
        assert outcome.candidate_id

    async def test_the_llm_keyword_is_still_a_one_tier_ladder(self, database, config):
        """Existing callers and fixtures pass `llm=`; that must keep working."""
        small = StubModel("small", facts=EventFacts())
        pipeline = CandidatePipeline(database, config, Fetcher(THIN), llm=small)
        assert pipeline.llm_providers == [small]
        await pipeline.process(_seed())
        assert small.calls == 1


class TestSwitchingHosts:
    """The ladder can be reordered at runtime, so a host can be swapped without a restart."""

    def _client(self, database, config, providers):
        from fastapi.testclient import TestClient

        from akaton.dashboard.runtime import MonitorController
        from akaton.dashboard.web import create_dashboard

        class Scheduler:
            state = 1

            def get_jobs(self):
                return []

        async def noop():
            return {}

        return TestClient(
            create_dashboard(
                database,
                MonitorController(Scheduler(), noop, noop),
                config,
                llm_providers=providers,
            )
        )

    def test_the_ladder_is_reported_in_order(self, database, config):
        providers = [
            OllamaLLMProvider("http://laptop.internal:11434", "dolphin3:8b"),
            OllamaLLMProvider("http://workstation.internal:11434", "qwen2.5vl:7b"),
        ]
        with self._client(database, config, providers) as client:
            tiers = client.get("/api/llm").json()["tiers"]
        assert [t["host"] for t in tiers] == ["laptop.internal:11434", "workstation.internal:11434"]
        assert [t["role"] for t in tiers] == ["primary", "escalation"]

    def test_promoting_a_host_reorders_the_list_the_pipeline_holds(self, database, config):
        """The swap is in place, so the pipeline sees it without a restart.

        The pipeline copies whatever list it is given — that is what normalises the
        one-tier `llm=` shorthand — so the dashboard must be handed `pipeline.llm_providers`
        itself and not the list it was built from. `app.py` wires it that way; passing the
        original list here instead would make this test pass while production silently
        kept using the old order.
        """
        pipeline = CandidatePipeline(
            database,
            config,
            Fetcher(THIN),
            llm_providers=[
                OllamaLLMProvider("http://laptop.internal:11434", "dolphin3:8b"),
                OllamaLLMProvider("http://workstation.internal:11434", "qwen2.5vl:7b"),
            ],
        )
        with self._client(database, config, pipeline.llm_providers) as client:
            response = client.post(
                "/api/actions/llm/primary", json={"host": "workstation.internal:11434"}
            )
            assert response.status_code == 200
            tiers = client.get("/api/llm").json()["tiers"]
        assert tiers[0]["host"] == "workstation.internal:11434"
        assert pipeline.llm_providers[0].model == "qwen2.5vl:7b", "the pipeline sees it too"

    def test_an_unknown_host_is_refused(self, database, config):
        providers = [OllamaLLMProvider("http://laptop.internal:11434", "dolphin3:8b")]
        with self._client(database, config, providers) as client:
            assert client.post("/api/actions/llm/primary", json={"host": "nope"}).status_code == 404

    def test_no_configured_host_reports_an_empty_ladder(self, database, config):
        with self._client(database, config, []) as client:
            assert client.get("/api/llm").json()["tiers"] == []


class TestTimeouts:
    def test_the_connect_timeout_is_far_shorter_than_the_read_timeout(self):
        """A cold model load takes tens of seconds; an absent host should not."""
        provider = OllamaLLMProvider("http://ollama.internal:11434", "dolphin3:8b")
        timeout = provider._timeout()
        assert timeout.connect == 5
        assert timeout.read == 180

    async def test_a_refused_connection_is_not_retried(self):
        """Retrying doubles the wait for an answer that is not coming."""
        attempts = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            raise httpx.ConnectError("refused", request=request)

        from akaton.domain.models import DocumentContext

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = OllamaLLMProvider(
                "http://ollama.internal:11434", "dolphin3:8b", client=client
            )
            with pytest.raises(httpx.ConnectError):
                await provider.extract(DocumentContext(url="https://x.test", text="hello"))
        assert attempts["n"] == 1, "one attempt, not two"
