from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akaton.domain.models import DocumentContext
from akaton.processing.deterministic import extract_deterministically
from akaton.processing.scorer import score_event
from akaton.processing.verifier import verify_event


@pytest.mark.parametrize("case_index", range(15))
def test_decision_fixtures(event_cases, config, case_index):
    case = event_cases[case_index]
    context = DocumentContext(
        url="https://ateneo.edu/events/example",
        title=case["title"],
        text=case["text"],
        links=[case["link"]] if case.get("link") else [],
    )
    extraction = extract_deterministically(
        context,
        now=datetime(2026, 8, 30, tzinfo=UTC),
        published=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert extraction.facts.category.value == case["category"]
    assert extraction.facts.document_kind.value == case["kind"]
    decision = verify_event(
        extraction,
        config.profile,
        source_authority=90,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert decision.accepted is case["accepted"], (case["id"], decision.model_dump())
    if decision.accepted:
        score = score_event(
            extraction.facts,
            config.profile,
            config.scoring,
            source_authority=90,
            now=datetime(2026, 8, 30, tzinfo=UTC),
        )
        assert score.total >= 65


def test_yearless_date_is_marked_inferred():
    extraction = extract_deterministically(
        DocumentContext(
            url="https://example.com/event",
            title="Hackathon",
            text=(
                "Registration deadline October 5. Registration is now open in Manila, Philippines."
            ),
        ),
        published=datetime(2026, 8, 30, tzinfo=UTC),
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert extraction.facts.registration_deadline.year_inferred is True
    assert extraction.facts.registration_deadline.value.year == 2026


def test_all_requested_scenarios_have_fixtures(event_cases):
    assert len(event_cases) == 26


def test_historical_mode_only_relaxes_time_and_registration_gates(event_cases, config):
    case = event_cases[6]
    extraction = extract_deterministically(
        DocumentContext(
            url="https://ateneo.edu/events/historical-test",
            title=case["title"],
            text=case["text"],
        ),
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )
    normal = verify_event(
        extraction,
        config.profile,
        source_authority=90,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )
    historical = verify_event(
        extraction,
        config.profile,
        source_authority=90,
        now=datetime(2026, 8, 30, tzinfo=UTC),
        allow_historical=True,
    )
    assert normal.accepted is False
    assert historical.accepted is True
    assert historical.gate_results["future"] is True
    assert historical.gate_results["registration"] is True
