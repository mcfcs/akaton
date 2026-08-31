from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from akaton.domain.enums import LinkTrust
from akaton.domain.models import EventFacts, NotificationPayload, ScoringResult
from akaton.persistence.models import EventChangeRow
from akaton.processing.links import host_of, is_shortener, link_trust

SUMMARY_TERMS = (
    "hackathon",
    "competition",
    "challenge",
    "ideathon",
    "datathon",
    "registration",
    "register",
    "apply",
    "deadline",
    "students",
    "teams",
    "prize",
    "innovation",
)


def _is_prose(sentence: str) -> bool:
    """Reject nav crumbs and countdown widgets such as "00 days hrs mins secs"."""
    if len(sentence) < 40:
        return False
    letters = sum(character.isalpha() or character.isspace() for character in sentence)
    return letters >= 0.75 * len(sentence)


def summarise(text: str | None, limit: int = 600, *, prefer_head: bool = False) -> str:
    """Pick the sentences that describe the competition.

    Page text starts with menus, cookie notices and countdown timers, so quoting it from
    the top puts boilerplate in the alert where the description should be.
    """
    if not text:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text)]
    prose = [sentence for sentence in sentences if _is_prose(sentence)]
    # A scraped page buries its announcement under navigation, so the keyword-bearing
    # sentences are the useful ones. A social post is already a summary and opens with
    # the announcement, so reordering it only scrambles the reading.
    if prefer_head:
        chosen = prose
    else:
        chosen = [
            sentence
            for sentence in prose
            if any(term in sentence.casefold() for term in SUMMARY_TERMS)
        ] or prose
    summary = ""
    for sentence in chosen:
        if len(summary) + len(sentence) + 1 > limit:
            break
        summary = f"{summary} {sentence}".strip()
    return summary


SOCIAL_COLOR = 0xE67E22
HIGH_PRIORITY_COLOR = 0xE74C3C
DEFAULT_COLOR = 0x2ECC71
MAX_RENDERED_LINKS = 5


def _escape(value: str | None) -> str:
    """Neutralise markdown in scraped text.

    Discord renders markdown inside embed descriptions and field values, so a post
    containing `[Click here](https://evil.example)` would otherwise produce a link the
    reader cannot tell from ours. AllowedMentions stops pings; it does nothing here.
    """
    return discord.utils.escape_markdown(value or "")


def render_links(urls: list[str] | None, sources: dict | None = None) -> str | None:
    """Render scraped links according to how far each host is trusted.

    Trusted hosts become markdown links. Everything else is shown in backticks, because
    Discord auto-linkifies a bare URL in an embed field and backticks are what actually
    make plain text plain. Nothing is hidden except platform chrome.
    """
    rendered: list[str] = []
    for url in urls or []:
        trust = link_trust(url, sources)
        if trust is LinkTrust.DROP:
            continue
        if trust is LinkTrust.CLICKABLE:
            rendered.append(f"[{_escape(host_of(url) or url)}]({url})")
        else:
            suffix = " (shortened link)" if is_shortener(url) else ""
            rendered.append(f"`{url}`{suffix}")
        if len(rendered) >= MAX_RENDERED_LINKS:
            break
    if not rendered:
        return None
    return "\n".join(rendered)[:1024]


def embed_dict(payload: NotificationPayload) -> dict:
    """The alert, as a Discord embed payload.

    One renderer for the gateway notifier and the REST backfill tool, which had drifted
    apart, so every safety rule below is written once.
    """
    social = payload.source_kind == "social_post"
    color = (
        SOCIAL_COLOR
        if social
        else HIGH_PRIORITY_COLOR
        if payload.relevance_tier == "HIGH_PRIORITY"
        else DEFAULT_COLOR
    )
    fields = [
        {"name": name[:256], "value": _escape(value)[:1024] or "Not specified", "inline": False}
        for name, value in payload.fields.items()
    ]
    if payload.source_label:
        source = payload.source_label
        if payload.source_url and link_trust(payload.source_url) is LinkTrust.CLICKABLE:
            source = f"[{_escape(payload.source_label)}]({payload.source_url})"
        else:
            source = _escape(source)
        fields.append({"name": "Source", "value": source[:1024], "inline": False})
    if payload.links_field:
        fields.append(
            {"name": "Links mentioned", "value": payload.links_field[:1024], "inline": False}
        )
    if payload.evidence_note:
        fields.append(
            {"name": "Note", "value": _escape(payload.evidence_note)[:1024], "inline": False}
        )
    links = []
    if payload.registration_url:
        links.append(f"[Register]({payload.registration_url})")
    if payload.official_url and not social:
        links.append(f"[Official announcement]({payload.official_url})")
    if links:
        fields.append({"name": "Links", "value": " · ".join(links), "inline": False})
    fields.append({"name": "Confidence", "value": payload.confidence_label, "inline": True})
    fields.append(
        {
            "name": "Relevance",
            "value": payload.relevance_tier.replace("_", " ").title(),
            "inline": True,
        }
    )
    embed: dict = {
        "title": _escape(payload.title)[:256],
        "description": _escape(payload.description)[:4096],
        "color": color,
        "fields": fields,
        "footer": {"text": payload.footer_token},
    }
    # A clickable title is an endorsement of the destination.
    if payload.official_url and payload.official_url_clickable:
        embed["url"] = payload.official_url
    return embed


