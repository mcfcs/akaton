"""Add a content fingerprint so one announcement on several URLs is one event.

Revision ID: c1a7d2e93f40
Revises: b38f4a7b61d4
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "c1a7d2e93f40"
down_revision = "b38f4a7b61d4"
branch_labels = None
depends_on = None

PREFIX_TOKENS = 24


def _normalize(value: str) -> str:
    """Local copy of processing.normalize.normalize_text.

    A migration has to keep producing the same value years from now, so it must not
    import application code that may change underneath it.
    """
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _prefix_hash(text: str) -> str | None:
    tokens = _normalize(text).split()[:PREFIX_TOKENS]
    if len(tokens) < 4:
        return None
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()[:32]


def upgrade() -> None:
    op.add_column("events", sa.Column("content_prefix_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_events_content_prefix_hash", "events", ["content_prefix_hash"])

    # Backfill, so events stored before this are visible to the new matcher immediately
    # rather than only after they are next updated.
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, title, current_facts FROM events")).fetchall()
    for event_id, title, current_facts in rows:
        facts = current_facts
        if isinstance(facts, str):
            try:
                facts = json.loads(facts)
            except ValueError:
                facts = {}
        description = (facts or {}).get("description") or ""
        digest = _prefix_hash(" ".join(part for part in (title, description) if part))
        if digest:
            connection.execute(
                sa.text("UPDATE events SET content_prefix_hash = :digest WHERE id = :id"),
                {"digest": digest, "id": event_id},
            )


def downgrade() -> None:
    op.drop_index("ix_events_content_prefix_hash", table_name="events")
    op.drop_column("events", "content_prefix_hash")
