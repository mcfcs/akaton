from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akaton.domain.models import DateFact
from akaton.processing.editions import (
    EDITION_GAP_DAYS,
    dates_contradict,
    editions_conflict,
    is_trustworthy,
)
from akaton.processing.normalize import extract_edition


def start(year, month, day, *, confidence=0.95, inferred=False):
    return DateFact(
        value=datetime(year, month, day, tzinfo=UTC),
        confidence=confidence,
        year_inferred=inferred,
    )


@pytest.mark.parametrize(
    ("left", "right", "conflict"),
    [
        # A coarse key is the same edition seen less precisely, not a different one. This
        # is the case that makes a raw `!=` wrong: every stored row has a year-only key,
        # and `!=` would split each from its own next update the moment one parsed a month.
        ("2026", "2026-09", False),
        ("2026-09", "2026", False),
        ("2026-03", "2026-09", True),
        ("2026", "2027", True),
        ("2026-03", "2026-03", False),
        (None, "2026-09", False),
        ("2026-09", None, False),
        (None, None, False),
        ("2026:edition-3", "2026:edition-4", True),
    ],
)
def test_editions_conflict_only_when_two_keys_positively_disagree(left, right, conflict):
    assert editions_conflict(left, right) is conflict


def test_a_month_refines_the_key_only_when_the_date_can_be_trusted():
    assert extract_edition("eGov Hackathon 2026", month=9) == ("2026-09", 2026)
    # Without a month it degrades to today's behaviour, which is what keeps stored rows
    # compatible with rows written after this change.
    assert extract_edition("eGov Hackathon 2026") == ("2026", 2026)
    assert extract_edition("eGov Hackathon", 2026, month=3) == ("2026-03", 2026)


def test_an_explicit_edition_number_still_wins_over_a_month():
    """ "Season 4" identifies the run better than the month its start happens to fall in."""
    assert extract_edition("DEVCON Hackathon 2026 season 4", month=9) == ("2026:edition-4", 2026)


def test_only_a_confident_uninferred_date_is_trustworthy():
    assert is_trustworthy(start(2026, 3, 10))
    assert not is_trustworthy(start(2026, 3, 10, inferred=True))
    assert not is_trustworthy(start(2026, 3, 10, confidence=0.65))
    assert not is_trustworthy(DateFact())
    assert not is_trustworthy(None)


def test_dates_contradict_needs_a_trustworthy_date_on_both_sides():
    march, september = start(2026, 3, 10), start(2026, 9, 14)
    assert dates_contradict(march, september)
    # A Facebook announcement often carries a deadline and no start at all. Reading that
    # absence as disagreement would break the three-reposts-one-alert collapse.
    assert not dates_contradict(march, DateFact())
    assert not dates_contradict(DateFact(), september)
    assert not dates_contradict(start(2026, 3, 10, inferred=True), september)


def test_a_gap_inside_the_window_is_the_same_run_moving():
    assert not dates_contradict(start(2026, 3, 10), start(2026, 4, 20))
    assert not dates_contradict(start(2026, 3, 10), start(2026, 3, 10))


def test_the_window_is_wide_enough_to_absorb_a_rescheduling():
    """A run does not move its own start by six weeks; a postponement stays on lineage."""
    assert EDITION_GAP_DAYS >= 30
