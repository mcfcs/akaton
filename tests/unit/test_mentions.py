from __future__ import annotations

import pytest

from akaton.processing.authority import organizer_vocabulary
from akaton.processing.mentions import classify_mention, extract_competition_name

SOURCES = {
    "organizers": [
        {
            "id": "dict",
            "name": "Department of Information and Communications Technology",
            "aliases": ["DICT", "DICT Philippines"],
            "domains": ["dict.gov.ph"],
        },
        {
            "id": "up",
            "name": "University of the Philippines",
            "aliases": ["UP Diliman", "UP"],
            "domains": ["up.edu.ph"],
        },
        {
            "id": "dlsu",
            "name": "De La Salle University",
            "aliases": ["DLSU", "De La Salle"],
            "domains": ["dlsu.edu.ph"],
        },
        {"id": "off", "name": "Disabled Org", "aliases": ["NOPE"], "enabled": False},
    ]
}
VOCAB = organizer_vocabulary(SOURCES)


def name(text: str) -> str | None:
    span = extract_competition_name(text, VOCAB)
    return span.text if span else None


class TestVocabulary:
    def test_aliases_are_available_whole_and_split(self):
        assert "dict" in VOCAB
        assert "diliman" in VOCAB and "up" in VOCAB
        assert "de la salle" in VOCAB and "salle" in VOCAB

    def test_connectives_from_a_long_name_never_enter_the_vocabulary(self):
        """Otherwise a name walk that trusts the vocabulary swallows "of the"."""
        assert "of" not in VOCAB
        assert "the" not in VOCAB
        assert "and" not in VOCAB
        assert "la" not in VOCAB

    def test_a_disabled_organizer_contributes_nothing(self):
        assert "nope" not in VOCAB


class TestNameExtraction:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # The three the design was written against, taken from the real corpus.
            ("pwede po ba manuod if hindi naka register sa egov hackaton?", "egov hackaton"),
            ("anyone joining the eGov hackathon?", "eGov hackathon"),
            ("any upcoming hackathon events near manila", None),
            # A head term is preferred over a coined token found earlier in the sentence.
            ("Questions about Hack4gov competition", "Hack4gov competition"),
            ("anyone joining Hack4Gov?", "Hack4Gov"),
            ("looking for teammates for the Shopee AI Hackathon 2026", "Shopee AI Hackathon"),
            ("congrats to the winners of the UNESCO Youth Hackathon", "UNESCO Youth Hackathon"),
            ("joining the DLSU business case competition", "DLSU business case competition"),
        ],
    )
    def test_names_are_lifted_out_of_real_phrasings(self, text, expected):
        assert name(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "is there any hackathon this year?",
            "hackathon 2026",
            "national hackathon",
            "saan may case competition sa Manila",
            "are there any upcoming hackathons and such suitable for college students",
            "hi is there any upcoming hackathon events near manila?? (im from mapua)",
            "",
        ],
    )
    def test_a_name_that_would_return_the_internet_is_refused(self, text):
        """A bare head, or a head plus a year, is not something to spend a search on."""
        assert name(text) is None

    def test_a_plural_head_is_still_a_bare_head(self):
        """ "hackathons" matched the coined-name rule and became a lead for the plural."""
        assert name("any upcoming hackathons this August") is None

    def test_punctuation_ends_a_name(self):
        assert name("did you register. Hackathon starts today") is None

    def test_an_alias_beats_the_stopword_list(self):
        """ "UP" is both an organizer alias and an English preposition."""
        assert name("joining the UP Diliman hackathon") == "UP Diliman hackathon"


class TestLeadKeys:
    def test_three_spellings_of_one_competition_share_a_key(self):
        """The corpus carries all three for one event; three keys would be three pings."""
        spellings = ["sa egov hackaton?", "the egov hackathon", "upcoming eGov Hackathons"]
        keys = {extract_competition_name(text, VOCAB).normalized for text in spellings}
        assert keys == {"egov hackathon"}

    def test_a_date_beside_the_name_becomes_the_edition_hint(self):
        span = extract_competition_name("joining the eGov hackathon this September?", VOCAB)
        assert span.edition_hint == "september"
        # A bare mention and a dated one are deliberately different leads, so a new
        # edition is searched at once instead of waiting out the previous one's cooldown.
        bare = extract_competition_name("anyone joining the eGov hackathon?", VOCAB)
        assert bare.edition_hint is None

    def test_tagalog_may_is_not_the_month_of_may(self):
        """ "may" means "there is" and appears in nearly every Taglish question."""
        span = extract_competition_name("may eGov hackathon ba this year", VOCAB)
        assert span is not None
        assert span.edition_hint is None

    def test_the_query_does_not_repeat_a_year_already_in_the_name(self):
        span = extract_competition_name("the ImaGnation 2026 challenge by GCash", VOCAB)
        assert span.query == "imagnation 2026 challenge"

    def test_the_query_uses_the_canonical_spelling_not_the_one_that_was_typed(self):
        """Three people in the real corpus wrote "hackaton"; searching that back is worse."""
        span = extract_competition_name("sa egov hackaton?", VOCAB)
        assert span.text == "egov hackaton", "the surface form is kept for display"
        assert span.query == "egov hackathon"


class TestClassifierIsUnchangedByTheMove:
    """`classify_mention` is what `facebook_parse.mention_kind` now delegates to."""

    def test_a_question_is_not_an_announcement(self):
        assert classify_mention("any upcoming hackathon near Manila?") == "question"

    def test_a_teammate_search_is_its_own_kind(self):
        assert classify_mention("looking for teammates for the eGov hackathon") == "teammate"

    def test_an_announcement_with_a_followable_link_is_an_event(self):
        assert (
            classify_mention(
                "Registration is now open for the 2026 hackathon",
                ["https://dict.gov.ph/egov-hackathon"],
            )
            == "event"
        )
