"""The classifier against the documents that actually fooled it.

`tests/fixtures/news_vs_events.json` is not invented. It is the real page text of the
eight events the live database had stored and alerted on — six of them wrong — plus six
candidates the classifier already rejected correctly, as a guard against over-correcting.

The failure it pins: a university or agency news article about a competition is mostly
*about the competition*, so on the body alone it reads exactly like the competition's own
page. The tense survives only in the headline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akaton.domain.enums import DocumentKind
from akaton.processing.classifier import classify_document
from akaton.processing.normalize import is_news_url
from akaton.processing.verifier import NON_ACTIONABLE

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "news_vs_events.json").read_text(
        encoding="utf-8"
    )
)
EXPECTED = [case for case in CASES if case.get("expected_kind")]
NON_ACTIONABLE_CASES = [case for case in CASES if case.get("expected_non_actionable")]


def _kind(case: dict) -> DocumentKind:
    return classify_document(case["text"], title=case["title"], url=case["url"])


@pytest.mark.parametrize("case", EXPECTED, ids=[c["id"] for c in EXPECTED])
def test_real_documents_are_classified_correctly(case):
    assert _kind(case).value == case["expected_kind"], case["note"]


@pytest.mark.parametrize("case", NON_ACTIONABLE_CASES, ids=[c["id"] for c in NON_ACTIONABLE_CASES])
def test_documents_already_rejected_stay_rejected(case):
    """Over-correcting is as bad as under-correcting; these were right before the fix."""
    assert _kind(case) in NON_ACTIONABLE, case["note"]


def test_every_recap_and_winner_document_is_non_actionable():
    """The whole point: a non-actionable kind is what stops the verifier accepting it."""
    for case in EXPECTED:
        kind = _kind(case)
        if case["expected_kind"] in {"WINNER_ANNOUNCEMENT", "PAST_EVENT_RECAP", "NEWS_ARTICLE"}:
            assert kind in NON_ACTIONABLE, case["id"]


class TestHeadlineBeatsBody:
    """The mechanism, in isolation from the fixtures."""

    BODY = (
        "The eGov Hackathon 2026 brought together student developers from across the "
        "Philippines. Registration is now open was the call that started it all back in "
        "June, and hundreds of teams answered. Prizes were awarded to the winning teams."
    )

    def test_a_result_headline_survives_an_announcement_quoted_in_the_body(self):
        """A news article quotes the original call for entries; that must not fool it."""
        assert (
            classify_document(self.BODY, title="CIT students secure top spots in HackForGov 5")
            is DocumentKind.WINNER_ANNOUNCEMENT
        )

    def test_a_genuine_call_for_entries_is_still_actionable(self):
        assert (
            classify_document(
                "Registration is now open for the eGov Hackathon 2026. Cash prizes await "
                "the winning teams and the grand champion takes home P100,000.",
                title="eGov Hackathon 2026 - registration now open",
            )
            is DocumentKind.REGISTRATION_OPEN
        )

    def test_prize_language_in_a_live_announcement_is_not_a_result(self):
        """ "champion" and "wins" belong in a call for entries describing the prizes."""
        assert (
            classify_document(
                "Register now. The grand champion wins P100,000 and the runner up takes "
                "second place honors.",
                title="Join the 2026 Manila Datathon",
            )
            is DocumentKind.REGISTRATION_OPEN
        )


class TestNewsUrlShape:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.pup.edu.ph/news/?go=UW5xh%2BLYZlA%3D",
            "http://wpu.edu.ph/home/2026/08/03/wpu-idea-pitch-2026-champions",
            "https://www.dmmmsu.edu.ph/2026/08/27/cit-students-secure-top-spots",
            "https://pia.gov.ph/news/dict-launches-hack4gov-in-basilan",
            "https://example.ph/press-release/something",
        ],
    )
    def test_newsroom_paths_are_recognised(self, url):
        assert is_news_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://dict.gov.ph/egov-hackathon-2026",
            "https://gcash.com/imagnation",
            "https://quezoncity.gov.ph/program/start-up-student-competition/",
            "",
            None,
        ],
    )
    def test_event_pages_are_not_news(self, url):
        assert not is_news_url(url)

    def test_a_registration_call_in_the_headline_overrides_the_url_shape(self):
        """An organiser announcing on their own newsroom is common and must still pass."""
        assert (
            classify_document(
                "Applications close on 30 September 2026.",
                title="Registration is now open for the DICT eGov Hackathon",
                url="https://dict.gov.ph/news/2026/08/01/egov-hackathon",
            )
            is DocumentKind.REGISTRATION_OPEN
        )
