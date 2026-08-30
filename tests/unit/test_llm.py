from __future__ import annotations

import json

import httpx
import pytest

from akaton.domain.models import DocumentContext, EventFacts, Evidence, ExtractionEnvelope
from akaton.processing.llm import OllamaLLMProvider, validate_llm_evidence


def test_llm_evidence_must_exist_in_source():
    context = DocumentContext(url="https://example.com", text="Registration closes October 5, 2026")
    extraction = ExtractionEnvelope(
        facts=EventFacts(title="Example"),
        evidence=[
            Evidence(
                field_name="registration_deadline",
                value="2026-10-05",
                quote="Invented quote",
                confidence=0.9,
            )
        ],
        overall_confidence=0.9,
    )
    with pytest.raises(ValueError):
        validate_llm_evidence(extraction, context)


async def test_ollama_provider_uses_local_structured_output():
    context = DocumentContext(
        url="https://example.com/event",
        title="Manila Hackathon 2026",
        text="Registration is open for students in the Philippines.",
    )
    extraction = ExtractionEnvelope(
        facts=EventFacts(title="Manila Hackathon 2026"),
        overall_confidence=0.9,
        extraction_version="ollama-test",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert str(request.url) == "http://100.102.10.69:11434/api/chat"
        assert payload["model"] == "qwen3.5:27b"
        assert payload["stream"] is False
        assert payload["think"] is False
        assert payload["format"]["type"] == "object"
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": extraction.model_dump_json()}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OllamaLLMProvider("http://100.102.10.69:11434", "qwen3.5:27b", client=client)
        result = await provider.extract(context)
    assert result.facts.title == "Manila Hackathon 2026"
