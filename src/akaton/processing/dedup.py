from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from rapidfuzz.fuzz import ratio, token_set_ratio

from akaton.domain.models import EventFacts
from akaton.processing.normalize import (
    normalize_organizer,
    normalize_text,
    normalize_title,
    normalize_url,
)


@dataclass(frozen=True)
class MatchDecision:
    action: str
    score: int
    reasons: tuple[str, ...]


# One announcement reaches the group several times: posted, shared from the organiser's
# page, reposted by a member. A real run produced three candidates for one DOST event on
# three different URLs, so URL identity cannot see them.
#
# Two are byte-identical for a while and the third prepends a clause, which shifts every
# token. A prefix hash catches the first pair cheaply; the third needs a similarity
# measure. Measured on that scrape, the three duplicates score 92-97 against each other
# while the highest-scoring pair of genuinely different events reaches only 68, so 85
# sits in a wide gap rather than on a guess.
PREFIX_TOKENS = 24
CONTENT_DUPLICATE_RATIO = 85


def content_prefix_hash(text: str | None) -> str | None:
    """Stable key for the opening of an announcement. Catches verbatim reposts."""
    # normalize_text folds case and Unicode and strips punctuation, so a repost that
    # differs only in styling hashes the same.
    tokens = normalize_text(text).split()[:PREFIX_TOKENS]
    if len(tokens) < 4:
        return None
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()[:32]


def content_similarity(left: str | None, right: str | None) -> float:
    """0-100 similarity, order- and duplication-insensitive.

    token_set_ratio is used rather than a plain ratio because the same announcement gets
    an introduction bolted on when it is shared, and comparing token sets ignores that.
    """
    first, second = normalize_text(left), normalize_text(right)
    if not first or not second:
        return 0.0
    return float(token_set_ratio(first, second))


def is_same_announcement(left: str | None, right: str | None) -> bool:
    return content_similarity(left, right) >= CONTENT_DUPLICATE_RATIO


def fingerprint_text(facts: EventFacts) -> str:
    return " ".join(part for part in (facts.title, facts.description) if part)


def compare_events(left: EventFacts, right: EventFacts) -> MatchDecision:
    reasons: list[str] = []
    if left.edition_year and right.edition_year and left.edition_year != right.edition_year:
        return MatchDecision("SEPARATE", 0, ("different edition years",))
    if left.edition_key and right.edition_key and left.edition_key != right.edition_key:
        return MatchDecision("SEPARATE", 0, ("different edition keys",))
    for field in ("canonical_url", "registration_url"):
        a, b = getattr(left, field), getattr(right, field)
        if a and b and normalize_url(a) == normalize_url(b):
            return MatchDecision("MERGE", 100, (f"same {field}",))

    title_score = ratio(normalize_title(left.title), normalize_title(right.title))
    organizers_match = bool(
        normalize_organizer(left.organizer)
        and normalize_organizer(left.organizer) == normalize_organizer(right.organizer)
    )
    if organizers_match:
        reasons.append("same organizer")
    dates_close = False
    if left.event_start.value and right.event_start.value:
        delta = abs(left.event_start.value - right.event_start.value)
        if delta > timedelta(days=120):
            return MatchDecision("SEPARATE", title_score, ("event dates over 120 days apart",))
        dates_close = delta <= timedelta(days=14)
        if dates_close:
            reasons.append("event dates within 14 days")
    edition_compatible = (
        not (left.edition_key and right.edition_key) or left.edition_key == right.edition_key
    )
    if title_score >= 92 and organizers_match and dates_close and edition_compatible:
        return MatchDecision("MERGE", title_score, tuple(["title similarity >= 92", *reasons]))
    if title_score >= 85 and (organizers_match or dates_close):
        return MatchDecision(
            "POSSIBLE_DUPLICATE", title_score, tuple(["title similarity 85-91", *reasons])
        )
    return MatchDecision("SEPARATE", title_score, tuple(reasons))
