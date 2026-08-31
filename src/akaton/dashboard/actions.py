"""Operator actions the dashboard can take on a stored event.

Sending an alert by hand is deliberately separate from the pipeline's own decision. The
pipeline suppresses below the relevance threshold, in shadow mode, and once an event has
already been announced — all correct for automatic delivery, and all things an operator
may want to override for one specific event they are looking at.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from akaton.config import ConfigBundle
from akaton.discord.embeds import build_new_event_payload
from akaton.domain.enums import NotificationState
from akaton.domain.models import EventFacts, NotificationPayload
from akaton.persistence.models import EventRow, NotificationRow
from akaton.persistence.repository import stable_hash
from akaton.processing.authority import authority_for_url
from akaton.processing.scorer import score_event


def build_manual_payload(row: EventRow, config: ConfigBundle) -> NotificationPayload:
    """Rebuild the alert for an event already in the database."""
    facts = EventFacts.model_validate(row.current_facts)
    authority = authority_for_url(row.canonical_url or "", config.sources)
    score = score_event(facts, config.profile, config.scoring, source_authority=authority)
    payload = build_new_event_payload(
        row.id,
        row.current_version,
        facts,
        score,
        row.confidence_score,
        discovery_channel=_channel_for(row),
        source_label=_source_label_for(row),
        links=[url for url in (facts.registration_url,) if url],
        published=facts.event_start.value,
        sources=config.sources,
    )
    # A manual send is an explicit instruction, so it must not collide with the automatic
    # `new:{event_id}` key or be refused because that alert already went out. The nonce
    # is what makes it safe to press twice: a timestamp alone collides on the unique
    # index when two sends land in the same second.
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    nonce = uuid4().hex[:8]
    return payload.model_copy(
        update={
            "dedupe_key": f"manual:{row.id}:{stamp}:{nonce}",
            "notification_type": "MANUAL_SEND",
            "footer_token": f"akaton:{row.id}:{row.current_version}:manual:{nonce}",
        }
    )


def _channel_for(row: EventRow) -> str | None:
    url = (row.canonical_url or "").casefold()
    if "facebook.com" in url:
        return "facebook"
    if "reddit.com" in url:
        return "reddit"
    return None


def _source_label_for(row: EventRow) -> str | None:
    url = (row.canonical_url or "").casefold()
    if "facebook.com/groups/" in url:
        return "Facebook group post"
    if "facebook.com" in url:
        return "Facebook post"
    if "reddit.com" in url:
        return "Reddit post"
    return None


def record_manual_notification(
    payload: NotificationPayload, *, message_id: str | None, error: str | None = None
) -> NotificationRow:
    """A row for the manual send, so the dashboard's history stays complete."""
    value = payload.model_dump(mode="json")
    return NotificationRow(
        event_id=payload.event_id,
        notification_type=payload.notification_type,
        dedupe_key=payload.dedupe_key,
        state=NotificationState.SENT.value if message_id else NotificationState.FAILED.value,
        event_version=payload.event_version,
        payload_hash=stable_hash(value),
        payload_json=value,
        discord_message_id=message_id,
        sent_at=datetime.now(UTC) if message_id else None,
        last_error=error[:2000] if error else None,
        attempts=1,
    )
