from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from akaton.domain.enums import (
    DatePrecision,
    DocumentKind,
    EventPhase,
    LocationType,
    RegistrationState,
)
from akaton.domain.models import (
    DateFact,
    DocumentContext,
    EligibilityFact,
    EventFacts,
    Evidence,
    ExtractionEnvelope,
    LocationFact,
)
from akaton.processing import locale
from akaton.processing.classifier import classify_category, classify_document
from akaton.processing.editions import is_trustworthy
from akaton.processing.normalize import (
    extract_edition,
    fold_text,
    is_listing_url,
    is_registration_url,
    normalize_organizer,
    normalize_text,
    normalize_title,
    normalize_url,
)

MANILA_TZ = ZoneInfo("Asia/Manila")
MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_RE = re.compile(
    rf"\b(?P<month>{MONTHS})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{{2}}))?\b",
    re.IGNORECASE,
)
ISO_RE = re.compile(r"\b(?P<year>20\d{2})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})\b")
DATE_LABELS = {
    "registration_deadline": (
        "registration deadline",
        "applications close",
        "registration closes",
        "deadline",
    ),
    "event_start": ("event date", "hackathon date", "starts on", "competition date"),
    "registration_open_date": ("registration opens", "applications open"),
}
CITY_ALIASES = {
    "bgc": ("Taguig", "Metro Manila"),
    "bonifacio global city": ("Taguig", "Metro Manila"),
    "makati": ("Makati", "Metro Manila"),
    "taguig": ("Taguig", "Metro Manila"),
    "quezon city": ("Quezon City", "Metro Manila"),
    "pasig": ("Pasig", "Metro Manila"),
    "mandaluyong": ("Mandaluyong", "Metro Manila"),
    "pasay": ("Pasay", "Metro Manila"),
    "san juan": ("San Juan", "Metro Manila"),
    "manila": ("Manila", "Metro Manila"),
    "up diliman": ("Quezon City", "Metro Manila"),
    "ateneo de manila": ("Quezon City", "Metro Manila"),
    "de la salle university": ("Manila", "Metro Manila"),
}


def _parse_match(match: re.Match[str], context_year: int | None) -> DateFact:
    groups = match.groupdict()
    year_text = groups.get("year")
    inferred = year_text is None
    year = int(year_text or context_year or 0)
    if not year:
        return DateFact(evidence=match.group(0), confidence=0.25)
    try:
        if match.re is ISO_RE:
            month = int(groups["month"])
        else:
            month = datetime.strptime(groups["month"][:3], "%b").month
        value = datetime(year, month, int(groups["day"]), tzinfo=MANILA_TZ)
    except ValueError:
        return DateFact(evidence=match.group(0), confidence=0.0)
    return DateFact(
        value=value.astimezone(UTC),
        precision=DatePrecision.DATE,
        year_inferred=inferred,
        confidence=0.65 if inferred else 0.95,
        evidence=match.group(0),
    )


def _context_year(text: str, published: datetime | None) -> int | None:
    title_year = re.search(r"\b(20\d{2})\b", text[:300])
    if title_year:
        return int(title_year.group(1))
    return published.year if published else None


def extract_labeled_dates(text: str, published: datetime | None = None) -> dict[str, DateFact]:
    result = {name: DateFact() for name in DATE_LABELS}
    year = _context_year(text, published)
    lowered = text.casefold()
    for field, labels in DATE_LABELS.items():
        for label in labels:
            start = lowered.find(label)
            if start < 0:
                continue
            window = text[start : start + 160]
            match = ISO_RE.search(window) or DATE_RE.search(window)
            if match:
                result[field] = _parse_match(match, year)
                break
    return result


# Defined in processing.locale, which also knows the neighbouring countries, and
# re-exported here because this is where callers have always imported it from.
PH_TERMS = locale.PH_TERMS


SOCIAL_IMAGE_KEYS = ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src")


def _social_image(context: DocumentContext) -> str | None:
    """The page's own share image, which for a competition is usually its poster.

    `extract_html` already keeps every meta tag, so this costs nothing extra. Whether the
    image is safe to *show* is decided later against the same host trust the links use —
    this only records what the page claims.
    """
    for key in SOCIAL_IMAGE_KEYS:
        value = str(context.metadata.get(key) or "").strip()
        if value.lower().startswith(("http://", "https://")):
            return urljoin(context.url, value)
    return None


