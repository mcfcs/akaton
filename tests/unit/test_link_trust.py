from __future__ import annotations

import pytest

from akaton.domain.enums import LinkTrust
from akaton.processing.links import is_shortener, is_trusted_registration_url, link_trust


@pytest.fixture
def sources(config):
    return config.sources


@pytest.mark.parametrize(
    "url",
    [
        "https://luma.com/ca1gbye7",
        "https://dict.gov.ph/hack4gov-2026",
        "https://wpu.edu.ph/idea-pitch",
        "https://devpost.com/hack4gov-2026",
        "https://forms.office.com/r/X17Bah5BPz",
        "https://forms.gle/abc123",
        "https://www.facebook.com/groups/philhacks/permalink/4125912344210844/",
    ],
)
def test_trusted_hosts_are_clickable(url, sources):
    assert link_trust(url, sources) is LinkTrust.CLICKABLE


@pytest.mark.parametrize(
    "url",
    [
        # Every shortener in the real philhacks scrape.
        "https://bit.ly/tcris2026",
        "https://jollibee.onelink.me/U65H/ppu7ow43",
        "https://sk-qr.com/hack26/",
        "https://linktr.ee/someorg",
        # Unknown host: shown so it is not hidden, but never endorsed.
        "https://hackathon.plan-ai.net",
        "https://some-random-site.example/event",
    ],
)
def test_untrusted_hosts_are_plain_not_clickable(url, sources):
    assert link_trust(url, sources) is LinkTrust.PLAIN


@pytest.mark.parametrize(
    "url",
    [
        "https://accountscenter.facebook.com/password_and_security",
        "https://www.facebook.com/login/",
        "https://www.facebook.com/notifications",
        "https://www.facebook.com/marketplace/item/123",
        "https://scontent.fbcdn.net/v/t39.jpg",
        "https://www.instagram.com/someorg",
        "https://www.youtube.com/watch?v=abc",
        "mailto:someone@example.com",
        "",
    ],
)
def test_chrome_and_opaque_links_are_dropped(url, sources):
    assert link_trust(url, sources) is LinkTrust.DROP


def test_a_facebook_event_page_is_still_the_event(sources):
    assert link_trust("https://www.facebook.com/events/123456789/", sources) is LinkTrust.CLICKABLE


def test_registration_path_on_a_hostile_host_is_not_trusted(sources):
    """is_registration_url matches /register on ANY host; the host must be checked too."""
    hostile = "https://evil.example/register"
    assert is_trusted_registration_url(hostile, sources) is False
    assert link_trust(hostile, sources) is LinkTrust.PLAIN


def test_registration_path_on_a_trusted_host_is_trusted(sources):
    assert is_trusted_registration_url("https://devpost.com/register", sources) is True


def test_a_shortener_is_never_a_registration_url(sources):
    # bit.ly/tcris2026 did point at a real conference sign-up, but that is unknowable
    # without following it, so it must not become a one-click Register button.
    assert is_shortener("https://bit.ly/tcris2026") is True
    assert is_trusted_registration_url("https://bit.ly/register", sources) is False


def test_trust_survives_without_a_sources_config():
    assert link_trust("https://forms.gle/abc", None) is LinkTrust.CLICKABLE
    assert link_trust("https://evil.example/register", None) is LinkTrust.PLAIN
