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
    (CompetitionCategory.HACKATHON, ("hackathon", "hack-a-thon", "codefest")),
    (CompetitionCategory.IDEATHON, ("ideathon", "idea competition")),
    (CompetitionCategory.STARTUP_COMPETITION, ("startup competition", "pitch competition")),
    (CompetitionCategory.INNOVATION_CHALLENGE, ("innovation challenge", "innovation competition")),
]

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
    if any(term in lowered for term in RESULT_TERMS):
        return DocumentKind.WINNER_ANNOUNCEMENT
    if any(term in lowered for term in RECAP_TERMS):
        return DocumentKind.PAST_EVENT_RECAP
    if "results" in lowered and not any(term in lowered for term in ACTION_TERMS):
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
