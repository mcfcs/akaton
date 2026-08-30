from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akaton.domain.enums import DocumentKind
from akaton.domain.models import DocumentContext
from akaton.processing.deterministic import extract_deterministically
from akaton.processing.normalize import is_listing_url
from akaton.processing.verifier import verify_event

NOW = datetime(2026, 8, 30, tzinfo=UTC)

# A city directory names every category and country it lists, which is exactly what the
# deterministic extractor keys on.
DIRECTORY_TEXT = (
    "Discover events and activities in Sunnyvale, CA. Food festival, hackathon, "
    "business networking, Filipino community night, Philippines independence party. "
    "Registration is now open for selected events. " * 6
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.eventbrite.com/d/ca--sunnyvale/feels-12",
        "https://www.eventbrite.com/discover/things-to-do",
        "https://example.com/search?q=hackathon",
        "https://www.thecleanzine.com/sitemap.php",
        "https://example.ph/tag/hackathon/",
        "https://devpost.com/hackathons/",
    ],
)
def test_directory_urls_are_detected(url):
    assert is_listing_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.eventbrite.com/e/manila-hackathon-tickets-123456",
        "https://buildovernights.com/hackathon-2026",
        "https://dict.gov.ph/egov-hackathon-2026",
        "https://lu.ma/manila-hack",
    ],
)
def test_single_event_pages_are_not_directories(url):
    assert is_listing_url(url) is False


def test_directory_page_is_not_accepted_as_an_event(config):
    extraction = extract_deterministically(
        DocumentContext(
            url="https://www.eventbrite.com/d/ca--sunnyvale/feels-12",
            title="Discover Feels 12 Events & Activities in Sunnyvale, CA | Eventbrite",
            text=DIRECTORY_TEXT,
            links=["https://www.eventbrite.com/e/some-event-tickets-99"],
        ),
        now=NOW,
    )
    assert extraction.facts.document_kind is DocumentKind.DIRECTORY
    decision = verify_event(extraction, config.profile, source_authority=80, now=NOW)
    assert decision.accepted is False
    assert decision.gate_results["actionable_document"] is False


def test_equivalent_single_event_page_still_passes(config):
    """The directory rule must key on the URL shape, not on the wording."""
    extraction = extract_deterministically(
        DocumentContext(
            url="https://www.eventbrite.com/e/manila-hackathon-tickets-123456",
            title="Manila Hackathon 2026",
            text=(
                "Registration is now open to university students nationwide in the "
                "Philippines. Registration deadline October 5, 2026. Event date "
                "October 20, 2026 in Makati. Build AI software in this hackathon. " * 6
            ),
            links=["https://forms.gle/manila-hack"],
        ),
        now=NOW,
    )
    assert extraction.facts.document_kind is not DocumentKind.DIRECTORY
    decision = verify_event(extraction, config.profile, source_authority=80, now=NOW)
    assert decision.accepted is True
