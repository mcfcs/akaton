from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from rapidfuzz.fuzz import ratio

from akaton.domain.models import EventFacts
from akaton.processing.normalize import normalize_organizer, normalize_title, normalize_url


@dataclass(frozen=True)
class MatchDecision:
    action: str
    score: int
    reasons: tuple[str, ...]


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
