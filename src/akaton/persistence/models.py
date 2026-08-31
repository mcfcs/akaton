from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from akaton.domain.models import utc_now


class Base(DeclarativeBase):
    pass


class CandidateRow(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    discovered_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    discovery_channel: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    query: Mapped[str | None] = mapped_column(Text)
    source_key: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(64), default="DISCOVERED")
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SourceSnapshotRow(Base):
    __tablename__ = "source_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)
    requested_url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(64), default="web")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetch_method: Mapped[str] = mapped_column(String(32))
    proxy_used: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    search_snippet: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    extraction_version: Mapped[str | None] = mapped_column(String(64))


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    normalized_title: Mapped[str] = mapped_column(Text, index=True)
    organizer: Mapped[str | None] = mapped_column(Text)
    organizer_normalized: Mapped[str | None] = mapped_column(Text, index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    document_kind: Mapped[str] = mapped_column(String(64))
    event_phase: Mapped[str] = mapped_column(String(32), index=True)
    registration_state: Mapped[str] = mapped_column(String(32), index=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    registration_url: Mapped[str | None] = mapped_column(Text, index=True)
    series_key: Mapped[str | None] = mapped_column(Text, index=True)
    edition_key: Mapped[str | None] = mapped_column(String(128), index=True)
    edition_year: Mapped[int | None] = mapped_column(Integer, index=True)
    current_facts: Mapped[dict[str, Any]] = mapped_column(JSON)
    relevance_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    material_hash: Mapped[str] = mapped_column(String(64))
    # Hash of the announcement's opening words, so the same event reposted under another
    # URL is recognised. Derived, so it lives here rather than in EventFacts:
    # material_facts drops `description` before hashing, and a description-derived fact
    # would perturb every material_hash and version every event.
    content_prefix_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # Fields an operator corrected by hand, as name -> value. A refresh re-reads the
    # source page every 24 hours and would otherwise put the wrong value straight back,
    # so a correction has to be pinned to survive. Released one field at a time.
    manual_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Archived events drop out of the dashboard, are skipped by RefreshJob and can never
    # alert again. Archiving rather than deleting because seven tables reference this row
    # with no cascade, and `notifications` is the delivery audit trail.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    versions: Mapped[list[EventVersionRow]] = relationship(back_populates="event")


class EventSourceRow(Base):
    __tablename__ = "event_sources"
    __table_args__ = (UniqueConstraint("event_id", "snapshot_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshots.id"), index=True)
    role: Mapped[str] = mapped_column(String(64))
    authority: Mapped[int] = mapped_column(Integer)
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EventVersionRow(Base):
    __tablename__ = "event_versions"
    __table_args__ = (UniqueConstraint("event_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    facts_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    material_hash: Mapped[str] = mapped_column(String(64))
    extraction_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    event: Mapped[EventRow] = relationship(back_populates="versions")


class EventChangeRow(Base):
    __tablename__ = "event_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(64), index=True)
    field_name: Mapped[str] = mapped_column(String(64))
    before_json: Mapped[Any] = mapped_column(JSON)
    after_json: Mapped[Any] = mapped_column(JSON)
    notify: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    event_change_id: Mapped[int | None] = mapped_column(ForeignKey("event_changes.id"))
    notification_type: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    event_version: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    discord_message_id: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchRunRow(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    query_group: Mapped[str] = mapped_column(String(64))
    query: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    request_count: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProxyHealthRow(Base):
    __tablename__ = "proxy_health"

    proxy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    successful_requests: Mapped[int] = mapped_column(Integer, default=0)
    failed_requests: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    average_latency_ms: Mapped[float | None] = mapped_column(Float)
    disabled_reason: Mapped[str | None] = mapped_column(Text)


class LeadRow(Base):
    """A competition someone mentioned without linking to it.

    A question, a teammate search or a complaint names a competition that exists but is
    not announced in the thread. The name is worth one search; the answer is a real page
    that goes through the normal pipeline. This row is what remembers that we already
    looked, so twenty people asking about eGovPH cost one search rather than twenty.

    A `SearchRunRow` alone would have been cheaper — it already carries budget accounting
    and a timestamp — but it cannot say which post a lead came from or whether it ever
    resolved, and both are things an operator needs to see. So the lead is a row and the
    search it triggers is *also* recorded as a search run, leaving budget accounting and
    the Search-health panel untouched.
    """

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    # sha256 of the normalized name and the edition hint. The hint is in the key on
    # purpose: "the eGov hackathon" and "eGov hackathon September" are different leads,
    # so a new edition is searched at once instead of waiting out the previous cooldown.
    lead_key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    edition_hint: Mapped[str | None] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(32))
    mention_kind: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str | None] = mapped_column(String(128))
    mention_excerpt: Mapped[str | None] = mapped_column(Text)
    sightings: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="NEW", index=True)
    search_runs: Mapped[int] = mapped_column(Integer, default=0)
    resolved_url: Mapped[str | None] = mapped_column(Text)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_searched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


Index("ix_events_series_edition", EventRow.series_key, EventRow.edition_key)
Index("ix_leads_state_searched", LeadRow.state, LeadRow.last_searched_at)
