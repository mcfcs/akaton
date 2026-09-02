"""How the alert is laid out, and what it refuses to show.

The alert used to render every fact as a full-width field, so nine rows — most of them
reading "Not specified" — stacked into one column. This pins the shape that replaced it,
and the trust rules that decide whether an image or a logo may appear at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from akaton.discord.embeds import (
    build_new_event_payload,
    displayable_image,
    embed_dict,
    organizer_for_url,
    organizer_icon,
    summarise_eligibility,
)
from akaton.domain.enums import CompetitionCategory, DocumentKind, LocationType
from akaton.domain.models import (
    DateFact,
    EligibilityFact,
    EventFacts,
    LocationFact,
    ScoringResult,
)

SOURCES = {
    "organizers": [
        {
            "id": "dict",
            "name": "Department of Information and Communications Technology",
            "aliases": ["DICT"],
            "domains": ["dict.gov.ph"],
            "authority": 90,
        },
        {
            "id": "gcash",
            "name": "GCash",
            "aliases": ["GCash"],
            "domains": ["gcash.com"],
            "authority": 85,
            "logo": "https://gcash.com/brand/logo.png",
        },
        {
            "id": "off",
            "name": "Gone",
            "aliases": ["GONE"],
            "domains": ["gone.ph"],
            "enabled": False,
        },
    ],
    "platforms": {"gov.ph": 85},
}

SCORE = ScoringResult(
    total=82, tier="HIGH_PRIORITY", components={}, match_reasons=["preferred city: Manila"]
)


def _facts(**overrides) -> EventFacts:
    facts = EventFacts(
        title="eGov Hackathon 2026",
        category=CompetitionCategory.HACKATHON,
        document_kind=DocumentKind.REGISTRATION_OPEN,
        canonical_url="https://dict.gov.ph/egov-hackathon-2026",
        registration_url="https://forms.gle/egov2026",
        image_url="https://dict.gov.ph/media/banner.jpg",
        event_start=DateFact(value=datetime(2026, 10, 20, tzinfo=UTC), confidence=0.95),
        registration_deadline=DateFact(value=datetime(2026, 10, 5, tzinfo=UTC), confidence=0.95),
        location=LocationFact(country="PH", city="Manila", location_type=LocationType.ONSITE),
        team_size_min=3,
        team_size_max=5,
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def _embed(facts: EventFacts | None = None, **kwargs) -> dict:
    payload = build_new_event_payload(
        1, 1, facts or _facts(), SCORE, 0.9, sources=SOURCES, **kwargs
    )
    return embed_dict(payload)


def _field(embed: dict, name: str) -> dict | None:
    return next((f for f in embed["fields"] if f["name"] == name), None)


class TestLayout:
    def test_the_short_facts_sit_side_by_side(self):
        """Everything was full width, which is what made it a column of one-line rows."""
        embed = _embed()
        inline = [f["name"] for f in embed["fields"] if f["inline"]]
        assert "📅 Event date" in inline
        assert "⏳ Registration closes" in inline
        assert "Location" in inline

    def test_the_first_three_fields_are_when_when_and_where(self):
        """Discord flows inline fields three to a row, so these form the top row."""
        assert [f["name"] for f in _embed()["fields"]][:3] == [
            "📅 Event date",
            "⏳ Registration closes",
            "Location",
        ]

    def test_a_fact_we_do_not_have_is_left_out(self):
        """It used to print "Not specified", nine times over."""
        embed = _embed(_facts(prize_information=None, team_size_min=None, team_size_max=None))
        rendered = "\n".join(f"{f['name']}{f['value']}" for f in embed["fields"])
        assert "Not specified" not in rendered
        assert _field(embed, "Prize") is None
        assert _field(embed, "Team size") is None

    def test_dates_use_discord_timestamp_markup(self):
        """Rendered in the reader's own timezone, with a live countdown."""
        value = _field(_embed(), "📅 Event date")["value"]
        assert value.startswith("<t:") and ":D>" in value and ":R>" in value

    def test_a_long_value_is_never_squeezed_inline(self):
        embed = _embed(_facts(prize_information="PHP 500,000 " * 8))
        assert _field(embed, "Prize")["inline"] is False

    def test_relevance_and_confidence_moved_into_the_footer(self):
        embed = _embed()
        assert _field(embed, "Relevance") is None
        assert _field(embed, "Confidence") is None
        assert "High Priority" in embed["footer"]["text"]
        assert "High confidence" in embed["footer"]["text"]

    def test_the_footer_still_carries_the_reconciliation_token(self):
        """Restarting looks for this string to avoid re-sending an alert."""
        assert "akaton:1:1:new" in _embed()["footer"]["text"]

    def test_a_registration_link_is_not_repeated_under_links_mentioned(self):
        embed = _embed(links=["https://forms.gle/egov2026", "https://dict.gov.ph/faq"])
        assert "forms.gle/egov2026" not in (_field(embed, "Links mentioned") or {}).get("value", "")

    def test_eligibility_is_one_sentence_not_the_page_again(self):
        long_text = (
            "Registration is now open to university students nationwide. "
            "Open to Filipino citizens currently enrolled in a Philippine university. "
            "Teams must have three to five members. " * 4
        )
        summary = summarise_eligibility(long_text)
        assert summary.startswith("Registration is now open")
        assert len(summary) <= 201


