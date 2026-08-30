from __future__ import annotations

import pytest

from akaton.domain.enums import LocationType
from akaton.processing.deterministic import extract_location, is_philippine_host


@pytest.mark.parametrize(
    ("text", "url"),
    [
        # "Philippine" is the singular form and is not a substring of "philippines".
        ("The Philippine Space Agency announces a challenge.", None),
        ("Hackathon sa Pilipinas para sa mga estudyante.", None),
        ("Open to all Filipinos nationwide in the Philippines.", None),
        # A .ph host identifies the country even when the text never names it.
        ("Municipality of Casiguran, Aurora logo making competition.", "https://x.gov.ph/a"),
        ("Campus innovation challenge for enrolled students.", "https://wpu.edu.ph/news"),
    ],
)
def test_philippine_pages_are_located_in_ph(text, url):
    assert extract_location(text, url).country == "PH"


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("A hackathon in Berlin for German students.", "https://example.de/x"),
        ("Ideathon hosted by the University of Utah.", "https://eccles.utah.edu/x"),
        ("Quantum computing hackathon in Jordan.", "https://indico.sesame.org.jo/event/53"),
    ],
)
def test_foreign_pages_are_not_located_in_ph(text, url):
    assert extract_location(text, url).country is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://philsa.gov.ph/news", True),
        ("https://wpu.edu.ph/home", True),
        ("https://aifest.ph/", True),
        ("https://example.com/ph", False),
        ("https://example.de/x", False),
        (None, False),
    ],
)
def test_philippine_host_detection(url, expected):
    assert is_philippine_host(url) is expected


def test_city_alias_still_wins_for_region_detail():
    location = extract_location("The hackathon runs in BGC this October.", None)
    assert location.country == "PH"
    assert location.city == "Taguig"
    assert location.region == "Metro Manila"
    assert location.location_type is LocationType.ONSITE
