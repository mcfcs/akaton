from __future__ import annotations

from datetime import UTC, datetime

from akaton.domain.models import DateFact, EventFacts
from akaton.processing.dedup import compare_events
from akaton.processing.normalize import normalize_url


def _event(title: str, year: int, *, url: str | None = None) -> EventFacts:
    return EventFacts(
        title=title,
        normalized_title=title.casefold(),
        organizer="DICT",
        organizer_normalized="dict",
        canonical_url=url,
        edition_key=str(year),
        edition_year=year,
        event_start=DateFact(value=datetime(year, 10, 20, tzinfo=UTC), confidence=1),
    )


def test_tracking_parameters_removed():
    assert normalize_url("HTTPS://Example.COM:443/event/?utm_source=x&b=2&a=1#top") == (
        "https://example.com/event?a=1&b=2"
    )


def test_social_and_official_duplicate_merge():
    left = _event("DICT eGovPH Hackathon 2026", 2026)
    right = _event("DICT's eGov PH Hackathon 2026", 2026)
    assert compare_events(left, right).action == "MERGE"


def test_same_competition_different_year_never_merges():
    assert (
        compare_events(
            _event("eGovPH Hackathon 2026", 2026), _event("eGovPH Hackathon 2027", 2027)
        ).action
        == "SEPARATE"
    )


def test_same_registration_url_merges():
    left = _event("Hack One", 2026)
    right = _event("Different repost title", 2026)
    left.registration_url = "https://forms.gle/abc?utm_source=post"
    right.registration_url = "https://forms.gle/abc"
    assert compare_events(left, right).action == "MERGE"


def test_two_runs_in_one_year_on_one_page_never_merge():
    """The failure this was built for: same series, same year, same landing page.

    URL identity fires before any date is consulted, so before the edition key carried a
    month the September run merged onto March at 100 and never alerted.
    """
    left = _event("eGovPH Hackathon 2026", 2026, url="https://dict.gov.ph/egov-hackathon")
    right = _event("eGovPH Hackathon 2026", 2026, url="https://dict.gov.ph/egov-hackathon")
    left.edition_key, right.edition_key = "2026-03", "2026-09"
    left.event_start = DateFact(value=datetime(2026, 3, 20, tzinfo=UTC), confidence=1)
    right.event_start = DateFact(value=datetime(2026, 9, 14, tzinfo=UTC), confidence=1)
    assert compare_events(left, right).action == "SEPARATE"


def test_a_year_only_key_still_matches_its_own_refined_update():
    """Stored rows carry year-granularity keys; a raw `!=` would split them all."""
    left = _event("eGovPH Hackathon 2026", 2026, url="https://dict.gov.ph/egov-hackathon")
    right = _event("eGovPH Hackathon 2026", 2026, url="https://dict.gov.ph/egov-hackathon")
    right.edition_key = "2026-10"
    assert compare_events(left, right).action == "MERGE"
