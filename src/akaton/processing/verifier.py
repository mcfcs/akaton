from __future__ import annotations

from datetime import UTC, datetime

from akaton.domain.enums import (
    DocumentKind,
    EventPhase,
    LocationType,
    RegistrationState,
    RejectionCode,
)
from akaton.domain.models import ExtractionEnvelope, ParticipantProfile, VerificationDecision

NON_ACTIONABLE = {
    DocumentKind.RESULTS_POST,
    DocumentKind.WINNER_ANNOUNCEMENT,
    DocumentKind.PAST_EVENT_RECAP,
    DocumentKind.NEWS_ARTICLE,
    DocumentKind.DIRECTORY,
    DocumentKind.CONFERENCE,
    DocumentKind.WEBINAR,
    DocumentKind.JOB_POSTING,
    DocumentKind.COURSE,
    DocumentKind.UNRELATED,
}


def verify_event(
    extraction: ExtractionEnvelope,
    profile: ParticipantProfile,
    *,
    source_authority: int,
    corroborating_sources: int = 1,
    now: datetime | None = None,
    allow_historical: bool = False,
) -> VerificationDecision:
    now = now or datetime.now(UTC)
    facts = extraction.facts
    rejection: list[RejectionCode] = []
    warnings: list[str] = []
    gates: dict[str, bool] = {}

    gates["competition"] = facts.category.value != "UNKNOWN"
    if not gates["competition"]:
        rejection.append(RejectionCode.NO_COMPETITION)

    gates["actionable_document"] = facts.document_kind not in NON_ACTIONABLE
    if not gates["actionable_document"]:
        rejection.append(
            RejectionCode.RESULTS_ONLY
            if facts.document_kind
            in {
                DocumentKind.RESULTS_POST,
                DocumentKind.WINNER_ANNOUNCEMENT,
                DocumentKind.PAST_EVENT_RECAP,
            }
            else RejectionCode.NO_COMPETITION
        )

    gates["future"] = facts.event_phase not in {EventPhase.PAST, EventPhase.CANCELLED} or (
        allow_historical and facts.event_phase is EventPhase.PAST
    )
    if not gates["future"]:
        rejection.append(RejectionCode.PAST_EVENT)

    deadline_past = bool(
        facts.registration_deadline.value
        and facts.registration_deadline.value < now
        and facts.registration_deadline.confidence >= 0.8
        and not facts.registration_deadline.year_inferred
    )
    gates["registration"] = allow_historical or (
        facts.registration_state in {RegistrationState.OPEN, RegistrationState.FORTHCOMING}
        and not deadline_past
    )
    if not gates["registration"]:
        if facts.registration_state is RegistrationState.CLOSED or deadline_past:
            rejection.append(RejectionCode.REGISTRATION_CLOSED)
        else:
            # Not provably closed, so this is not a rejection reason on its own — but the
            # gate still fails, and without a code the candidate disappeared into a
            # generic AMBIGUOUS bucket. Recorded so the dashboard can count it.
            warnings.append(RejectionCode.REGISTRATION_UNCONFIRMED.value)

    eligibility = facts.eligibility
    online = facts.location.location_type in {LocationType.ONLINE, LocationType.HYBRID}
    if eligibility.philippines_allowed is False:
        gates["philippines_eligible"] = False
        rejection.append(RejectionCode.NOT_PHILIPPINES_ELIGIBLE)
    elif facts.location.country == "PH":
        # A Philippine event is open to a Philippine participant whether it runs onsite,
        # hybrid, or online. Only a foreign online event has to say so explicitly.
        gates["philippines_eligible"] = True
    elif online and profile.allow_online_international:
        gates["philippines_eligible"] = eligibility.philippines_allowed is True
        if not gates["philippines_eligible"]:
            rejection.append(RejectionCode.NOT_PHILIPPINES_ELIGIBLE)
    else:
        gates["philippines_eligible"] = False
        rejection.append(RejectionCode.INTERNATIONAL_ONSITE)

    roles = {role.casefold() for role in profile.participant_roles}
    if eligibility.student_only and "university_student" not in roles:
        gates["profile_match"] = False
        rejection.append(RejectionCode.PROFILE_INCOMPLETE)
    else:
        gates["profile_match"] = True

    gates["authority"] = source_authority >= 60 or corroborating_sources >= 2
    if not gates["authority"]:
        rejection.append(RejectionCode.LOW_AUTHORITY)

    gates["confidence"] = extraction.overall_confidence >= 0.75
    if not gates["confidence"]:
        rejection.append(RejectionCode.LOW_CONFIDENCE)

    accepted = not rejection and all(gates.values())
    return VerificationDecision(
        accepted=accepted,
        rejection_codes=list(dict.fromkeys(rejection)),
        warnings=warnings,
        gate_results=gates,
        confidence=extraction.overall_confidence,
    )
