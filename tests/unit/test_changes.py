from __future__ import annotations

from datetime import UTC, datetime

from akaton.domain.enums import NotificationType, RegistrationState
from akaton.domain.models import DateFact, EligibilityFact, EventFacts
from akaton.processing.changes import detect_changes


def test_deadline_extension_and_registration_opened():
    before = EventFacts(
        title="Example",
        registration_state=RegistrationState.FORTHCOMING,
        registration_deadline=DateFact(value=datetime(2026, 10, 1, tzinfo=UTC)),
    )
    after = before.model_copy(deep=True)
    after.registration_state = RegistrationState.OPEN
    after.registration_deadline.value = datetime(2026, 10, 8, tzinfo=UTC)
    kinds = {change.change_type for change in detect_changes(before, after)}
    assert NotificationType.REGISTRATION_OPENED in kinds
    assert NotificationType.DEADLINE_EXTENDED in kinds


def test_description_wording_is_not_material_change():
    before = EventFacts(title="Example", description="First wording")
    after = EventFacts(title="Example", description="Second wording")
    assert detect_changes(before, after) == []


def test_reworded_eligibility_prose_is_not_a_change():
    """The extractor picks different sentences on each re-read of the same page.

    Comparing the whole model made that alert as "Eligibility Changed" — the reported
    false alarm. Only the answers a reader acts on are compared.
    """
    before = EventFacts(
        title="Example",
        eligibility=EligibilityFact(
            text="Open to Senior High School and Undergraduate students. Contact Myles Gomez.",
            university_students_allowed=True,
            confidence=0.9,
        ),
    )
    after = EventFacts(
        title="Example",
        eligibility=EligibilityFact(
            text="Open to SHS and Undergraduate students\nTeams must consist of 2-4 members",
            university_students_allowed=True,
            confidence=1.0,
        ),
    )
    assert detect_changes(before, after) == []


def test_a_real_eligibility_rule_change_still_alerts():
    before = EventFacts(
        title="Example", eligibility=EligibilityFact(text="Students", student_only=True)
    )
    after = EventFacts(
        title="Example",
        eligibility=EligibilityFact(text="Students", student_only=True, professionals_allowed=True),
    )
    changes = detect_changes(before, after)
    assert [change.change_type for change in changes] == [NotificationType.ELIGIBILITY_CHANGED]
    # What is recorded is the rules, not the prose or the extractor's confidence.
    assert "text" not in changes[0].after and "confidence" not in changes[0].after
    assert changes[0].after["professionals_allowed"] is True
