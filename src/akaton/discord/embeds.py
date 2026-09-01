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


# Short facts, rendered side by side. Discord lays inline fields out three to a row, so
# these read as a compact grid instead of a column of one-line paragraphs — which is what
# every field being full width had made of the alert.
INLINE_FIELDS = ("Category", "Location", "Team size", "Prize", "Posted")
# Long enough to be worth a full row of its own even if it is nominally a short fact.
INLINE_VALUE_LIMIT = 44


def _discord_time(value: datetime | None) -> str | None:
    """Discord's own timestamp markup: each reader's timezone, plus a live countdown.

    `<t:…:D>` renders as "5 October 2026" and `<t:…:R>` as "in 34 days", both computed by
    the client. That is strictly better than baking Manila time into the text, and the
    countdown is the single most useful thing an alert about a deadline can say.
    """
    if value is None:
        return None
    stamp = int(value.timestamp())
    return f"<t:{stamp}:D> (<t:{stamp}:R>)"


def embed_dict(payload: NotificationPayload) -> dict:
    """The alert, as a Discord embed payload.

    One renderer for the gateway notifier and the REST backfill tool, which had drifted
    apart, so every safety rule below is written once.

    Two kinds of text meet here and are treated differently. Anything scraped is escaped,
    because Discord renders markdown inside embeds and a post containing a link would
    otherwise produce one the reader cannot tell from ours. Anything this function
    generates — timestamp markup, our own links — is emitted as written.
    """
    social = payload.source_kind == "social_post"
    color = (
        SOCIAL_COLOR
        if social
        else HIGH_PRIORITY_COLOR
        if payload.relevance_tier == "HIGH_PRIORITY"
        else DEFAULT_COLOR
    )
    fields: list[dict] = []

    def add(name: str, value: str | None, *, inline: bool = False) -> None:
        # An absent fact is left out rather than printed as "Not specified". Nine rows of
        # that was most of the noise, and it buried the facts that were actually known.
        if value:
            fields.append({"name": name[:256], "value": value[:1024], "inline": inline})

    # Dates first: they are what the reader is deciding on, and they are ours to render.
    add("📅 Event date", _discord_time(payload.event_start), inline=True)
    add("⏳ Registration closes", _discord_time(payload.deadline), inline=True)

    for name, value in payload.fields.items():
        rendered = _escape(value)
        inline = name in INLINE_FIELDS and len(rendered) <= INLINE_VALUE_LIMIT
        add(name, rendered, inline=inline)

    links = []
    if payload.registration_url:
        links.append(f"[Register]({payload.registration_url})")
    if payload.official_url and not social:
        links.append(f"[Official announcement]({payload.official_url})")
    add("Links", " · ".join(links))

    if payload.source_label:
        clickable = payload.source_url and link_trust(payload.source_url) is LinkTrust.CLICKABLE
        add(
            "Source",
            f"[{_escape(payload.source_label)}]({payload.source_url})"
            if clickable
            else _escape(payload.source_label),
        )
    add("Links mentioned", payload.links_field)
    add("Note", _escape(payload.evidence_note))

    embed: dict = {
        "title": _escape(payload.title)[:256],
        "description": _escape(payload.description)[:4096],
        "color": color,
        "fields": fields[:25],
        # Relevance and confidence are context for the alert, not facts about the event,
        # so they belong beside the identifying token rather than taking two field slots.
        # The token stays in the text because reconciliation looks for it here.
        "footer": {
            "text": (
                f"{payload.relevance_tier.replace('_', ' ').title()} · "
                f"{payload.confidence_label} confidence · {payload.footer_token}"
            )[:2048]
        },
    }
    if payload.author_name:
        author: dict = {"name": _escape(payload.author_name)[:256]}
        if payload.author_icon_url:
            author["icon_url"] = payload.author_icon_url
        if payload.author_url:
            author["url"] = payload.author_url
        embed["author"] = author
    if payload.image_url:
        embed["image"] = {"url": payload.image_url}
    # The event's own date is the meaningful timestamp; falling back to "now" would just
    # restate when the alert was sent, which Discord already shows.
    if payload.event_start:
        embed["timestamp"] = payload.event_start.isoformat()
    # A clickable title is an endorsement of the destination.
    if payload.official_url and payload.official_url_clickable:
        embed["url"] = payload.official_url
    return embed


