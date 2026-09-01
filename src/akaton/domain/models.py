from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from akaton.domain.enums import (
    CompetitionCategory,
    DatePrecision,
    DocumentKind,
    EventPhase,
    FailureCode,
    LocationType,
    RegistrationState,
    RejectionCode,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: str
    value: Any | None = None
    quote: str | None = None
    source_url: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DateFact(BaseModel):
    value: datetime | None = None
    precision: DatePrecision = DatePrecision.UNKNOWN
    timezone: str = "Asia/Manila"
    year_inferred: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str | None = None


class MentionLead(BaseModel):
    """A competition someone named without linking to it.

    Produced by a collector instead of a seed, when the thread turns out to be a
    question, a teammate search or a post-mortem. The name is what gets searched; the
    thread itself must never become the candidate, which is the failure this replaces.
    """

    name: str
    normalized_name: str
    edition_hint: str | None = None
    platform: str
    mention_kind: str
    source_url: str
    source_key: str | None = None
    excerpt: str | None = None
    query: str


class LeadRef(BaseModel):
    """Why a seed was looked for, carried alongside where it was found."""

    lead_id: int
    platform: str
    source_url: str
    name: str


class CandidateSeed(BaseModel):
    url: HttpUrl
    title: str | None = None
    snippet: str | None = None
    discovery_channel: str
    provider: str
    query: str | None = None
    source_key: str | None = None
    published_hint: datetime | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    # Document text an adapter already holds, for sources whose pages cannot be fetched.
    # A Reddit permalink serves only a JavaScript shell to a logged-out client, so a
    # self-post's body has to travel with the seed or it is lost.
    content: str | None = None
    # Outbound links collected with that prefetched body (registration forms, etc.).
    links: list[str] = Field(default_factory=list)
    # Set when this page was found by resolving a social mention. `discovery_channel`
    # still describes the *document* — a dict.gov.ph page found through a Facebook
    # question is an official document, and styling it as a social post would strip its
    # clickable official link — while this says why we went looking.
    lead: LeadRef | None = None


class FetchAttempt(BaseModel):
    method: str
    started_at: datetime
    elapsed_ms: int | None = None
    status_code: int | None = None
    proxy_id: str | None = None
    failure: FailureCode | None = None
    detail: str | None = None


class FetchResult(BaseModel):
    requested_url: str
    final_url: str | None = None
    fetch_method: str
    status_code: int | None = None
    content_type: str | None = None
    title: str | None = None
    text: str | None = None
    links: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    content_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    unchanged: bool = False
    proxy_used: bool = False
    usable: bool = False
    failure: FailureCode | None = None
    attempts: list[FetchAttempt] = Field(default_factory=list)


class LocationFact(BaseModel):
    country: str | None = None
    region: str | None = None
    city: str | None = None
    venue: str | None = None
    location_type: LocationType = LocationType.UNKNOWN
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class EligibilityFact(BaseModel):
    text: str | None = None
    student_only: bool | None = None
    university_students_allowed: bool | None = None
    professionals_allowed: bool | None = None
    philippines_allowed: bool | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class EventFacts(BaseModel):
    title: str | None = None
    normalized_title: str | None = None
    category: CompetitionCategory = CompetitionCategory.UNKNOWN
    organizer: str | None = None
    organizer_normalized: str | None = None
    description: str | None = None
    announcement_date: DateFact = Field(default_factory=DateFact)
    registration_open_date: DateFact = Field(default_factory=DateFact)
    registration_deadline: DateFact = Field(default_factory=DateFact)
    event_start: DateFact = Field(default_factory=DateFact)
    event_end: DateFact = Field(default_factory=DateFact)
    location: LocationFact = Field(default_factory=LocationFact)
    eligibility: EligibilityFact = Field(default_factory=EligibilityFact)
    team_size_min: int | None = Field(default=None, ge=1)
    team_size_max: int | None = Field(default=None, ge=1)
    prize_information: str | None = None
    prize_value: float | None = Field(default=None, ge=0)
    prize_currency: str | None = None
    canonical_url: str | None = None
    registration_url: str | None = None
    # The page's own og:image, which for a competition is almost always its poster or
    # banner. Presentational, so `material_facts` drops it before hashing: a redesigned
    # banner is not a material change to the event and must not version it or alert.
    image_url: str | None = None
    document_kind: DocumentKind = DocumentKind.AMBIGUOUS
    event_phase: EventPhase = EventPhase.UNKNOWN
    registration_state: RegistrationState = RegistrationState.UNKNOWN
    series_key: str | None = None
    edition_key: str | None = None
    edition_year: int | None = Field(default=None, ge=2000, le=2200)
    topics: list[str] = Field(default_factory=list)

    @field_validator("team_size_max")
    @classmethod
    def valid_team_range(cls, value: int | None, info: Any) -> int | None:
        minimum = info.data.get("team_size_min")
        if value is not None and minimum is not None and value < minimum:
            raise ValueError("team_size_max must be >= team_size_min")
        return value


class ExtractionEnvelope(BaseModel):
    facts: EventFacts
    evidence: list[Evidence] = Field(default_factory=list)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguities: list[str] = Field(default_factory=list)
    extraction_version: str = "deterministic-v1"


class ParticipantProfile(BaseModel):
    country_of_residence: str
    preferred_cities: list[str]
    nearby_regions: list[str] = Field(default_factory=list)
    participant_roles: list[str]
    education_level: str | None = None
    university: str | None = None
    degree_area: str | None = None
    graduation_year: int | None = None
    age: int | None = Field(default=None, ge=1, le=120)
    allow_online_international: bool = True


class VerificationDecision(BaseModel):
    accepted: bool
    rejection_codes: list[RejectionCode] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    gate_results: dict[str, bool] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ScoringResult(BaseModel):
    total: int = Field(ge=0, le=100)
    tier: str
    components: dict[str, int]
    match_reasons: list[str]


class DocumentContext(BaseModel):
    url: str
    title: str | None = None
    text: str
    snippet: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)


class NotificationPayload(BaseModel):
    dedupe_key: str
    notification_type: str
    event_id: int
    event_version: int
    title: str
    description: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    official_url: str | None = None
    registration_url: str | None = None
    footer_token: str
    relevance_tier: str
    confidence_label: str
    # Optional so a NotificationRow written before these existed still validates on the
    # reconciliation path. dedupe_key is untouched, so no alert is re-sent.
    source_kind: str = "official"  # "official" | "social_post" | "aggregator"
    # Decided where the sources config is available and carried with the payload, so a
    # re-render on the reconciliation path cannot reach a different verdict.
    official_url_clickable: bool = True
    source_label: str | None = None
    source_url: str | None = None
    links_field: str | None = None
    evidence_note: str | None = None
    # The organizer, shown as the embed's author line with its logo rather than as one
    # more row in a stack of fields.
    author_name: str | None = None
    author_icon_url: str | None = None
    author_url: str | None = None
    # Already judged against host trust where the sources config is available, exactly as
    # `official_url_clickable` is, so a re-render on the reconciliation path cannot reach
    # a different verdict about what is safe to display.
    image_url: str | None = None
    # Carried as datetimes, not preformatted strings, so the renderer can emit Discord's
    # own timestamp markup — which shows each reader the date in their timezone and a
    # live countdown, and which must not be markdown-escaped like scraped text is.
    event_start: datetime | None = None
    deadline: datetime | None = None


class DeliveryReceipt(BaseModel):
    message_id: str
    sent_at: datetime = Field(default_factory=utc_now)
