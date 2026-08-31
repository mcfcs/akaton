"""Let an operator correct an event by hand, and archive one that should not have been.

Revision ID: e7c25f81b3d9
Revises: d4e8b91c07a2

`manual_overrides` is what makes a correction stick: a refresh re-reads the source page
every 24 hours, so without pinning an edit is silently undone within a day.

`archived_at` is a soft delete. Seven tables carry `ForeignKey("events.id")` with no
cascade configured, and `notifications` is the record of what was actually delivered, so
removing the row would either fail or orphan the audit trail.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7c25f81b3d9"
down_revision = "d4e8b91c07a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so rows written before this migration read back as {} rather than
    # NULL, which would make every override lookup guard against None.
    op.add_column(
        "events",
        sa.Column("manual_overrides", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column("events", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_events_archived_at", "events", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_events_archived_at", table_name="events")
    op.drop_column("events", "archived_at")
    op.drop_column("events", "manual_overrides")
