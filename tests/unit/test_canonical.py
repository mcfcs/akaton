from __future__ import annotations

from akaton.processing.canonical import choose_urls

FOOTER_LINKS = [
    "https://www.facebook.com/SomeOrganizer",
    "https://www.instagram.com/someorganizer",
    "https://www.linkedin.com/company/someorganizer",
]


def test_social_footer_links_never_become_the_canonical_url():
    """Social domains outrank an unknown site on authority, so scoring page links
    would replace the event page with whatever profile sits in the footer."""
    canonical, registration = choose_urls(
        "https://buildovernights.com/hackathon-2026",
        "https://buildovernights.com/hackathon-2026",
        FOOTER_LINKS,
        {},
    )
    assert canonical == "https://buildovernights.com/hackathon-2026"
    assert registration is None


def test_registration_link_is_still_taken_from_page_links():
    canonical, registration = choose_urls(
        "https://buildovernights.com/hackathon-2026",
        None,
        [*FOOTER_LINKS, "https://forms.gle/abc123"],
        {},
    )
    assert canonical == "https://buildovernights.com/hackathon-2026"
    assert registration == "https://forms.gle/abc123"


def test_same_site_declared_canonical_is_honoured():
    canonical, _ = choose_urls(
        "https://www.example.ph/events/hack?utm_source=fb",
        "https://www.example.ph/events/hack",
        [],
        {"canonical": "https://example.ph/events/hackathon-2026"},
    )
    assert canonical == "https://example.ph/events/hackathon-2026"


def test_offsite_declared_canonical_is_ignored():
    canonical, _ = choose_urls(
        "https://aggregator.test/listing/42",
        "https://aggregator.test/listing/42",
        [],
        {"og:url": "https://www.facebook.com/SomeOrganizer"},
    )
    assert canonical == "https://aggregator.test/listing/42"


def test_site_root_canonical_does_not_swallow_a_deep_page():
    """gcash.com/imagnation declares www.new.gcash.com/ as canonical for every page."""
    canonical, _ = choose_urls(
        "https://gcash.com/imagnation",
        "https://gcash.com/imagnation",
        [],
        {"canonical": "https://www.new.gcash.com/"},
    )
    assert canonical == "https://gcash.com/imagnation"


def test_root_canonical_is_kept_when_the_page_itself_is_the_root():
    canonical, _ = choose_urls(
        "https://aifest.ph",
        "https://aifest.ph",
        [],
        {"canonical": "https://www.aifest.ph/"},
    )
    assert canonical == "https://www.aifest.ph/"


def test_redirect_target_wins_over_the_requested_url():
    canonical, _ = choose_urls(
        "https://example.ph/old-link",
        "https://example.ph/events/hackathon-2026",
        [],
        {},
    )
    assert canonical == "https://example.ph/events/hackathon-2026"
