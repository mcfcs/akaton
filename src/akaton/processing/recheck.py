"""When a page we have already judged is worth judging again.

Search returns the same URLs run after run. Measured on the real database, 97 of 364
candidates had been fetched more than once — 491 fetches for 364 pages — and the most
repeated was a Facebook group URL fetched seven times, rejected identically every time
because `config/domains.yaml` disables fetching that host. That verdict cannot change no
matter how often it is asked.

So a candidate is re-fetched only when the answer could plausibly differ, and how long
that takes depends on *why* it was dropped:

- an event we already track belongs to `RefreshJob`, which re-reads it on its own cadence.
  Discovery re-fetching it as well is pure duplication;
- a rejection about the *host or the kind of page* — a blocked domain, an unlisted domain,
  a foreign event, a results post — is a property of the page that a week will not change;
- a rejection about *what the page did not say yet* — thin confidence, an unconfirmed
  registration — can genuinely change when the organiser fills the page in.

Nothing here is permanent. Everything is re-examined eventually, and an operator can force
one immediately with Retry on the dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from akaton.domain.enums import CandidateState

# Rejections that describe the page or its host rather than its current contents.
SETTLED_REASONS = frozenset(
    {
        "SEARCH_SNIPPET_ONLY",
        "LOW_AUTHORITY",
        "NO_COMPETITION",
        "NOT_PHILIPPINES_ELIGIBLE",
        "INTERNATIONAL_ONSITE",
        "RESULTS_ONLY",
        "REGISTRATION_CLOSED",
    }
)

# States whose page is owned by another job, or which are still mid-flight.
EVENT_STATES = frozenset(
    {
        CandidateState.EVENT_CREATED.value,
        CandidateState.EVENT_MATCHED.value,
        CandidateState.NOTIFIED.value,
        CandidateState.NOTIFICATION_PENDING.value,
        CandidateState.SUPPRESSED.value,
    }
)


def last_judged_at(candidate: Any) -> datetime | None:
    """When the pipeline last reached a verdict on this candidate.

    Read from the trace rather than `updated_at`, because `upsert_candidate` touches the
    row on every sighting and would keep pushing `updated_at` forward — the cooldown would
    then never expire for exactly the URLs that keep coming back.
    """
    for step in reversed(candidate.trace or []):
        stamp = step.get("at")
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


# Channels whose whole purpose is to re-read a page we have already seen. `RefreshJob`
# runs the same pipeline on its own cadence, and the dashboard's Retry button is someone
# asking for this page now — neither may ever be told it was checked recently.
DELIBERATE_CHANNELS = frozenset({"refresh", "manual"})


def recheck_reason(
    candidate: Any,
    *,
    channel: str | None = None,
    historical_test: bool = False,
    config: Any = None,
    now: datetime | None = None,
) -> str | None:
    """A reason to skip this candidate, or None to process it normally."""
    # A backdate is someone asking explicitly for a date range to be re-read. Refusing on
    # the grounds that we looked last week would defeat the point of the button.
    if historical_test or channel in DELIBERATE_CHANNELS:
        return None
    if candidate.event_id and candidate.state in EVENT_STATES:
        return "already_an_event"
    judged = last_judged_at(candidate)
    if judged is None:
        return None
    settled = SETTLED_REASONS.intersection(candidate.rejection_reasons or [])
    if candidate.state == CandidateState.REJECTED.value and settled:
        days = _setting(config, "candidate_settled_recheck_days", 30)
    elif candidate.state in {CandidateState.REJECTED.value, CandidateState.AMBIGUOUS.value}:
        days = _setting(config, "candidate_recheck_days", 7)
    else:
        return None
    if judged + timedelta(days=days) > (now or datetime.now(UTC)):
        return "judged_recently"
    return None


def _setting(config: Any, name: str, default: int) -> int:
    # `value or default` would turn a deliberate 0 — "never defer" — back into the
    # default, which is the one setting someone is most likely to reach for.
    value = getattr(getattr(config, "app", None), name, None)
    return default if value is None else int(value)
