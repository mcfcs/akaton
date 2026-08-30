from __future__ import annotations

from datetime import UTC, datetime

from akaton.domain.enums import LocationType
from akaton.domain.models import DocumentContext
from akaton.processing.deterministic import extract_deterministically, extract_location
from akaton.processing.verifier import verify_event

NOW = datetime(2026, 8, 15, tzinfo=UTC)

# Modelled on gcash.com/imagnation: a Metro Manila competition whose page also advertises
# online rounds. Before the fix the word "online" erased the venue and the event was
# rejected as an international one.
# Deliberately never states who may enter, matching the real page: the eligibility
# extractor leaves philippines_allowed unset, so the gate has to infer it from location.
IMAGNATION = (
    "GCash launches ImaGnation 2026, an innovation and business case competition. "
    "Registration deadline September 30, 2026. The online elimination round runs before "
    "the grand finals in Bonifacio Global City, Taguig. Teams of five compete for prizes. "
) * 5


def test_named_venue_with_online_rounds_is_hybrid_not_online():
    location = extract_location(IMAGNATION, "https://gcash.com/imagnation")
    assert location.country == "PH"
    assert location.city == "Taguig"
    assert location.location_type is LocationType.HYBRID


def test_purely_online_wording_still_reads_as_online():
    location = extract_location("A fully online virtual hackathon, open worldwide.", None)
    assert location.location_type is LocationType.ONLINE


def test_philippine_event_is_eligible_without_an_explicit_eligibility_sentence(config):
    """A local event need not announce that Filipinos may enter it."""
    extraction = extract_deterministically(
        DocumentContext(
            url="https://gcash.com/imagnation",
            title="ImaGnation 2026",
            text=IMAGNATION,
            links=["https://forms.gle/imagnation"],
        ),
        now=NOW,
    )
    assert extraction.facts.eligibility.philippines_allowed is not True
    decision = verify_event(extraction, config.profile, source_authority=85, now=NOW)
    assert decision.gate_results["philippines_eligible"] is True
    assert decision.accepted is True


def test_foreign_online_event_still_needs_explicit_philippine_eligibility(config):
    extraction = extract_deterministically(
        DocumentContext(
            url="https://example.de/hack",
            title="Berlin Online Hackathon 2026",
            text=(
                "A fully online hackathon run from Berlin for European students. "
                "Registration is now open. Registration deadline September 30, 2026. " * 5
            ),
        ),
        now=NOW,
    )
    decision = verify_event(extraction, config.profile, source_authority=85, now=NOW)
    assert decision.gate_results["philippines_eligible"] is False
    assert decision.accepted is False


def test_explicit_exclusion_still_rejects_a_philippine_page(config):
    extraction = extract_deterministically(
        DocumentContext(
            url="https://example.ph/hack",
            title="Regional Hackathon 2026",
            text=(
                "Hackathon in Makati. Registration is now open. Registration deadline "
                "September 30, 2026. Residents of Philippines are not eligible to join. " * 5
            ),
        ),
        now=NOW,
    )
    decision = verify_event(extraction, config.profile, source_authority=85, now=NOW)
    assert decision.gate_results["philippines_eligible"] is False


def test_gcash_domain_clears_the_authority_gate(config):
    from akaton.processing.authority import authority_for_url

    assert authority_for_url("https://gcash.com/imagnation", config.sources) >= 60
