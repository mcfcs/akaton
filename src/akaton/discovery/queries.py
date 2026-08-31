from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class ScheduledQuery:
    group: str
    query: str
    freshness: str | None
    cadence_hours: int
    weight: int


def configured_queries(config: dict) -> list[ScheduledQuery]:
    values: list[ScheduledQuery] = []
    for group, settings in config.get("groups", {}).items():
        for query in settings.get("queries", []):
            values.append(
                ScheduledQuery(
                    group=group,
                    query=query,
                    freshness=settings.get("freshness"),
                    cadence_hours=int(settings.get("cadence_hours", 24)),
                    weight=int(settings.get("weight", 1)),
                )
            )
    return values


def organizer_queries(config: dict) -> list[ScheduledQuery]:
    """Expand the per-organizer templates.

    Templates get `{name}`, `{alias}` and `{domain}`. `alias` is the organizer's first
    alias — the short form people actually write, "DICT" rather than "Department of
    Information and Communications Technology" — falling back to the full name.
    """
    values: list[ScheduledQuery] = []
    templates = config.get("query_templates", [])
    cadence = int(config.get("organizer_cadence_hours", 24))
    for organizer in config.get("organizers", []):
        if not organizer.get("enabled", True):
            continue
        domains = organizer.get("domains") or [""]
        aliases = organizer.get("aliases") or []
        alias = aliases[0] if aliases else organizer["name"]
        for template in templates:
            for domain in domains[:1]:
                values.append(
                    ScheduledQuery(
                        group="organizers",
                        query=template.format(name=organizer["name"], alias=alias, domain=domain),
                        freshness="pm",
                        cadence_hours=cadence,
                        weight=3,
                    )
                )
    return values


def choose_due_queries(
    queries: list[ScheduledQuery],
    history: dict[tuple[str, str], datetime],
    count: int,
    *,
    now: datetime | None = None,
) -> list[ScheduledQuery]:
    now = now or datetime.now(UTC)

    def priority(item: ScheduledQuery) -> tuple[float, int, str]:
        last = history.get((item.group, item.query))
        if last is None:
            overdue = 10_000.0
        else:
            due = last + timedelta(hours=item.cadence_hours)
            overdue = (now - due).total_seconds() / 3600
        return overdue, item.weight, item.query

    due = [
        item
        for item in queries
        if (item.group, item.query) not in history
        or history[(item.group, item.query)] + timedelta(hours=item.cadence_hours) <= now
    ]
    return sorted(due, key=priority, reverse=True)[:count]
