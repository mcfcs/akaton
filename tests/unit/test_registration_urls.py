from __future__ import annotations

import pytest

from akaton.processing.authority import authority_for_url
from akaton.processing.normalize import is_registration_url


@pytest.mark.parametrize(
    "url",
    [
        "https://forms.gle/abc123",
        "https://docs.google.com/forms/d/e/1/viewform",
        "https://www.eventbrite.com/e/manila-hackathon-tickets-123456",
        "https://devpost.com/register",
        "https://example.ph/apply",
        "https://lu.ma/manila-hack",
    ],
)
def test_registration_links_are_recognised(url):
    assert is_registration_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # Eventbrite discovery pages list many unrelated events and register you for none.
        "https://www.eventbrite.com/d/ca--sunnyvale/feels-12",
        "https://www.eventbrite.com/d/philippines--manila/hackathon",
        "https://www.eventbrite.com/",
        "https://example.ph/events/hackathon-2026",
    ],
)
def test_listing_pages_are_not_registration_links(url):
    assert is_registration_url(url) is False


def test_restricted_philippine_tlds_are_authoritative(config):
    """Only PH government agencies and accredited schools can hold these domains."""
    sources = config.sources
    assert authority_for_url("https://philsa.gov.ph/news/x", sources) >= 60
    assert authority_for_url("https://wpu.edu.ph/home/2026/08/03/x", sources) >= 60


def test_listed_organizer_keeps_its_own_authority(config):
    assert authority_for_url("https://dict.gov.ph/news/x", config.sources) == 90


def test_unrelated_foreign_site_stays_third_party(config):
    assert authority_for_url("https://famt.ac.in/news/ideathon", config.sources) == 50
