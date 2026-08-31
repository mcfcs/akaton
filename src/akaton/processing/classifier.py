from __future__ import annotations

from akaton.domain.enums import CompetitionCategory, DocumentKind
from akaton.processing.normalize import fold_text

CATEGORY_TERMS: list[tuple[CompetitionCategory, tuple[str, ...]]] = [
    (CompetitionCategory.BUSINESS_CASE, ("business case competition", "case competition")),
    (
        CompetitionCategory.CONSULTING_COMPETITION,
        ("consulting competition", "strategy competition"),
    ),
    (CompetitionCategory.AI_COMPETITION, ("ai competition", "artificial intelligence challenge")),
    (CompetitionCategory.DATATHON, ("datathon", "data challenge")),
    (
        CompetitionCategory.HACKATHON,
        # "buildathon" and the misspellings turn up constantly in Philippine group posts.
        (
            "hackathon",
            "hack-a-thon",
            "hackaton",
            "hakaton",
            "codefest",
            "buildathon",
            "code league",
        ),
    ),
    (CompetitionCategory.IDEATHON, ("ideathon", "idea competition")),
    (CompetitionCategory.STARTUP_COMPETITION, ("startup competition", "pitch competition")),
    (CompetitionCategory.INNOVATION_CHALLENGE, ("innovation challenge", "innovation competition")),
]

# Every word that means "this is a competition", including the misspellings and Filipino
# terms that turn up in group posts. Shared with the Facebook adapter and the relevance
# gate so the vocabulary is not maintained in three places.
COMPETITION_TERMS = (
    "hackathon",
    "hackaton",
    "hakaton",
    "hack-a-thon",
    "buildathon",
    "codefest",
    "code fest",
    "code league",
    "datathon",
    "ideathon",
    "case competition",
    "case comp",
    "business case",
    "pitch competition",
    "startup competition",
    "innovation challenge",
    "innovation competition",
    "consulting competition",
    "coding competition",
    "programming competition",
    "kompetisyon",
    "paligsahan",
)

RESULT_TERMS = (
    "congratulations to",
    "winner announcement",
    "winning team",
    "champions of",
    "winners of",
)
RECAP_TERMS = (
    "event recap",
    "successfully held",
    "highlights from",
    "concluded last",
    "last weekend",
)
ACTION_TERMS = (
    "registration is now open",
    "registration now open",
    "applications are now open",
    "applications now open",
    "register now",
    "apply now",
    "registration deadline",
    "form your team",
    "calling all students",
)


def classify_category(text: str) -> CompetitionCategory:
    lowered = fold_text(text).casefold()
    for category, terms in CATEGORY_TERMS:
        if any(term in lowered for term in terms):
            return category
    if "competition" in lowered or "challenge" in lowered:
        return CompetitionCategory.OTHER_COMPETITION
    return CompetitionCategory.UNKNOWN


def classify_document(text: str) -> DocumentKind:
    lowered = fold_text(text).casefold()
    # A live call for entries can still describe what the winners get: "cash prizes await
    # the winning teams" is a promise, not a result. An explicit call to action settles
    # the tense, the same way it already does for the bare word "results" below.
    forward_looking = any(term in lowered for term in ACTION_TERMS)
    if not forward_looking:
        if any(term in lowered for term in RESULT_TERMS):
            return DocumentKind.WINNER_ANNOUNCEMENT
        if any(term in lowered for term in RECAP_TERMS):
            return DocumentKind.PAST_EVENT_RECAP
    if "results" in lowered and not forward_looking:
        return DocumentKind.RESULTS_POST
    if "webinar" in lowered and "competition" not in lowered and "hackathon" not in lowered:
        return DocumentKind.WEBINAR
    if (
        "conference" in lowered
        and ("competition" not in lowered or "no competition" in lowered)
        and "hackathon" not in lowered
    ):
        return DocumentKind.CONFERENCE
    if any(term in lowered for term in ("job opening", "we are hiring", "apply for this role")):
        return DocumentKind.JOB_POSTING
    if any(term in lowered for term in ACTION_TERMS):
        return DocumentKind.REGISTRATION_OPEN
    category = classify_category(lowered)
    if category is not CompetitionCategory.UNKNOWN:
        return DocumentKind.EVENT_ANNOUNCEMENT
    return DocumentKind.UNRELATED
