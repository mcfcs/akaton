from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from akaton.domain.enums import NotificationType, RegistrationState
from akaton.domain.models import EligibilityFact, EventFacts, LocationFact


@dataclass(frozen=True)
class DetectedChange:
    change_type: NotificationType
    field: str
    before: Any
    after: Any
    notify: bool


# The eligibility answers a reader acts on. `text` is the sentences the extractor happened
# to pick out of the page and `confidence` is how sure it was — neither is a fact about
# who may enter, and both wobble on every re-read. Comparing the whole model made a
# re-scrape that merely gathered different sentences, or firmed 0.9 up to 1.0, alert as
# "Eligibility Changed". This is the same rule `material_facts` applies to `description`.
ELIGIBILITY_RULES = (
    "student_only",
    "university_students_allowed",
    "professionals_allowed",
    "philippines_allowed",
)


def _rules(fact: EligibilityFact) -> dict[str, bool | None]:
    return {name: getattr(fact, name) for name in ELIGIBILITY_RULES}


# Where the event is, without the extractor's confidence in having read it.
PLACE_PARTS = ("country", "region", "city", "venue", "location_type")


def _place(fact: LocationFact) -> dict[str, Any]:
    value = {name: getattr(fact, name) for name in PLACE_PARTS}
    value["location_type"] = fact.location_type.value
    return value


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
                _place(before.location),
                _place(after.location),
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
    if _rules(before.eligibility) != _rules(after.eligibility):
        changes.append(
            DetectedChange(
                NotificationType.ELIGIBILITY_CHANGED,
                "eligibility",
                _rules(before.eligibility),
                _rules(after.eligibility),
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