def organizer_for_url(url: str | None, sources: dict | None = None) -> str | None:
    """The organizer that owns this domain, from config/sources.yaml.

    A page on dict.gov.ph is run by DICT whether or not its prose ever says so, and
    deterministic extraction frequently cannot tell — it looks for an organizer in the
    text and government pages rarely introduce themselves. Without this the alert has no
    author line, and therefore no logo.
    """
    host = host_of(url or "")
    if not host:
        return None
    for organizer in (sources or {}).get("organizers", []):
        if not organizer.get("enabled", True):
            continue
        for domain in organizer.get("domains", []):
            domain = str(domain).casefold()
            if host == domain or host.endswith(f".{domain}"):
                aliases = organizer.get("aliases") or []
                return str(aliases[0] if aliases else organizer.get("name") or "")
    return None


ELIGIBILITY_LIMIT = 200


def summarise_eligibility(text: str | None) -> str | None:
    """One sentence, not the announcement again.

    `extract_eligibility` gathers every sentence containing an eligibility marker, which
    on a well-written announcement is most of the page — so this field was reprinting the
    description underneath it.
    """
    if not text:
        return None
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0].strip()
    if len(first) > ELIGIBILITY_LIMIT:
        first = first[:ELIGIBILITY_LIMIT].rsplit(" ", 1)[0] + "…"
    return first or None


def organizer_icon(url: str | None, sources: dict | None = None) -> str | None:
    """A small logo for the organizer, or None.

    Prefers a `logo:` configured against the organizer in config/sources.yaml, and falls
    back to the site's own `/favicon.ico`. Both are on the organizer's own domain, so
    nothing here tells a third party what Akaton is looking at — which a favicon service
    would. Discord fetches it server-side and simply shows nothing if it 404s, so a
    guessed favicon path costs nothing when it is wrong.
    """
    if not url:
        return None
    host = host_of(url)
    if not host:
        return None
    for organizer in (sources or {}).get("organizers", []):
        for domain in organizer.get("domains", []):
            domain = str(domain).casefold()
            if host == domain or host.endswith(f".{domain}"):
                configured = organizer.get("logo")
                if configured:
                    return str(configured)
    # Only for hosts already trusted enough to link to, so an arbitrary scraped domain
    # cannot put its artwork in our alert.
    if link_trust(url, sources) is not LinkTrust.CLICKABLE:
        return None
    return f"https://{host}/favicon.ico"


def displayable_image(url: str | None, sources: dict | None = None) -> str | None:
    """An image is a link the reader cannot inspect before it renders, so it is judged
    by the same host trust as one.

    A banner from a page we would happily link to is the event's own poster. A banner
    from an arbitrary scraped host is an image chosen by whoever wrote that page, shown
    full width in a channel, with no way for the reader to see where it came from first.
    """
    if not url:
        return None
    return url if link_trust(url, sources) is LinkTrust.CLICKABLE else None


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
    # Only facts we actually have. An absent one is omitted rather than rendered as
    # "Not specified", and the dates are carried on the payload instead of being
    # formatted here, so the renderer can use Discord's own timestamp markup.
    fields = {
        name: value
        for name, value in (
            # Order matters: Discord flows inline fields three to a row, so the first
            # three shown are the two dates plus this one. Where it is beats what kind
            # it is when someone is deciding whether to open the alert.
            ("Location", location),
            ("Category", facts.category.value.replace("_", " ").title()),
            ("Team size", _team_size(facts.team_size_min, facts.team_size_max)),
            ("Prize", facts.prize_information),
            ("Eligibility", summarise_eligibility(facts.eligibility.text)),
            ("Why this matched", " + ".join(score.match_reasons[:4])),
        )
        if value and value != "Not specified"
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
        # Links already shown as their own buttons are not repeated here.
        links_field=render_links(
            [url for url in (links or []) if url not in {registration, facts.canonical_url}],
            sources,
        ),
        evidence_note=note,
        author_name=facts.organizer or organizer_for_url(facts.canonical_url, sources),
        author_icon_url=organizer_icon(facts.canonical_url, sources),
        author_url=facts.canonical_url
        if link_trust(facts.canonical_url or "", sources) is LinkTrust.CLICKABLE
        else None,
        # Decided here, where the sources config is available, for the same reason
        # `official_url_clickable` is: the reconciliation path re-renders from the stored
        # payload and must not be able to reach a different verdict about what to show.
        image_url=displayable_image(facts.image_url, sources),
        event_start=facts.event_start.value,
        deadline=facts.registration_deadline.value,
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