def is_philippine_host(url: str | None) -> bool:
    """A `.ph` host is a strong signal on its own: the TLD is reserved for the country."""
    host = (urlsplit(url or "").hostname or "").casefold()
    return host.endswith(".ph")


def extract_location(text: str, url: str | None = None) -> LocationFact:
    lowered = normalize_text(text)
    online = any(term in lowered for term in ("online", "virtual", "remote"))
    hybrid = "hybrid" in lowered
    city = region = None
    confidence = 0.0
    for alias, (candidate_city, candidate_region) in CITY_ALIASES.items():
        if alias in lowered:
            city, region, confidence = candidate_city, candidate_region, 0.9
            break
    # `normalize_text` strips punctuation, so "Philippine Space Agency" and a .ph host both
    # have to be recognised here or a local government page reads as an overseas event.
    country = (
        "PH"
        if any(term in lowered for term in PH_TERMS) or city or is_philippine_host(url)
        else None
    )
    if hybrid or (online and city):
        # A named venue alongside online wording is a hybrid event, not a remote one.
        # Treating it as purely online discards the very city that makes it local.
        location_type = LocationType.HYBRID
    elif online:
        location_type = LocationType.ONLINE
    elif city or country:
        location_type = LocationType.ONSITE
    else:
        location_type = LocationType.UNKNOWN
    return LocationFact(
        country=country,
        region=region,
        city=city,
        location_type=location_type,
        confidence=max(confidence, 0.75 if country else 0.0),
    )


def extract_eligibility(text: str) -> EligibilityFact:
    lowered = normalize_text(text)
    excludes_ph = any(
        phrase in lowered
        for phrase in (
            "residents of philippines are not eligible",
            "excluding philippines",
            "not open to philippines",
        )
    )
    ph_allowed = None
    if excludes_ph:
        ph_allowed = False
    elif any(
        phrase in lowered
        for phrase in (
            "open to filipinos",
            "participants from the philippines",
            "philippine residents",
            "nationwide",
            "open to all countries",
            "worldwide",
            "global competition",
        )
    ):
        ph_allowed = True
    student = any(
        term in lowered
        for term in ("college students", "university students", "undergraduate students")
    )
    student_only = student and any(
        term in lowered for term in ("only", "exclusive", "currently enrolled")
    )
    professionals = (
        True
        if any(term in lowered for term in ("professionals", "open to everyone", "open to all"))
        else None
    )
    confidence = 0.9 if ph_allowed is not None or student else 0.25
    eligibility_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
        if any(
            marker in sentence.casefold()
            for marker in (
                "eligible",
                "eligibility",
                "student",
                "resident",
                "participants from",
                "open to",
                "excluding",
                "nationwide",
            )
        )
    ]
    return EligibilityFact(
        text=" ".join(eligibility_sentences)[:1000] or None,
        student_only=student_only or None,
        university_students_allowed=student or None,
        professionals_allowed=professionals,
        philippines_allowed=ph_allowed,
        confidence=confidence,
    )


def _derive_states(facts: EventFacts, now: datetime) -> None:
    lowered = normalize_text(facts.description)
    if "cancelled" in lowered or "canceled" in lowered:
        facts.event_phase = EventPhase.CANCELLED
    elif "postponed" in lowered:
        facts.event_phase = EventPhase.POSTPONED
    elif facts.event_end.value and facts.event_end.value < now:
        facts.event_phase = EventPhase.PAST
    elif facts.event_start.value and facts.event_start.value <= now:
        facts.event_phase = EventPhase.ONGOING
    elif facts.event_start.value:
        facts.event_phase = EventPhase.UPCOMING
    elif facts.document_kind in {DocumentKind.EVENT_ANNOUNCEMENT, DocumentKind.REGISTRATION_OPEN}:
        facts.event_phase = EventPhase.ANNOUNCED

    if any(
        term in lowered
        for term in ("registration closed", "applications closed", "no longer accepting")
    ):
        facts.registration_state = RegistrationState.CLOSED
    elif facts.registration_deadline.value and facts.registration_deadline.value < now:
        if facts.registration_deadline.confidence >= 0.8:
            facts.registration_state = RegistrationState.CLOSED
    elif facts.document_kind is DocumentKind.REGISTRATION_OPEN:
        facts.registration_state = RegistrationState.OPEN
    elif any(
        term in lowered for term in ("registration opens", "applications will open", "coming soon")
    ):
        facts.registration_state = RegistrationState.FORTHCOMING


