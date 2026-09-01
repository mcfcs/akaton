"""The phrasing that dropped a real hackathon.

A Henkel Philippines Hackathon post reached the pipeline through the philhacks group, was
read correctly as a hackathon at 0.83 confidence, and was then dropped as AMBIGUOUS —
because "Registration is open until August 21" matched none of the action terms, so the
document was never REGISTRATION_OPEN, so the registration state stayed UNKNOWN, so the
verifier's registration gate failed.

Two separate misses in one sentence: nothing recognised that registration *was* open, and
nothing recognised *when it closed*.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akaton.domain.enums import DocumentKind, RegistrationState
from akaton.domain.models import DocumentContext
from akaton.processing.classifier import classify_document
from akaton.processing.deterministic import _combined_text, extract_deterministically

# The real post, trimmed. The emoji are in the original and matter: they are what the
# Unicode folding has to survive.
HENKEL = (
    "If you've been scrolling Facebook for opportunities... this might be the one worth "
    "stopping for. \U0001f440\n"
    "Got bold ideas and a passion for solving real-world challenges? \U0001f31f Join the "
    "Henkel Philippines Hackathon 2026, featuring Loctite, where you'll collaborate with "
    "fellow students and take on a real-world innovation challenge. \U0001f4a1\n"
    "Registration is open until August 21, so don't miss your chance to join."
)
AUGUST = datetime(2026, 8, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "Registration is open until August 21, 2026.",
        "Applications are open until 30 September 2026.",
        "We are now accepting applications for the 2026 hackathon.",
        "Open for registration until October 5, 2026.",
    ],
)
def test_open_ended_phrasing_is_a_live_call_for_entries(text):
    assert classify_document(text) is DocumentKind.REGISTRATION_OPEN


class TestTheRealPost:
    def _facts(self, now=AUGUST):
        # A prefetched social seed takes its title from the first line of its own text,
        # which is what caused the duplication this also guards against.
        context = DocumentContext(
            url="https://www.facebook.com/groups/philhacks/permalink/1518160267016909/",
            title=HENKEL.splitlines()[0],
            text=HENKEL,
        )
        return extract_deterministically(context, now=now).facts

    def test_registration_is_recognised_as_open(self):
        facts = self._facts()
        assert facts.document_kind is DocumentKind.REGISTRATION_OPEN
        assert facts.registration_state is RegistrationState.OPEN

    def test_the_closing_date_is_read(self):
        """Without it the post would stay open for ever instead of closing when it says."""
        deadline = self._facts().registration_deadline
        assert deadline.value is not None
        assert deadline.value.astimezone(UTC).strftime("%m-%d") in {"08-20", "08-21"}

    def test_it_is_no_longer_open_once_the_date_has_passed(self):
        facts = self._facts(now=datetime(2026, 9, 2, tzinfo=UTC))
        assert facts.registration_state is not RegistrationState.OPEN


def test_the_title_is_not_repeated_in_the_text_the_extractors_read():
    """A social post's title is its own first line, and joining them blindly printed the
    opening twice — which pushed "2026" past the 300 characters `_context_year` reads, so
    the deadline it found had no year and was unusable."""
    context = DocumentContext(url="https://example.ph/x", title=HENKEL.splitlines()[0], text=HENKEL)
    combined = _combined_text(context)
    assert combined.count("If you've been scrolling") == 1
    assert combined.index("2026") < 300, "the year must stay inside the window that reads it"


def test_a_web_page_still_keeps_its_title():
    """The de-duplication must not drop a title that is genuinely extra information."""
    combined = _combined_text(
        DocumentContext(
            url="https://dict.gov.ph/x",
            title="eGov Hackathon 2026",
            text="Registration is now open to students. Event date October 20, 2026 in Manila.",
        )
    )
    assert "eGov Hackathon 2026" in combined
    assert "Registration is now open" in combined


def test_a_snippet_that_merely_quotes_the_page_is_dropped():
    combined = _combined_text(
        DocumentContext(
            url="https://dict.gov.ph/x",
            title="eGov Hackathon 2026",
            snippet="Registration is now open",
            text="Registration is now open to students in Manila.",
        )
    )
    assert combined.count("Registration is now open") == 1
