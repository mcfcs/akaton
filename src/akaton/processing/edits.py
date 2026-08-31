"""Which fields of an event a person may correct, and how a correction is applied.

One table serves two callers, which is the point: the dashboard uses it to apply an edit,
and `repository.update_event` uses it to re-apply pinned edits after a refresh has re-read
the page. If they used separate code an edit would drift back to the page's value in ways
nobody would notice for a day.

`EventFacts` is nested — a date is a `DateFact` with its own confidence, a city lives
inside `LocationFact` — so each entry knows how to read and write its own shape. Anything
not listed here cannot be edited at all, which keeps a mistyped field name from silently
becoming a new attribute on the model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from akaton.domain.enums import CompetitionCategory, DatePrecision
from akaton.domain.models import DateFact, EventFacts
from akaton.processing.normalize import normalize_title, normalize_url


class EditError(ValueError):
    """A rejected edit, with a message meant for the person who typed it."""


@dataclass(frozen=True)
class FieldSpec:
    parse: Callable[[Any], Any]
    read: Callable[[EventFacts], Any]
    write: Callable[[EventFacts, Any], None]


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _required_text(value: Any) -> str:
    text = _text(value)
    if not text:
        raise EditError("must not be empty")
    return text


def _url(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if not text.lower().startswith(("http://", "https://")):
        raise EditError("must be an http(s) URL")
    return normalize_url(text)


def _category(value: Any) -> str:
    try:
        return CompetitionCategory(str(value).strip().upper()).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in CompetitionCategory)
        raise EditError(f"unknown category; expected one of {allowed}") from exc


def _date(value: Any) -> str | None:
    """An ISO date or datetime, stored as a fully trusted DateFact."""
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EditError("must be a date like 2026-10-20") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _score(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError) as exc:
        raise EditError("must be a whole number") from exc
    if not 0 <= score <= 100:
        raise EditError("must be between 0 and 100")
    return score


def _read_date(name: str) -> Callable[[EventFacts], Any]:
    def read(facts: EventFacts) -> Any:
        fact: DateFact = getattr(facts, name)
        return fact.value.isoformat() if fact.value else None

    return read


def _write_date(name: str) -> Callable[[EventFacts, Any], None]:
    def write(facts: EventFacts, value: Any) -> None:
        if value is None:
            setattr(facts, name, DateFact())
            return
        # A person typing a date is the most reliable source there is, so it is recorded
        # at full confidence and never marked inferred. That matters beyond display:
        # `processing.editions` uses exactly those two flags to decide whether a date may
        # separate two runs of a series.
        setattr(
            facts,
            name,
            DateFact(
                value=datetime.fromisoformat(value),
                precision=DatePrecision.DATE,
                year_inferred=False,
                confidence=1.0,
                evidence="set by hand from the dashboard",
            ),
        )

    return write


def _write_title(facts: EventFacts, value: Any) -> None:
    facts.title = value
    facts.normalized_title = normalize_title(value)


FIELDS: dict[str, FieldSpec] = {
    "title": FieldSpec(_required_text, lambda f: f.title, _write_title),
    "organizer": FieldSpec(_text, lambda f: f.organizer, lambda f, v: setattr(f, "organizer", v)),
    "category": FieldSpec(
        _category,
        lambda f: f.category.value,
        lambda f, v: setattr(f, "category", CompetitionCategory(v)),
    ),
    "canonical_url": FieldSpec(
        _url, lambda f: f.canonical_url, lambda f, v: setattr(f, "canonical_url", v)
    ),
    "registration_url": FieldSpec(
        _url, lambda f: f.registration_url, lambda f, v: setattr(f, "registration_url", v)
    ),
    "event_start": FieldSpec(_date, _read_date("event_start"), _write_date("event_start")),
    "registration_deadline": FieldSpec(
        _date, _read_date("registration_deadline"), _write_date("registration_deadline")
    ),
    "city": FieldSpec(
        _text,
        lambda f: f.location.city,
        lambda f, v: setattr(f.location, "city", v),
    ),
    "country": FieldSpec(
        _text,
        lambda f: f.location.country,
        lambda f, v: setattr(f.location, "country", v),
    ),
}

# Lives on the event row rather than in EventFacts, so it is applied separately.
ROW_FIELDS: dict[str, Callable[[Any], Any]] = {"relevance_score": _score}

EDITABLE = frozenset(FIELDS) | frozenset(ROW_FIELDS)


def parse_edits(edits: dict[str, Any]) -> dict[str, Any]:
    """Validate an incoming edit, raising EditError naming the offending field."""
    unknown = sorted(set(edits) - EDITABLE)
    if unknown:
        raise EditError(f"cannot edit {', '.join(unknown)}")
    parsed: dict[str, Any] = {}
    for name, value in edits.items():
        parser = FIELDS[name].parse if name in FIELDS else ROW_FIELDS[name]
        try:
            parsed[name] = parser(value)
        except EditError as exc:
            raise EditError(f"{name}: {exc}") from exc
    return parsed


def apply_overrides(facts: EventFacts, overrides: dict[str, Any]) -> EventFacts:
    """Put pinned values back over freshly extracted facts.

    Called on every refresh. Without it a correction is silently reverted the next time
    the source page is read, which is at most 24 hours.
    """
    if not overrides:
        return facts
    updated = facts.model_copy(deep=True)
    for name, value in overrides.items():
        spec = FIELDS.get(name)
        if spec is not None:
            spec.write(updated, value)
    return updated


def current_values(facts: EventFacts) -> dict[str, Any]:
    """What each editable field holds now, for the dashboard's form."""
    return {name: spec.read(facts) for name, spec in FIELDS.items()}
