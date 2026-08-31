from __future__ import annotations

from datetime import UTC, datetime

from akaton.discovery.facebook_parse import clean_facebook_text, mention_kind
from akaton.domain.enums import DocumentKind
from akaton.domain.models import DocumentContext
from akaton.processing.classifier import classify_category, classify_document
from akaton.processing.deterministic import extract_deterministically
from akaton.processing.normalize import fold_text

NOW = datetime(2026, 8, 31, tzinfo=UTC)


def bold(text: str) -> str:
    """Mathematical sans-serif bold, which is what Facebook posts are actually written in."""
    out = []
    for char in text:
        if "A" <= char <= "Z":
            out.append(chr(0x1D5D4 + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            out.append(chr(0x1D5EE + ord(char) - ord("a")))
        elif "0" <= char <= "9":
            out.append(chr(0x1D7EC + ord(char) - ord("0")))
        else:
            out.append(char)
    return "".join(out)


def test_fold_text_keeps_case_and_punctuation():
    # normalize_text would casefold and strip the hyphen, which breaks "hack-a-thon".
    assert fold_text(bold("Hack-A-Thon")) == "Hack-A-Thon"
    assert fold_text(None) == ""


def test_styled_registration_announcement_is_classified_like_plain_text():
    plain = "Registration is now open for our hackathon. Deadline September 30, 2026."
    assert classify_document(bold(plain)) is classify_document(plain)
    assert classify_document(bold(plain)) is DocumentKind.REGISTRATION_OPEN


def test_styled_category_terms_are_recognised():
    assert classify_category(bold("Join our hackathon")) is classify_category("Join our hackathon")


def test_styled_digits_do_not_defeat_the_date_regexes():
    styled = bold(
        "Manila Hackathon. Registration deadline October 5, 2026. Registration is now open."
    )
    extraction = extract_deterministically(
        DocumentContext(url="https://example.ph/e", title="Hackathon", text=styled), now=NOW
    )
    deadline = extraction.facts.registration_deadline.value
    assert deadline is not None and deadline.year == 2026


def test_facebook_cleaning_folds_before_classification():
    """The real philhacks corpus contains a post titled in mathematical bold."""
    styled = bold("REGISTRATION IS NOW OPEN: RESEARCH CONFERENCE 2026") + "\nJoin the summit."
    cleaned = clean_facebook_text(styled)
    assert "REGISTRATION IS NOW OPEN" in cleaned
    # A conference is not a competition, and that is only visible once folded.
    assert mention_kind(cleaned, []) == "unrelated"


def test_ascii_text_is_unchanged_by_folding():
    plain = "Registration is now open for the hackathon in Makati."
    assert fold_text(plain) == plain
    assert classify_document(plain) is DocumentKind.REGISTRATION_OPEN
