from __future__ import annotations

from datetime import UTC, datetime

from akaton.domain.enums import CompetitionCategory, DocumentKind
from akaton.domain.models import (
    DateFact,
    DocumentContext,
    EligibilityFact,
    EventFacts,
    Evidence,
    ExtractionEnvelope,
)
from akaton.processing.llm import LLM_ASSISTED_CONFIDENCE_CAP, merge_extraction
from akaton.processing.relevance import is_plausibly_relevant

SOURCE = (
    "Manila Hackathon 2026. Registration is now open to university students in the "
    "Philippines. The registration deadline is October 5, 2026. Prizes worth PHP 100,000."
)
CONTEXT = DocumentContext(url="https://example.ph/e", title="Manila Hackathon 2026", text=SOURCE)


def _deterministic(**overrides) -> ExtractionEnvelope:
    facts = EventFacts(
        title="Manila Hackathon 2026",
        category=CompetitionCategory.HACKATHON,
        document_kind=DocumentKind.REGISTRATION_OPEN,
        registration_deadline=DateFact(value=datetime(2026, 10, 5, tzinfo=UTC), confidence=0.95),
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return ExtractionEnvelope(facts=facts, overall_confidence=0.71)


def _llm(facts: EventFacts, evidence: list[Evidence] | None = None) -> ExtractionEnvelope:
    return ExtractionEnvelope(
        facts=facts, evidence=evidence or [], overall_confidence=0.99, ambiguities=[]
    )


def test_an_unbacked_category_guess_is_refused():
    """Benchmarked, dolphin3:8b promoted a webinar and a job ad to OTHER_COMPETITION.

    Category feeds the verifier's `competition` gate and the scorer's +15 for a preferred
    category, so a guess with no quote behind it turns straight into a false alert. It is
    a gap-fill like every other contributed field and gets the same evidence requirement.
    """
    guess = _llm(EventFacts(category=CompetitionCategory.OTHER_COMPETITION))
    merged = merge_extraction(_deterministic(category=CompetitionCategory.UNKNOWN), guess, CONTEXT)
    assert merged.facts.category is CompetitionCategory.UNKNOWN


def test_a_category_quoted_from_the_document_is_accepted():
    backed = _llm(
        EventFacts(category=CompetitionCategory.HACKATHON),
        [Evidence(field_name="category", value="HACKATHON", quote="Manila Hackathon 2026")],
    )
    merged = merge_extraction(_deterministic(category=CompetitionCategory.UNKNOWN), backed, CONTEXT)
    assert merged.facts.category is CompetitionCategory.HACKATHON


def test_an_unbacked_eligibility_claim_is_refused():
    """ "Philippines allowed" decides whether an event can alert at all."""
    guess = _llm(EventFacts(eligibility=EligibilityFact(philippines_allowed=True)))
    merged = merge_extraction(_deterministic(), guess, CONTEXT)
    assert merged.facts.eligibility.philippines_allowed is None


def test_the_model_cannot_assert_its_own_confidence():
    merged = merge_extraction(_deterministic(), _llm(EventFacts()), CONTEXT)
    assert merged.overall_confidence <= LLM_ASSISTED_CONFIDENCE_CAP


def test_deterministic_facts_are_not_overwritten():
    llm = _llm(
        EventFacts(
            title="Something Else Entirely",
            registration_deadline=DateFact(value=datetime(2027, 1, 1, tzinfo=UTC)),
        ),
        [Evidence(field_name="title", value="x", quote="Manila Hackathon 2026")],
    )
    merged = merge_extraction(_deterministic(), llm, CONTEXT)
    assert merged.facts.title == "Manila Hackathon 2026"
    assert merged.facts.registration_deadline.value.year == 2026


def test_a_gap_is_filled_when_the_quote_is_really_in_the_source():
    llm = _llm(
        EventFacts(organizer="Manila Tech Guild"),
        [Evidence(field_name="organizer", value="Manila Tech Guild", quote="Manila Hackathon")],
    )
    merged = merge_extraction(_deterministic(organizer=None), llm, CONTEXT)
    assert merged.facts.organizer == "Manila Tech Guild"


def test_a_fabricated_field_is_dropped_not_the_whole_extraction():
    """An empty or unsupported evidence list used to pass validation unconditionally."""
    llm = _llm(
        EventFacts(organizer="Invented Organisation", prize_information="A million pesos"),
        [Evidence(field_name="organizer", value="Invented Organisation", quote="never appears")],
    )
    merged = merge_extraction(_deterministic(organizer=None), llm, CONTEXT)
    assert merged.facts.organizer is None
    # The unquoted prize is dropped too, but the rest of the extraction survives.
    assert merged.facts.prize_information is None
    assert merged.facts.title == "Manila Hackathon 2026"


def test_document_kind_may_be_downgraded():
    llm = _llm(EventFacts(document_kind=DocumentKind.PAST_EVENT_RECAP))
    merged = merge_extraction(_deterministic(), llm, CONTEXT)
    assert merged.facts.document_kind is DocumentKind.PAST_EVENT_RECAP


def test_document_kind_may_not_be_promoted():
    """REGISTRATION_OPEN unlocks the registration gate and the actionability score."""
    llm = _llm(EventFacts(document_kind=DocumentKind.REGISTRATION_OPEN))
    merged = merge_extraction(_deterministic(document_kind=DocumentKind.NEWS_ARTICLE), llm, CONTEXT)
    assert merged.facts.document_kind is DocumentKind.NEWS_ARTICLE


def test_an_off_topic_page_is_not_worth_a_model_call():
    off_topic = DocumentContext(
        url="https://example.com/news",
        title="City council approves new road budget",
        text="The council voted to approve the annual road maintenance budget on Tuesday.",
    )
    assert is_plausibly_relevant(off_topic) is False


def test_a_thin_competition_page_is_still_worth_a_model_call():
    thin = DocumentContext(
        url="https://example.ph/x", title="Announcement", text="A competition is being organised."
    )
    assert is_plausibly_relevant(thin) is True


def test_a_vague_word_alone_is_not_enough():
    vague = DocumentContext(
        url="https://example.com/x",
        title="Consulting services",
        text="We help firms solve their toughest business challenges every day.",
    )
    assert is_plausibly_relevant(vague) is False
