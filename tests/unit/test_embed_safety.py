from __future__ import annotations

from datetime import UTC, datetime

from akaton.discord.embeds import (
    SOCIAL_COLOR,
    build_new_event_payload,
    embed_dict,
    render_links,
)
from akaton.domain.enums import CompetitionCategory
from akaton.domain.models import EventFacts, NotificationPayload, ScoringResult

NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _payload(**overrides) -> NotificationPayload:
    data = dict(
        dedupe_key="k",
        notification_type="NEW_EVENT",
        event_id=1,
        event_version=1,
        title="ImaGnation 2026",
        description="A business case competition.",
        fields={"Category": "Business Case"},
        footer_token="tok",
        relevance_tier="RECOMMENDED",
        confidence_label="High",
    )
    data.update(overrides)
    return NotificationPayload(**data)


def _score() -> ScoringResult:
    return ScoringResult(total=79, tier="RECOMMENDED", components={}, match_reasons=["Taguig"])


def test_markdown_in_scraped_text_is_neutralised():
    """A post can contain a markdown link that would render as if we had written it.

    Both brackets have to be escaped. `escape_markdown` on its own escapes only the
    opening one, and Discord still renders `\\[Click here](url)` as a working link — so
    the closing bracket is escaped too, breaking the syntax on both sides and leaving the
    URL visible as text where the reader can see where it actually points.
    """
    embed = embed_dict(
        _payload(
            title="[Click here](https://evil.example) Hackathon",
            description="Free prizes [claim now](https://evil.example)",
            fields={"Organizer": "**Totally Legit** [org](https://evil.example)"},
        )
    )
    assert embed["title"].startswith("\\[Click here\\]")
    assert "\\[claim now\\]" in embed["description"]
    value = embed["fields"][0]["value"]
    assert "\\[org\\]" in value
    # No bracket anywhere is left able to close a link.
    for text in (embed["title"], embed["description"], value):
        assert text.count("]") == text.count("\\]")
    # Bold and other emphasis is defused too, so scraped text cannot shout.
    assert "**Totally Legit**" not in value


def test_an_untrusted_official_url_does_not_make_the_title_clickable():
    embed = embed_dict(
        _payload(official_url="https://evil.example/event", official_url_clickable=False)
    )
    assert "url" not in embed


def test_untrusted_links_are_backticked_never_linkified(config):
    field = render_links(
        [
            "https://bit.ly/tcris2026",
            "https://sk-qr.com/hack26/",
            "https://luma.com/ca1gbye7",
            "https://accountscenter.facebook.com/x",
        ],
        config.sources,
    )
    assert "`https://bit.ly/tcris2026` (shortened link)" in field
    assert "`https://sk-qr.com/hack26/` (shortened link)" in field
    assert "[luma.com](https://luma.com/ca1gbye7)" in field
    # Platform chrome is the only thing removed outright.
    assert "accountscenter" not in field


def test_a_social_alert_is_visually_distinct_and_names_its_source(config):
    facts = EventFacts(
        title="Shopee AI Hackathon 2026",
        category=CompetitionCategory.HACKATHON,
        description="Applications are now open for students.",
        canonical_url="https://www.facebook.com/groups/philhacks/permalink/123/",
    )
    payload = build_new_event_payload(
        1,
        1,
        facts,
        _score(),
        0.83,
        discovery_channel="facebook",
        source_label="Facebook group · philhacks",
        links=["https://forms.gle/abc"],
        published=NOW,
        sources=config.sources,
    )
    assert payload.source_kind == "social_post"
    embed = embed_dict(payload)
    assert embed["color"] == SOCIAL_COLOR
    names = {field["name"] for field in embed["fields"]}
    assert "Source" in names
    assert "Posted" in names


def test_a_social_alert_without_a_registration_link_says_so_instead_of_faking_one(config):
    facts = EventFacts(
        title="Some hackathon",
        category=CompetitionCategory.HACKATHON,
        description="Join us.",
        canonical_url="https://www.facebook.com/groups/philhacks/permalink/123/",
    )
    payload = build_new_event_payload(
        1, 1, facts, _score(), 0.83, discovery_channel="facebook", sources=config.sources
    )
    assert payload.registration_url is None
    assert payload.evidence_note and "No registration link" in payload.evidence_note
    embed = embed_dict(payload)
    # There must be no Register button pointing at the post itself.
    assert not any("[Register]" in field["value"] for field in embed["fields"])


def test_an_official_page_keeps_its_clickable_title(config):
    facts = EventFacts(
        title="ImaGnation",
        category=CompetitionCategory.BUSINESS_CASE,
        description="Registration is now open.",
        canonical_url="https://gcash.com/imagnation",
    )
    payload = build_new_event_payload(1, 1, facts, _score(), 0.83, sources=config.sources)
    assert payload.source_kind == "official"
    assert embed_dict(payload)["url"] == "https://gcash.com/imagnation"
