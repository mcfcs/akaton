from __future__ import annotations

from datetime import UTC, datetime

from akaton.domain.enums import CompetitionCategory, LocationType, RegistrationState
from akaton.domain.models import EventFacts, ParticipantProfile, ScoringResult

METRO_MANILA = {
    "manila",
    "makati",
    "taguig",
    "quezon city",
    "pasig",
    "mandaluyong",
    "pasay",
    "san juan",
}


def score_event(
    facts: EventFacts,
    profile: ParticipantProfile,
    config: dict,
    *,
    source_authority: int,
    now: datetime | None = None,
) -> ScoringResult:
    now = now or datetime.now(UTC)
    weights = config.get("weights", {})
    components: dict[str, int] = {}
    reasons: list[str] = []
    city = (facts.location.city or "").casefold()
    preferred = {item.casefold() for item in profile.preferred_cities}

    if city in preferred:
        geo = weights.get("geography", 30)
        reasons.append(f"preferred Metro Manila city: {facts.location.city}")
    elif city in METRO_MANILA:
        geo = min(27, weights.get("geography", 30))
        reasons.append("Metro Manila location")
    elif facts.location.region and facts.location.region.casefold() in {
        x.casefold() for x in profile.nearby_regions
    }:
        geo = min(22, weights.get("geography", 30))
        reasons.append("near Metro Manila")
    elif facts.location.country == "PH":
        geo = min(18, weights.get("geography", 30))
        reasons.append("Philippines-based")
    elif (
        facts.location.location_type in {LocationType.ONLINE, LocationType.HYBRID}
        and facts.eligibility.philippines_allowed
    ):
        geo = min(24, weights.get("geography", 30))
        reasons.append("online and explicitly open to the Philippines")
    else:
        geo = 0
    components["geography"] = geo

    if facts.eligibility.philippines_allowed is True or facts.location.country == "PH":
        eligibility = weights.get("eligibility", 20)
        reasons.append("Philippine participation supported")
    elif facts.eligibility.philippines_allowed is None:
        eligibility = 5
    else:
        eligibility = 0
    components["eligibility"] = eligibility

    preferred_categories = {str(item) for item in config.get("preferred_categories", [])}
    if facts.category.value in preferred_categories:
        category = weights.get("category", 15)
        reasons.append(f"preferred category: {facts.category.value.replace('_', ' ').title()}")
    elif facts.category not in {CompetitionCategory.UNKNOWN, CompetitionCategory.OTHER_COMPETITION}:
        category = min(12, weights.get("category", 15))
    elif facts.category is CompetitionCategory.OTHER_COMPETITION:
        category = min(7, weights.get("category", 15))
    else:
        category = 0
    components["category"] = category

    preferred_topics = {str(item).casefold() for item in config.get("preferred_topics", [])}
    topic_matches = preferred_topics.intersection(topic.casefold() for topic in facts.topics)
    components["topic"] = weights.get("topic", 10) if topic_matches else 0
    if topic_matches:
        reasons.append("focus: " + ", ".join(sorted(topic_matches)))

    if facts.registration_state is RegistrationState.OPEN:
        actionability = weights.get("actionability", 15)
        reasons.append("registration open")
    elif facts.registration_state is RegistrationState.FORTHCOMING:
        actionability = min(10, weights.get("actionability", 15))
        reasons.append("registration forthcoming")
    else:
        actionability = 0
    components["actionability"] = actionability

    components["authority"] = round(weights.get("authority", 5) * min(100, source_authority) / 100)

    lead_time = 0
    target = facts.registration_deadline.value or facts.event_start.value
    if target:
        days = (target - now).days
        if days >= 21:
            lead_time = weights.get("lead_time", 5)
            reasons.append("at least three weeks to prepare")
        elif days >= 7:
            lead_time = min(3, weights.get("lead_time", 5))
    components["lead_time"] = lead_time

    total = min(100, sum(components.values()))
    thresholds = config.get("thresholds", {})
    if total >= thresholds.get("high", 80):
        tier = "HIGH_PRIORITY"
    elif total >= thresholds.get("recommended", 65):
        tier = "RECOMMENDED"
    elif total >= thresholds.get("possible", 50):
        tier = "POSSIBLE"
    else:
        tier = "LOW"
    return ScoringResult(total=total, tier=tier, components=components, match_reasons=reasons)
