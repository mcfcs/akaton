from __future__ import annotations

from akaton.discord.embeds import summarise

# Real shape of gcash.com/imagnation: a countdown widget renders as short bare tokens
# before any prose, so quoting the text from the top puts that in the alert.
COUNTDOWN_PAGE = (
    "00\ndays\nhrs\nmins\nsecs\n"
    "Home\nAbout\nContact\n"
    "ImaGnation 2026 is a business case competition for undergraduate students. "
    "Registration closes on September 30, 2026 and teams of five may join."
)


def test_countdown_and_nav_fragments_are_dropped():
    summary = summarise(COUNTDOWN_PAGE)
    assert "secs" not in summary
    assert "Home" not in summary
    assert summary.startswith("ImaGnation 2026 is a business case competition")


def test_summary_prefers_sentences_about_the_competition():
    text = (
        "We use cookies to improve your browsing experience on this website always. "
        "The registration deadline for the hackathon is September 30, 2026 for all teams."
    )
    summary = summarise(text)
    assert "registration deadline" in summary.casefold()
    assert "cookies" not in summary.casefold()


def test_summary_is_bounded():
    text = "Registration is now open for the hackathon competition. " * 200
    assert len(summarise(text, limit=300)) <= 300


def test_summary_falls_back_to_prose_when_nothing_matches():
    text = "An organisation published a lengthy statement about its annual programme today."
    assert summarise(text) == text


def test_empty_text_is_handled():
    assert summarise(None) == ""
    assert summarise("") == ""
    assert summarise("00\ndays\nhrs\nsecs") == ""
