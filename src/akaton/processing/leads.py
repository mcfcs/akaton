"""Turning a mention into one search, and never into twenty.

A question, a teammate search or a post-mortem names a competition without linking to it.
Today that evidence is discarded on Facebook, and on Reddit it becomes a candidate that
burns a fetch and a fourteen-second model call before the verifier rejects it — because
the thread is a question, not an announcement. A lead is the third option: keep the name,
spend one search on it later, and put the *page that answers* through the normal pipeline.

Two failure modes have to be avoided at once, and they pull in opposite directions.

Repeat pings. Twenty people asking about eGovPH is one competition, so the lead is keyed
on the name and every further mention only increments a counter.

A genuinely new edition. eGovPH running again in September is a different competition
from the March one, and a key that ignored that would suppress it for thirty days. The
`edition_hint` is what separates them: a year or month written near the name goes into
the key, so "the eGov hackathon" and "eGov hackathon September" are different leads and
the September one is searched immediately.

A September mention carrying no date at all does collapse onto the March lead. That is
the right conservative default — nothing in the text distinguishes them, and the
scheduled queries still cover the event — and it errs towards one search rather than a
search per person.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

# How long before a lead is worth another search.
#
# A resolved lead is not re-searched for a month: the page was found and the pipeline
# owns it from there. An unresolved one backs off, because a name that cannot resolve
# once usually cannot resolve at all, and without the backoff it would cost a request
# every run forever.
RESOLVED_COOLDOWN_DAYS = 30
UNRESOLVED_COOLDOWN_DAYS = 7
MAX_UNRESOLVED_COOLDOWN_DAYS = 60

# Names shorter than this are almost always a head term that slipped through.
MIN_NAME_LENGTH = 5


class LeadState:
    NEW = "NEW"
    SEARCHED = "SEARCHED"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    # Resolved to a page the pipeline then rejected. Distinct from UNRESOLVED so the
    # dashboard can tell "we never found it" from "we found it and it was not for us".
    DISCARDED = "DISCARDED"


def lead_key(normalized_name: str, edition_hint: str | None) -> str:
    """Stable identity for one competition-and-edition a mention referred to."""
    material = f"{normalized_name.strip()}|{(edition_hint or '').strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]


def unresolved_cooldown(search_runs: int) -> timedelta:
    """7 days, then 14, 28, 56, capped at 60."""
    days = UNRESOLVED_COOLDOWN_DAYS * (2 ** max(0, search_runs - 1))
    return timedelta(days=min(days, MAX_UNRESOLVED_COOLDOWN_DAYS))


def is_due(
    state: str,
    search_runs: int,
    last_searched_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether this lead has earned another search request."""
    if last_searched_at is None:
        return True
    now = now or datetime.now(UTC)
    if last_searched_at.tzinfo is None:
        last_searched_at = last_searched_at.replace(tzinfo=UTC)
    if state in {LeadState.RESOLVED, LeadState.DISCARDED}:
        return last_searched_at + timedelta(days=RESOLVED_COOLDOWN_DAYS) <= now
    return last_searched_at + unresolved_cooldown(search_runs) <= now
