from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from akaton.domain.models import EventFacts, NotificationPayload, ScoringResult
from akaton.persistence.models import EventChangeRow


def _format_date(value: datetime | None) -> str:
    return (
        value.astimezone(ZoneInfo("Asia/Manila")).strftime("%b %d, %Y")
        if value
        else "Not specified"
    )


def build_new_event_payload(
    event_id: int,
    event_version: int,
    facts: EventFacts,
    score: ScoringResult,
    confidence: float,
) -> NotificationPayload:
    location = " — ".join(filter(None, (facts.location.city, facts.location.region)))
    if facts.location.location_type.value == "ONLINE":
        location = (
            "Online — Philippines eligible" if facts.eligibility.philippines_allowed else "Online"
        )
    fields = {
        "Category": facts.category.value.replace("_", " ").title(),
        "Organizer": facts.organizer or "Not specified",
        "Location": location or "Not specified",
        "Registration deadline": _format_date(facts.registration_deadline.value),
        "Event date": _format_date(facts.event_start.value),
        "Eligibility": (facts.eligibility.text or "Not specified")[:1024],
        "Team size": _team_size(facts.team_size_min, facts.team_size_max),
        "Prize": facts.prize_information or "Not specified",
        "Why this matched": " + ".join(score.match_reasons[:4]) or "Passed configured preferences",
    }
    label = "High" if confidence >= 0.85 else "Medium" if confidence >= 0.75 else "Low"
    return NotificationPayload(
        dedupe_key=f"new:{event_id}",
        notification_type="NEW_EVENT",
        event_id=event_id,
        event_version=event_version,
        title=facts.title or "Untitled competition",
        description=(facts.description or "")[:1000],
        fields=fields,
        official_url=facts.canonical_url,
        registration_url=facts.registration_url,
        footer_token=f"akaton:{event_id}:{event_version}:new",
        relevance_tier=score.tier,
        confidence_label=label,
    )


def build_change_payload(
    event_id: int,
    event_version: int,
    facts: EventFacts,
    changes: list[EventChangeRow],
) -> NotificationPayload:
    change_ids = ",".join(str(change.id) for change in changes)
    fields = {
        change.change_type.replace("_", " ").title(): (
            f"{_display_change(change.before_json)} → {_display_change(change.after_json)}"
        )[:1024]
        for change in changes
    }
    return NotificationPayload(
        dedupe_key=f"change:{event_id}:{change_ids}",
        notification_type=changes[0].change_type if len(changes) == 1 else "EVENT_UPDATED",
        event_id=event_id,
        event_version=event_version,
        title=f"Updated: {facts.title or 'Competition'}",
        description="An authoritative source reported a meaningful event update.",
        fields=fields,
        official_url=facts.canonical_url,
        registration_url=facts.registration_url,
        footer_token=f"akaton:{event_id}:{event_version}:change:{change_ids}",
        relevance_tier="UPDATE",
        confidence_label="High",
    )


def _display_change(value: object) -> str:
    return "Not specified" if value is None else str(value)


def _team_size(minimum: int | None, maximum: int | None) -> str:
    if minimum and maximum and minimum != maximum:
        return f"{minimum}–{maximum}"
    if minimum:
        return str(minimum)
    if maximum:
        return f"Up to {maximum}"
    return "Not specified"
