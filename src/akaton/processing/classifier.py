from __future__ import annotations

from akaton.domain.enums import CompetitionCategory, DocumentKind
from akaton.processing.normalize import fold_text, is_news_url

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

# Body-level result and recap phrasing. Deliberately narrow, and unchanged: a live call
# for entries describes what the winners will get, so anything looser produces false
# positives on exactly the pages we want. These are also exported to the Facebook and
# Reddit mention classifier, which reads whole posts.
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

# Headline-level phrasing, which can be far broader because a headline states the tense of
# the whole document in a few words.
#
# The narrow body lists above missed every real failure. Of the eight events the live
# database had stored and alerted on, none of "CIT students secure top spots in HackForGov
# 5", "WPU IDEA Pitch 2026 Champions Youth Innovation" or "QCU Hosts ... Showcases
# Excellence" matched anything, so each fell through to EVENT_ANNOUNCEMENT. This
# vocabulary comes from those headlines and from Philippine campus and agency newsroom
# style generally, which reports competitions with verbs the old list did not contain.
HEADLINE_RESULT_TERMS = RESULT_TERMS + (
    "champion",
    "champions",
    "secure top spot",
    "secures top spot",
    "secured top spot",
    "top spot",
    "top honors",
    "top honours",
    "bags",
    "bagged",
    "clinch",
    "clinches",
    "clinched",
    "emerged as",
    "emerges as",
    "hailed as",
    "crowned",
    "victorious",
    "grand winner",
    "1st place",
    "2nd place",
    "3rd place",
    "first place",
    "second place",
    "third place",
    "placed first",
    "placed second",
    "placed third",
    "wins",
    "won the",
    "triumph",
    # Campus newsroom style is "<team> <verb> at <competition>", where the competition is
    # the object of a preposition because the article is about the people, not the event.
    # A live resolve of "Hack4Gov Philippines" returned "ICLMS teams excel at Hack4Gov
    # 2025 Capture the Flag competition", which nothing above matched. The prepositions
    # are kept in each phrase so a forward-looking "excel in your career" cannot match.
    "excel at",
    "excel in the",
    "excels at",
    "excelled at",
    "shine at",
    "shines at",
    "shone at",
    "dominate at",
    "dominates",
    "dominated",
    "sweeps",
    "swept",
    "tops the",
    "topped the",
    "represent the philippines",
    "advance to the",
    "qualified for",
)
HEADLINE_RECAP_TERMS = RECAP_TERMS + (
    "concluded",
    "culminated",
    "wrapped up",
    "showcases",
    "showcased",
    "was held",
    "were held",
    "took place",
    "recently held",
    "successfully concluded",
    "look back",
    "in review",
)

# How much of the body stands in for a headline when a document has no title.
HEADLINE_CHARS = 200
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


def classify_document(
    text: str, *, title: str | None = None, url: str | None = None
) -> DocumentKind:
    """What kind of document this is, which decides whether it can alert at all.

    `title` and `url` are optional but change the answer, and the caller in
    `deterministic.py` has both.

    The headline carries the tense and the body dilutes it. A university news article
    about a hackathon is mostly *about the hackathon* — it names it, describes it, quotes
    its organisers — so on the body alone it is indistinguishable from the hackathon's own
    page. "CIT students secure top spots in HackForGov 5" is only unambiguous in the
    headline. Re-classifying the eight events the live database had stored showed exactly
    this: four came back UNRELATED from the title alone and EVENT_ANNOUNCEMENT from the
    body, and all eight had alerted.
    """
    # The title is part of the document for every later test. Callers in the pipeline pass
    # text that already contains it; a caller that passes them separately must get the
    # same answer.
    lowered = fold_text(f"{title}\n{text}" if title else text).casefold()
    headline = fold_text(title or text[:HEADLINE_CHARS]).casefold()

    # A live call for entries can still describe what the winners get: "cash prizes await
    # the winning teams" is a promise, not a result. An explicit call to action settles
    # the tense, the same way it already does for the bare word "results" below.
    forward_looking = any(term in lowered for term in ACTION_TERMS)
    headline_action = any(term in headline for term in ACTION_TERMS)

    # The headline decides first, and on a much broader vocabulary than the body dares
    # use. A news report of a finished competition usually quotes the original call for
    # entries, and that must not talk the classifier out of what the headline plainly
    # says — so this runs before `forward_looking`, which reads the whole document.
    if not headline_action:
        if any(term in headline for term in HEADLINE_RESULT_TERMS):
            return DocumentKind.WINNER_ANNOUNCEMENT
        if any(term in headline for term in HEADLINE_RECAP_TERMS):
            return DocumentKind.PAST_EVENT_RECAP
        # A `/news/` path or a `/YYYY/MM/DD/` date segment is a newsroom URL. It is the
        # only signal that catches a headline like "Polytechnic University of the
        # Philippines", which says nothing at all.
        if is_news_url(url):
            return DocumentKind.NEWS_ARTICLE

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
