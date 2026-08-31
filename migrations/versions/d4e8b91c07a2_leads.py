"""Add leads, so a mention of a competition costs one search rather than twenty.

Revision ID: d4e8b91c07a2
Revises: c1a7d2e93f40

Creates one table and backfills nothing. Edition keys on existing events are deliberately
left alone: `processing.editions.editions_conflict` treats a year-only key as a prefix of
a month-bearing one, so a stored "2026" stays compatible with the "2026-09" its own next
update will produce. Recomputing them here would silently re-key historical events on
facts that were parsed under a different set of rules.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e8b91c07a2"
down_revision = "c1a7d2e93f40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("edition_hint", sa.String(length=64), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("mention_kind", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=True),
        sa.Column("mention_excerpt", sa.Text(), nullable=True),
        sa.Column("sightings", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="NEW"),
        sa.Column("search_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_url", sa.Text(), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_searched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_key"),
    )
    op.create_index("ix_leads_normalized_name", "leads", ["normalized_name"])
    op.create_index("ix_leads_state", "leads", ["state"])
    # The due-lead query filters on state and orders by when each was last searched.
    op.create_index("ix_leads_state_searched", "leads", ["state", "last_searched_at"])


def downgrade() -> None:
    op.drop_index("ix_leads_state_searched", table_name="leads")
    op.drop_index("ix_leads_state", table_name="leads")
    op.drop_index("ix_leads_normalized_name", table_name="leads")
    op.drop_table("leads")
