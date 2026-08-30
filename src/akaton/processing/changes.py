from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from akaton.domain.enums import NotificationType, RegistrationState
from akaton.domain.models import EventFacts


@dataclass(frozen=True)
class DetectedChange:
    change_type: NotificationType
    field: str
    before: Any
    after: Any
    notify: bool


def detect_changes(before: EventFacts, after: EventFacts) -> list[DetectedChange]:
    changes: list[DetectedChange] = []
    if (
        before.registration_state is not RegistrationState.OPEN
        and after.registration_state is RegistrationState.OPEN
    ):
        changes.append(
            DetectedChange(
                NotificationType.REGISTRATION_OPENED,
                "registration_state",
                before.registration_state,
                after.registration_state,
                True,
            )
        )
    if (
        before.registration_state is not RegistrationState.CLOSED
        and after.registration_state is RegistrationState.CLOSED
    ):
        changes.append(
            DetectedChange(
                NotificationType.REGISTRATION_CLOSED,
                "registration_state",
                before.registration_state,
                after.registration_state,
                False,
            )
        )
    old_deadline = before.registration_deadline.value
    new_deadline = after.registration_deadline.value
    if old_deadline and new_deadline and old_deadline != new_deadline:
        kind = (
            NotificationType.DEADLINE_EXTENDED
            if new_deadline > old_deadline
            else NotificationType.DEADLINE_CHANGED
        )
        changes.append(
            DetectedChange(kind, "registration_deadline", old_deadline, new_deadline, True)
        )
    for field, kind in (
        ("event_start", NotificationType.DATES_CHANGED),
        ("event_end", NotificationType.DATES_CHANGED),
    ):
        old_value = getattr(before, field).value
        new_value = getattr(after, field).value
        if old_value and new_value and old_value != new_value:
            changes.append(DetectedChange(kind, field, old_value, new_value, True))
    if before.location.city != after.location.city:
        changes.append(
            DetectedChange(
                NotificationType.LOCATION_CHANGED,
                "location",
                before.location.model_dump(),
                after.location.model_dump(),
                True,
            )
        )
    if before.location.venue != after.location.venue:
        changes.append(
            DetectedChange(
                NotificationType.VENUE_CHANGED,
                "venue",
                before.location.venue,
                after.location.venue,
                True,
            )
        )
    if before.eligibility.model_dump() != after.eligibility.model_dump():
        changes.append(
            DetectedChange(
                NotificationType.ELIGIBILITY_CHANGED,
                "eligibility",
                before.eligibility.model_dump(),
                after.eligibility.model_dump(),
                True,
            )
        )
    if before.event_phase != after.event_phase:
        if after.event_phase.value == "CANCELLED":
            changes.append(
                DetectedChange(
                    NotificationType.EVENT_CANCELLED,
                    "event_phase",
                    before.event_phase,
                    after.event_phase,
                    True,
                )
            )
        elif after.event_phase.value == "POSTPONED":
            changes.append(
                DetectedChange(
                    NotificationType.EVENT_POSTPONED,
                    "event_phase",
                    before.event_phase,
                    after.event_phase,
                    True,
                )
            )
    if before.prize_information != after.prize_information:
        changes.append(
            DetectedChange(
                NotificationType.PRIZE_CHANGED,
                "prize_information",
                before.prize_information,
                after.prize_information,
                False,
            )
        )
    return changes
