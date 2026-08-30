from __future__ import annotations

from datetime import UTC, datetime

from akaton.domain.enums import NotificationType, RegistrationState
from akaton.domain.models import DateFact, EventFacts
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
