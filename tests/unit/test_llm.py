from __future__ import annotations

import pytest

from akaton.domain.models import DocumentContext, EventFacts, Evidence, ExtractionEnvelope
from akaton.processing.llm import validate_llm_evidence


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
