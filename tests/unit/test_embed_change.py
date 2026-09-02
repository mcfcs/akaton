"""How an update alert reads.

`build_change_payload` had no test, and it showed: it stringified the stored JSON, so an
eligibility update reached Discord as `{'text': '…', 'student_only': None, …}` — a Python
repr, in a channel, as the entire body of the alert. These pin the rendering and the
context an update carries.
"""

from __future__ import annotations

from datetime import UTC, datetime

from akaton.discord.embeds import build_change_payload, embed_dict
from akaton.domain.models import DateFact, EventFacts
from akaton.persistence.models import EventChangeRow

SOURCES = {
    "organizers": [
        {
            "id": "dict",
            "name": "Department of Information and Communications Technology",
            "aliases": ["DICT"],
            "domains": ["dict.gov.ph"],
            "authority": 90,
        }
    ],
    "platforms": {"gov.ph": 85},
}


def _facts(**overrides) -> EventFacts:
    facts = EventFacts(
        title="eGov Hackathon 2026",
        canonical_url="https://dict.gov.ph/egov-hackathon-2026",
        registration_url="https://dict.gov.ph/register",
        image_url="https://dict.gov.ph/media/banner.jpg",
        event_start=DateFact(value=datetime(2026, 10, 20, tzinfo=UTC)),
        registration_deadline=DateFact(value=datetime(2026, 10, 5, tzinfo=UTC)),
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def _change(change_type: str, before, after, *, change_id: int = 3) -> EventChangeRow:
    return EventChangeRow(
        id=change_id,
        event_id=12,
        change_type=change_type,
        field_name=change_type.lower(),
        before_json=before,
        after_json=after,
        notify=True,
    )


def _field(embed: dict, needle: str) -> str:
    return next(item["value"] for item in embed["fields"] if needle in item["name"])


def test_eligibility_change_reads_as_rules_not_as_a_python_repr():
    """The reported bug, pinned."""
    change = _change(
        "ELIGIBILITY_CHANGED",
        {"student_only": None, "university_students_allowed": True},
        {"student_only": True, "university_students_allowed": True, "professionals_allowed": False},
    )
    embed = embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES))
    value = _field(embed, "Eligibility")
    assert "{" not in value and "'" not in value
    assert "student_only" not in value and "confidence" not in value
    assert "Students only, university students, no professionals" in value


def test_location_change_reads_as_a_place():
    change = _change(
        "LOCATION_CHANGED",
        {"city": "Cebu", "region": "Central Visayas", "location_type": "ONSITE"},
        {"city": "Manila", "region": "NCR", "location_type": "ONSITE"},
    )
    embed = embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES))
    assert _field(embed, "Location") == "Cebu — Central Visayas → Manila — NCR"


def test_dates_are_formatted_not_printed_as_iso_strings():
    change = _change(
        "DEADLINE_EXTENDED", "2026-10-05T00:00:00+00:00", "2026-10-19T00:00:00+00:00"
    )
    value = _field(
        embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES)), "Deadline"
    )
    assert "T00:00" not in value
    assert "Oct 05, 2026" in value and "Oct 19, 2026" in value


def test_enum_values_are_not_shouted():
    change = _change("REGISTRATION_OPENED", "FORTHCOMING", "OPEN")
    # "🟢 Registration", not the "⏳ Registration closes" date field above it.
    value = _field(
        embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES)),
        "🟢 Registration",
    )
    assert value == "Forthcoming → Open"


def test_missing_before_value_says_so_rather_than_none():
    change = _change("PRIZE_CHANGED", None, "PHP 30,000")
    value = _field(
        embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES)), "Prize"
    )
    assert value == "Not specified → PHP 30,000"


def test_update_carries_the_same_context_a_new_event_alert_does():
    """An update is about the same competition, so the reader needs the same context."""
    change = _change("DEADLINE_EXTENDED", "2026-10-05T00:00:00+00:00", "2026-10-19T00:00:00+00:00")
    embed = embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES))
    assert embed["author"]["name"] == "DICT"
    assert embed["image"]["url"] == "https://dict.gov.ph/media/banner.jpg"
    assert embed["url"] == "https://dict.gov.ph/egov-hackathon-2026"
    assert "Register" in _field(embed, "Links")
    # The dates the change is about, as Discord's own countdown markup.
    assert _field(embed, "Registration closes").startswith("<t:")


def test_title_is_the_event_not_a_prefixed_sentence():
    change = _change("DEADLINE_EXTENDED", "2026-10-05T00:00:00+00:00", "2026-10-19T00:00:00+00:00")
    embed = embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES))
    assert embed["title"] == "eGov Hackathon 2026"
    assert embed["description"] == "The registration deadline was extended."


def test_urgent_update_is_coloured_as_such_but_still_reads_as_an_update():
    """Colour and label are separate: a cancellation is red, and still says "Update"."""
    change = _change("EVENT_CANCELLED", "UPCOMING", "CANCELLED")
    embed = embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES))
    assert embed["color"] == 0xE74C3C
    assert embed["footer"]["text"].startswith("Update · ")


def test_several_changes_are_summarised_in_one_alert():
    changes = [
        _change("DEADLINE_EXTENDED", "2026-10-05T00:00:00+00:00", "2026-10-19T00:00:00+00:00"),
        _change("VENUE_CHANGED", "SMX Manila", "PICC", change_id=4),
    ]
    payload = build_change_payload(12, 2, _facts(), changes, sources=SOURCES)
    assert payload.notification_type == "EVENT_UPDATED"
    assert payload.description == "2 details of this event changed."
    assert payload.footer_token.endswith("change:3,4")


def test_scraped_markdown_in_a_change_cannot_forge_a_link():
    """`escape_markdown` escapes the `[` and leaves `](url)`, which Discord still links.

    Both brackets are escaped, so the syntax is broken and the URL stays visible as text.
    """
    change = _change("PRIZE_CHANGED", None, "[Click here](https://evil.example)")
    value = _field(
        embed_dict(build_change_payload(12, 2, _facts(), [change], sources=SOURCES)), "Prize"
    )
    assert "\\[Click here\\]" in value
    assert value.count("]") == value.count("\\]")
    # The destination stays readable, so the reader can see where it points.
    assert "evil.example" in value


def test_untrusted_banner_is_not_shown_on_an_update():
    """The same host-trust rule a new-event alert applies to an image."""
    facts = _facts(
        canonical_url="https://random-blog.example/post",
        image_url="https://random-blog.example/banner.jpg",
    )
    change = _change("DEADLINE_EXTENDED", "2026-10-05T00:00:00+00:00", "2026-10-19T00:00:00+00:00")
    embed = embed_dict(build_change_payload(12, 2, facts, [change], sources=SOURCES))
    assert "image" not in embed