def extract_deterministically(
    context: DocumentContext,
    *,
    now: datetime | None = None,
    published: datetime | None = None,
) -> ExtractionEnvelope:
    now = now or datetime.now(UTC)
    # Folded once here so the date regexes, location and eligibility matching all see
    # ASCII. Social posts arrive in mathematical-bold, where 𝟮𝟬𝟮𝟲 defeats \b20\d{2}\b.
    combined = fold_text("\n".join(filter(None, (context.title, context.snippet, context.text))))
    dates = extract_labeled_dates(combined, published)
    title = context.title or context.metadata.get("og:title") or context.metadata.get("title")
    organizer = context.metadata.get("organizer") or context.metadata.get("author")
    registration = next(
        (normalize_url(url) for url in context.links if is_registration_url(url)), None
    )
    category = classify_category(combined)
    kind = (
        DocumentKind.DIRECTORY
        if is_listing_url(context.url)
        # The title and URL are what let the classifier see a newsroom post; without them
        # a news article about a competition is indistinguishable from the competition.
        else classify_document(combined, title=title, url=context.url)
    )
    facts = EventFacts(
        title=title,
        normalized_title=normalize_title(title),
        category=category,
        organizer=organizer,
        organizer_normalized=normalize_organizer(organizer),
        description=context.text[:4000],
        registration_open_date=dates["registration_open_date"],
        registration_deadline=dates["registration_deadline"],
        event_start=dates["event_start"],
        location=extract_location(combined, context.url),
        eligibility=extract_eligibility(combined),
        canonical_url=normalize_url(context.url),
        registration_url=registration,
        image_url=_social_image(context),
        document_kind=kind,
        topics=[
            term
            for term in (
                "ai",
                "software",
                "data",
                "technology",
                "consulting",
                "strategy",
                "business",
            )
            if term in normalize_text(combined).split()
        ],
    )
    edition_key, edition_year = extract_edition(
        title,
        facts.event_start.value.year if facts.event_start.value else None,
        # Only a trustworthy start refines the key to a month. An inferred year would
        # manufacture a March/September split out of a guess.
        month=(facts.event_start.value.month if is_trustworthy(facts.event_start) else None),
    )
    facts.edition_key = edition_key
    facts.edition_year = edition_year
    series_title = re.sub(r"\b20\d{2}\b", "", facts.normalized_title or "").strip()
    facts.series_key = f"{facts.organizer_normalized}:{series_title}"
    _derive_states(facts, now)
    evidence: list[Evidence] = []
    for name, date in dates.items():
        if date.evidence:
            evidence.append(
                Evidence(
                    field_name=name,
                    value=date.value,
                    quote=date.evidence,
                    source_url=context.url,
                    confidence=date.confidence,
                )
            )
    confidence, ambiguities = confidence_for(facts)
    return ExtractionEnvelope(
        facts=facts, evidence=evidence, overall_confidence=confidence, ambiguities=ambiguities
    )


def confidence_for(facts: EventFacts) -> tuple[float, list[str]]:
    """Score an extraction from the evidence actually present.

    Kept separate from extraction so a merged LLM result is scored the same way rather
    than being allowed to assert its own confidence, which the verifier gates on.
    """
    signals = sum(
        (
            facts.category.value != "UNKNOWN",
            bool(facts.title),
            bool(facts.registration_url),
            bool(facts.registration_deadline.value or facts.event_start.value),
            facts.location.confidence >= 0.7,
            facts.document_kind
            in {DocumentKind.EVENT_ANNOUNCEMENT, DocumentKind.REGISTRATION_OPEN},
        )
    )
    confidence = min(0.95, 0.35 + signals * 0.12)
    ambiguities = []
    if not facts.title:
        ambiguities.append("missing_title")
    if not facts.registration_deadline.value and not facts.event_start.value:
        ambiguities.append("missing_dates")
    if facts.category.value == "UNKNOWN":
        ambiguities.append("unknown_category")
    return confidence, ambiguities