def _format_date(value: datetime | None) -> str:
    return (
        value.astimezone(ZoneInfo("Asia/Manila")).strftime("%b %d, %Y")
        if value
        else "Not specified"
    )


def build_new_event_payload(
    event_id: int,
    event_version: int,
    facts: EventFacts,
    score: ScoringResult,
    confidence: float,
    *,
    discovery_channel: str | None = None,
    source_label: str | None = None,
    source_url: str | None = None,
    links: list[str] | None = None,
    published: datetime | None = None,
    sources: dict | None = None,
) -> NotificationPayload:
    social = discovery_channel in {"facebook", "reddit"}
    location = " — ".join(filter(None, (facts.location.city, facts.location.region)))
    if facts.location.location_type.value == "ONLINE":
        location = (
            "Online — Philippines eligible" if facts.eligibility.philippines_allowed else "Online"
        )
    fields = {
        "Category": facts.category.value.replace("_", " ").title(),
        "Organizer": facts.organizer or "Not specified",
        "Location": location or "Not specified",
        "Registration deadline": _format_date(facts.registration_deadline.value),
        "Event date": _format_date(facts.event_start.value),
        "Eligibility": (facts.eligibility.text or "Not specified")[:1024],
        "Team size": _team_size(facts.team_size_min, facts.team_size_max),
        "Prize": facts.prize_information or "Not specified",
        "Why this matched": " + ".join(score.match_reasons[:4]) or "Passed configured preferences",
    }
    if social and published:
        fields["Posted"] = _format_date(published)
    registration = facts.registration_url
    note = None
    if social and not registration:
        # Most social announcements carry no registration link at all. Saying so is
        # information; pointing a "Register" button at the post itself is a hazard.
        note = "No registration link in the post — open the source to check."
    label = "High" if confidence >= 0.85 else "Medium" if confidence >= 0.75 else "Low"
    return NotificationPayload(
        dedupe_key=f"new:{event_id}",
        notification_type="NEW_EVENT",
        event_id=event_id,
        event_version=event_version,
        title=facts.title or "Untitled competition",
        # A social post is already a summary and leads with its announcement, unlike a
        # web page that opens with navigation.
        description=summarise(facts.description, prefer_head=social),
        fields=fields,
        official_url=facts.canonical_url,
        registration_url=registration,
        footer_token=f"akaton:{event_id}:{event_version}:new",
        relevance_tier=score.tier,
        confidence_label=label,
        source_kind="social_post" if social else "official",
        official_url_clickable=bool(facts.canonical_url)
        and link_trust(facts.canonical_url, sources) is LinkTrust.CLICKABLE,
        source_label=source_label,
        # For a social post the source is the post itself. For a page found by resolving
        # a mention it is the thread that mentioned it — the caller passes that in, since
        # the canonical URL here is the official page and is already rendered above.
        source_url=source_url or (facts.canonical_url if social else None),
        links_field=render_links(links, sources),
        evidence_note=note,
    )


def build_change_payload(
    event_id: int,
    event_version: int,
    facts: EventFacts,
    changes: list[EventChangeRow],
) -> NotificationPayload:
    change_ids = ",".join(str(change.id) for change in changes)
    fields = {
        change.change_type.replace("_", " ").title(): (
            f"{_display_change(change.before_json)} → {_display_change(change.after_json)}"
        )[:1024]
        for change in changes
    }
    return NotificationPayload(
        dedupe_key=f"change:{event_id}:{change_ids}",
        notification_type=changes[0].change_type if len(changes) == 1 else "EVENT_UPDATED",
        event_id=event_id,
        event_version=event_version,
        title=f"Updated: {facts.title or 'Competition'}",
        description="An authoritative source reported a meaningful event update.",
        fields=fields,
        official_url=facts.canonical_url,
        registration_url=facts.registration_url,
        footer_token=f"akaton:{event_id}:{event_version}:change:{change_ids}",
        relevance_tier="UPDATE",
        confidence_label="High",
    )


def _display_change(value: object) -> str:
    return "Not specified" if value is None else str(value)


def _team_size(minimum: int | None, maximum: int | None) -> str:
    if minimum and maximum and minimum != maximum:
        return f"{minimum}–{maximum}"
    if minimum:
        return str(minimum)
    if maximum:
        return f"Up to {maximum}"
    return "Not specified"