class TestOrganizerIdentity:
    def test_the_organizer_is_recognised_from_its_domain(self):
        """A government page rarely introduces itself, so extraction finds no organizer."""
        assert organizer_for_url("https://dict.gov.ph/egov-hackathon-2026", SOURCES) == "DICT"

    def test_a_disabled_organizer_is_not_used(self):
        assert organizer_for_url("https://gone.ph/x", SOURCES) is None

    def test_the_author_line_carries_the_organizer_and_a_logo(self):
        embed = _embed()
        assert embed["author"]["name"] == "DICT"
        assert embed["author"]["icon_url"] == "https://dict.gov.ph/favicon.ico"

    def test_a_configured_logo_wins_over_the_favicon(self):
        assert organizer_icon("https://gcash.com/imagnation", SOURCES) == (
            "https://gcash.com/brand/logo.png"
        )

    def test_an_extracted_organizer_is_preferred_over_the_domain(self):
        embed = _embed(_facts(organizer="DICT Region VII"))
        assert embed["author"]["name"] == "DICT Region VII"

    def test_an_untrusted_host_gets_no_logo(self):
        """Otherwise any scraped domain could put its artwork in our alert."""
        assert organizer_icon("https://random-blog.example/post", SOURCES) is None


class TestImageSafety:
    def test_a_banner_from_a_trusted_page_is_shown(self):
        assert _embed()["image"]["url"] == "https://dict.gov.ph/media/banner.jpg"

    def test_a_banner_from_an_untrusted_host_is_refused(self):
        """An image is a link the reader cannot inspect before it renders."""
        assert displayable_image("https://evil.example/shock.png", SOURCES) is None
        embed = _embed(_facts(image_url="https://evil.example/shock.png"))
        assert "image" not in embed

    def test_a_social_post_contributes_no_image(self):
        """fbcdn and facebook.com are dropped by link_trust, so nothing is rendered."""
        assert displayable_image("https://scontent.fmnl.fbcdn.net/v/t39/photo.jpg", SOURCES) is None

    def test_no_image_is_not_an_error(self):
        assert "image" not in _embed(_facts(image_url=None))


def test_an_event_with_almost_nothing_known_still_renders():
    """The renderer must not depend on any particular fact being present."""
    embed = _embed(
        EventFacts(title="Something", canonical_url="https://dict.gov.ph/x"),
    )
    assert embed["title"] == "Something"
    assert isinstance(embed["fields"], list)
    assert "timestamp" not in embed


def test_scraped_text_is_still_escaped():
    """Discord renders markdown inside embeds; a post's own link must not look like ours."""
    facts = _facts(
        eligibility=EligibilityFact(text="Open to [everyone](https://evil.example) really")
    )
    value = _field(_embed(facts), "Eligibility")["value"]
    # Both brackets have to carry a backslash. Escaping only the opening one leaves
    # `\[text](url)`, which Discord still renders as a link; breaking the closing bracket
    # too is what actually disarms it and leaves the URL readable as text.
    assert "\\[everyone\\]" in value
    assert value.count("[") == value.count("\\[")
    assert value.count("]") == value.count("\\]")
