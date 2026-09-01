"""Parse Facebook group posts and decide which ones are real competitions.

Facebook's markup is generated CSS with no stable class names, so this module
never keys off those. It accepts three shapes of input that a headed session
can actually produce:

1. Records from a small DOM evaluator (`[role="article"]` text + hrefs).
2. Relay hydration JSON embedded in `<script type="application/json">` tags.
3. Concatenated `/api/graphql` response bodies intercepted while the page loads.

Identification is stricter than the page classifier. A post that merely says
"any upcoming hackathon?" is not an event; a reply under it that names one and
links out is. That is the philhacks pattern.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from akaton.domain.models import CandidateSeed, MentionLead
from akaton.processing.links import should_follow_url
from akaton.processing.mentions import build_mention, classify_mention
from akaton.processing.normalize import fold_text

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "web.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "l.facebook.com",
    "lm.facebook.com",
    "fb.com",
    "www.fb.com",
    "fb.me",
    "fbcdn.net",
}
SKIP_HOSTS = FACEBOOK_HOSTS | {
    "instagram.com",
    "www.instagram.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "www.tiktok.com",
    "messenger.com",
    "www.messenger.com",
}
LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
TRAILING_PUNCT_RE = re.compile(r"[),.]+$")
RELATIVE_TIME_RE = re.compile(r"^\d+\s*[smhdwy]$", re.IGNORECASE)
POST_ID_RE = re.compile(
    r"/(?:permalink|posts|videos|reel)/(\d{5,})|"
    r"[?&](?:story_fbid|multi_permalinks|fbid)=(\d{5,})",
    re.IGNORECASE,
)
COMMENT_ID_RE = re.compile(r"[?&]comment_id=(\d{5,}|fb_[0-9a-f]{8,})", re.IGNORECASE)
GROUP_SLUG_RE = re.compile(r"facebook\.com/groups/([^/?#]+)", re.IGNORECASE)
# A page slug is whatever follows the host, excluding the site's own sections.
PAGE_SLUG_RE = re.compile(r"facebook\.com/(?!groups/)([^/?#]+)", re.IGNORECASE)

# Meta's own account and security notices render into `[role="article"]` elements that
# look exactly like comments to the DOM scraper. A real run captured "You're now using a
# Meta Account on Facebook." and "We noticed a new login from a device or location you
# don't usually use" as replies, dragging accountscenter.facebook.com links in with them.
PLATFORM_CHROME_MARKERS = (
    "meta account",
    "meta accounts are coming",
    "you're now using a meta account",
    "we noticed a new login",
    "a device or location you don't usually use",
    "review recent login",
    "was this you",
    "your account has been",
    "log in to continue",
    "see new posts",
)


def is_platform_chrome(text: str) -> bool:
    """True for Facebook's own account/security notices scraped as if they were replies."""
    lowered = fold_text(text).casefold()
    return any(marker in lowered for marker in PLATFORM_CHROME_MARKERS)


CHROME_LINES = {
    "like",
    "love",
    "haha",
    "wow",
    "sad",
    "angry",
    "care",
    "reply",
    "share",
    "see translation",
    "see more",
    "see less",
    "write a comment",
    "most relevant",
    "all comments",
    "newest",
    "comment",
    "comments",
    "follow",
    "following",
    "join group",
    "joined",
}


@dataclass
class FacebookComment:
    comment_id: str
    text: str
    urls: list[str] = field(default_factory=list)
    author: str | None = None
    created_at: datetime | None = None
    permalink: str | None = None


@dataclass
class FacebookPost:
    post_id: str
    group: str
    permalink: str
    text: str
    urls: list[str] = field(default_factory=list)
    author: str | None = None
    created_at: datetime | None = None
    comments: list[FacebookComment] = field(default_factory=list)
    comment_count: int = 0
    # Which shape of target this came from, so a fallback permalink is built the right
    # way round. Defaults to "group" because that is what every existing caller means.
    kind: str = "group"


@dataclass(frozen=True)
class GroupTarget:
    """Something on Facebook worth reading: a group's feed, or an organizer's page.

    A page is the same DOM — `[role="feed"]` of `[role="article"]` elements — reached by a
    different URL and without a Join button, so the collector treats both the same way and
    only the URL builders differ.
    """

    url: str
    name: str
    location: str = "Philippines"
    kind: str = "group"


def normalize_group_identifier(value: str) -> str:
    match = GROUP_SLUG_RE.search(value.strip())
    if match:
        return match.group(1).strip("/")
    return value.strip().strip("/")


def normalize_page_identifier(value: str) -> str:
    match = PAGE_SLUG_RE.search(value.strip())
    if match:
        return match.group(1).strip("/")
    return value.strip().strip("/")


def group_feed_url(group: str) -> str:
    slug = normalize_group_identifier(group)
    return f"https://www.facebook.com/groups/{slug}/?sorting_setting=CHRONOLOGICAL"


def page_feed_url(page: str) -> str:
    return f"https://www.facebook.com/{normalize_page_identifier(page)}"


def feed_url(target: GroupTarget) -> str:
    return page_feed_url(target.url) if target.kind == "page" else group_feed_url(target.url)


def permalink_url(
    group: str, post_id: str, *, comment_id: str | None = None, kind: str = "group"
) -> str:
    """A fallback link for a post whose own permalink the DOM did not give us.

    A page's posts live at `/<page>/posts/<id>`, not `/groups/<slug>/permalink/<id>`, and
    a link built the wrong way round resolves to nothing while still looking plausible.
    """
    if kind == "page":
        url = f"https://www.facebook.com/{normalize_page_identifier(group)}/posts/{post_id}/"
    else:
        url = f"https://www.facebook.com/groups/{normalize_group_identifier(group)}/permalink/{post_id}/"
    if comment_id:
        return f"{url}?comment_id={comment_id}"
    return url


def post_id_from_url(url: str) -> str | None:
    match = POST_ID_RE.search(url)
    if not match:
        return None
    return match.group(1) or match.group(2)


def comment_id_from_url(url: str) -> str | None:
    match = COMMENT_ID_RE.search(url)
    return match.group(1) if match else None


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold()


def is_facebook_url(url: str) -> bool:
    host = host_of(url)
    return host in FACEBOOK_HOSTS or host.endswith(".facebook.com") or host.endswith(".fbcdn.net")


def unwrap_facebook_url(url: str) -> str:
    """Follow l.facebook.com/l.php?u=... wrappers onto the real destination."""
    current = url.strip()
    for _ in range(3):
        parts = urlsplit(current)
        host = (parts.hostname or "").casefold()
        if host not in {"l.facebook.com", "lm.facebook.com"}:
            return current
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        target = query.get("u")
        if not target:
            return current
        current = unquote(target)
    return current


def _trim_url(url: str) -> str:
    return TRAILING_PUNCT_RE.sub("", url.strip())


def extract_urls(*blobs: str | Iterable[str] | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        trimmed = _trim_url(raw)
        if not trimmed.startswith(("http://", "https://")):
            return
        unwrapped = unwrap_facebook_url(trimmed)
        if unwrapped not in seen:
            seen.add(unwrapped)
            found.append(unwrapped)

    for blob in blobs:
        if blob is None:
            continue
        if isinstance(blob, str):
            for match in LINK_RE.findall(blob):
                add(match)
            continue
        for item in blob:
            if item:
                add(str(item))
    return found


def outbound_urls(urls: Iterable[str]) -> list[str]:
    outbound: list[str] = []
    for url in urls:
        host = host_of(url)
        if not host or host in SKIP_HOSTS or host.endswith(".facebook.com"):
            # Facebook Event pages are the event, not chrome.
            if "/events/" in url and is_facebook_url(url) and "/groups/" not in url:
                outbound.append(url)
            continue
        outbound.append(url)
    return outbound


def clean_facebook_text(text: str, *, author: str | None = None) -> str:
    """Strip Facebook chrome and fold styled characters onto ASCII.

    Every post and comment body passes through here before `mention_kind`, `_assemble`
    and `_title_from`, so folding once at this point is what lets the shared classifier
    read a post written as ð—¥ð—˜ð—šð—œð—¦ð—§ð—¥ð—”ð—§ð—œð—¢ð—¡ ð—œð—¦ ð—¡ð—¢ð—ª ð—¢ð—£ð—˜ð—¡.
    """
    lines: list[str] = []
    author_key = (author or "").casefold()
    for raw in fold_text(text).replace("\r", "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.casefold()
        if lowered in CHROME_LINES:
            continue
        if RELATIVE_TIME_RE.fullmatch(lowered.replace(" ", "")):
            continue
        if author_key and lowered == author_key:
            continue
        if re.fullmatch(r"\d+", line) and len(line) <= 4:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def parse_facebook_time(value: Any, *, now: datetime | None = None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        return parse_facebook_time(int(text), now=now)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text.replace("Z", "+00:00"), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def thread_to_mentions(
    post: FacebookPost,
    *,
    cutoff: datetime | None = None,
    vocabulary: frozenset[str] = frozenset(),
) -> list[MentionLead]:
    """Leads from a thread that talks about a competition without announcing one.

    These are the posts `thread_to_seeds` drops on the floor: a question, a teammate
    search, a post-mortem. Each names something real, and the name is worth one search.
    The thread itself never becomes the candidate — that is the mistake this replaces.
    """
    if post.created_at and cutoff and post.created_at < cutoff:
        return []
    mentions: list[MentionLead] = []
    parts = [(post.text, post.urls, post.permalink, f"fb:{post.group}:{post.post_id}")]
    for comment in post.comments:
        comment_id = comment.comment_id or _synthetic_comment_id(comment.text)
        parts.append(
            (
                comment.text,
                comment.urls,
                comment.permalink or post.permalink,
                f"fb:{post.group}:{post.post_id}:{comment_id}",
            )
        )
    for text, urls, source_url, source_key in parts:
        body = clean_facebook_text(text)
        mention = build_mention(
            body,
            kind=mention_kind(body, urls),
            platform="facebook",
            source_url=source_url,
            source_key=source_key,
            vocabulary=vocabulary,
        )
        if mention:
            mentions.append(mention)
    return mentions


def mention_kind(text: str, urls: Iterable[str] | None = None) -> str:
    """Classify one Facebook post or reply.

    Facebook's half of the job: strip its chrome, fold its styled Unicode, and apply its
    `/events/` carve-out to the link list. The judgement itself is shared with Reddit and
    lives in `processing.mentions`.
    """
    body = clean_facebook_text(text)
    if not body:
        return "unrelated"
    return classify_mention(body, outbound_urls(urls or extract_urls(body)))


def needs_comment_expansion(post: FacebookPost) -> bool:
    # Only in a group. Opening a thread is worth it there because that is the philhacks
    # pattern — someone asks, someone else replies with the real listing. On an
    # organizer's own page the announcement *is* the post, nobody drops a competing
    # listing underneath it, and the timeline is mostly marketing that classifies as
    # "unrelated" — which is one of the kinds below, so every one of those posts would
    # spend a permalink and several seconds to read replies that say nothing.
    if post.kind == "page":
        return False
    kind = mention_kind(post.text, post.urls)
    # A question or teammate thread is exactly where a reply carries the real listing.
    # A recap, job or foreign post has nothing worth opening, and opening it would spend
    # the run's permalink budget on threads that can never produce a candidate.
    if kind in {"recap", "job", "foreign"}:
        return False
    if kind in {"question", "question_with_link", "teammate", "unrelated"}:
        return True
    return post.comment_count > 0 or bool(post.comments)


def _synthetic_comment_id(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"fb_{digest}"


def _title_from(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "Facebook group post"
    return line[:180]


def _assemble(
    post: FacebookPost,
    extra: FacebookComment | None,
    *,
    location: str,
) -> str:
    # The group's configured location is deliberately NOT written into the document.
    # Naming the country here made `extract_location` read it off this preamble rather
    # than off the post, so a Malaysian announcement came out as country="PH" at 0.75
    # confidence: it cleared the eligibility gate, earned a free confidence signal, and
    # collected 38 scoring points it had not earned.
    # The post's own words lead. Attribution goes last: the alert quotes the head of
    # this document, and a harness preamble there would be the first thing read.
    parts = []
    if post.author:
        parts.append(f"{post.author}: {post.text}".strip())
    else:
        parts.append(post.text)
    comments = list(post.comments)
    if extra and extra not in comments:
        comments.append(extra)
    # Only comments that look like an event join the document. Otherwise marketplace
    # listings and promo replies drag their links in, and those links then become
    # candidate registration URLs.
    relevant = [
        comment
        for comment in comments
        if comment is extra or mention_kind(comment.text, comment.urls) != "unrelated"
    ]
    if relevant:
        parts.append("Comments:")
        for comment in relevant:
            label = comment.author or "Comment"
            parts.append(f"{label}: {comment.text}")
            for url in outbound_urls(comment.urls):
                if url not in comment.text:
                    parts.append(url)
    for url in outbound_urls(post.urls):
        if url not in "\n".join(parts):
            parts.append(url)
    parts.append(f"Source: Facebook group post in {post.group}.")
    return "\n".join(part for part in parts if part).strip()


def _seed(
    *,
    url: str,
    title: str,
    snippet: str,
    content: str | None,
    links: list[str],
    source_key: str,
    published: datetime | None,
    provider: str,
    query: str | None,
) -> CandidateSeed | None:
    try:
        return CandidateSeed(
            url=url,
            title=title[:180] or None,
            snippet=snippet[:500] or None,
            discovery_channel="facebook",
            provider=provider,
            query=query,
            source_key=source_key[:128],
            published_hint=published,
            content=content,
            links=links,
        )
    except ValueError:
        return None


def _seeds_for_mention(
    *,
    post: FacebookPost,
    mention_text: str,
    mention_urls: list[str],
    source_key: str,
    seed_url: str | None,
    published: datetime | None,
    extra_comment: FacebookComment | None,
    location: str,
    provider: str,
    query: str | None,
    link_only: bool = False,
) -> list[CandidateSeed]:
    outbound = outbound_urls(mention_urls)
    followable = list(dict.fromkeys(url for url in outbound if should_follow_url(url)))
    if link_only and not followable:
        # A question thread is only worth the listing it points at, never itself.
        return []
    assembled = _assemble(post, extra_comment, location=location)
    title = _title_from(mention_text)
    snippet = mention_text[:500]
    seeds: list[CandidateSeed] = []
    if followable:
        for url in followable:
            seed = _seed(
                url=url,
                title=title,
                snippet=snippet,
                content=None,
                links=[],
                source_key=source_key,
                published=published,
                provider=provider,
                query=query,
            )
            if seed:
                seeds.append(seed)
        return seeds
    seed = _seed(
        url=seed_url or post.permalink,
        title=title,
        snippet=snippet,
        content=assembled,
        links=outbound,
        source_key=source_key,
        published=published,
        provider=provider,
        query=query,
    )
    return [seed] if seed else []


def thread_to_seeds(
    post: FacebookPost,
    *,
    cutoff: datetime | None = None,
    location: str = "Philippines",
    provider: str = "facebook",
    query: str | None = None,
) -> list[CandidateSeed]:
    """Turn one thread into zero or more candidates.

    The post and every reply are classified independently. A question post is
    not emitted; a reply that actually names a competition is.
    """
    if post.created_at and cutoff and post.created_at < cutoff:
        return []
    seeds: list[CandidateSeed] = []
    seen_urls: set[str] = set()

    def take(items: list[CandidateSeed]) -> None:
        for seed in items:
            key = str(seed.url)
            if key in seen_urls:
                continue
            seen_urls.add(key)
            seeds.append(seed)

    post_kind = mention_kind(post.text, post.urls)
    if post_kind in {"event", "question_with_link"}:
        take(
            _seeds_for_mention(
                post=post,
                mention_text=post.text,
                mention_urls=list(post.urls) + extract_urls(post.text),
                source_key=f"fb:{post.group}:{post.post_id}",
                seed_url=post.permalink,
                published=post.created_at,
                extra_comment=None,
                location=location,
                provider=provider,
                query=query,
                link_only=post_kind == "question_with_link",
            )
        )
    for comment in post.comments:
        if mention_kind(comment.text, comment.urls) != "event":
            continue
        comment_id = comment.comment_id or _synthetic_comment_id(comment.text)
        # A synthetic id is a hash of the reply text, so building a permalink around it
        # produces a URL that resolves to nothing yet would become the event's canonical
        # link. Fall back to the post's own permalink and keep the hash for identity.
        if comment.permalink:
            comment_permalink = comment.permalink
        elif comment_id.startswith("fb_"):
            comment_permalink = post.permalink
        else:
            comment_permalink = permalink_url(
                post.group, post.post_id, comment_id=comment_id, kind=post.kind
            )
        take(
            _seeds_for_mention(
                post=post,
                mention_text=comment.text,
                mention_urls=list(comment.urls) + extract_urls(comment.text),
                source_key=f"fb:{post.group}:{post.post_id}:c:{comment_id}"[:128],
                seed_url=comment_permalink,
                published=comment.created_at or post.created_at,
                extra_comment=comment,
                location=location,
                provider=provider,
                query=query,
            )
        )
    return seeds


def groups_from_config(raw: dict | None) -> tuple[GroupTarget, ...]:
    items = (raw or {}).get("groups") or (
        {
            "url": "https://www.facebook.com/groups/philhacks/",
            "name": "philhacks",
            "location": "Philippines",
        },
    )
    groups: list[GroupTarget] = []
    for item in items:
        if isinstance(item, str):
            slug = normalize_group_identifier(item)
            groups.append(GroupTarget(url=group_feed_url(slug), name=slug))
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        slug = str(item.get("name") or normalize_group_identifier(url))
        groups.append(
            GroupTarget(
                url=group_feed_url(url),
                name=slug,
                location=str(item.get("location") or "Philippines"),
            )
        )
    return tuple(groups)


def pages_from_config(raw: dict | None) -> tuple[GroupTarget, ...]:
    """Organizer pages to read, alongside the groups.

    Search finds these constantly and cannot read any of them: of 134 candidates rejected
    as SEARCH_SNIPPET_ONLY on one real run, 62 were facebook.com pages — including the
    GCash post announcing ImaGnation. Reading them is the difference between finding an
    announcement and merely knowing one exists.
    """
    pages: list[GroupTarget] = []
    for item in (raw or {}).get("pages") or ():
        if isinstance(item, str):
            slug = normalize_page_identifier(item)
            pages.append(GroupTarget(url=page_feed_url(slug), name=slug, kind="page"))
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        pages.append(
            GroupTarget(
                url=page_feed_url(url),
                name=str(item.get("name") or normalize_page_identifier(url)),
                location=str(item.get("location") or "Philippines"),
                kind="page",
            )
        )
    return tuple(pages)


def post_from_dom(record: dict, group: str, kind: str = "group") -> FacebookPost | None:
    hrefs = [str(item) for item in record.get("hrefs") or [] if item]
    permalink = (
        record.get("permalink")
        or next((href for href in hrefs if post_id_from_url(href)), None)
        or next((href for href in hrefs if "/permalink/" in href or "/posts/" in href), None)
    )
    post_id = str(record.get("post_id") or (post_id_from_url(permalink) if permalink else "") or "")
    text = clean_facebook_text(str(record.get("text") or ""), author=record.get("author"))
    if not post_id and not text:
        return None
    if not post_id:
        post_id = _synthetic_comment_id(text)
    if not permalink:
        permalink = permalink_url(group, post_id, kind=kind)
    elif permalink.startswith("/"):
        permalink = f"https://www.facebook.com{permalink}"
    urls = extract_urls(text, hrefs)
    created = parse_facebook_time(record.get("created_at") or record.get("creation_time"))
    comment_count = record.get("comment_count") or record.get("comments_count") or 0
    try:
        comment_count = int(comment_count)
    except (TypeError, ValueError):
        comment_count = 0
    identifier = (
        normalize_page_identifier(group) if kind == "page" else normalize_group_identifier(group)
    )
    return FacebookPost(
        post_id=post_id,
        group=identifier,
        permalink=permalink.split("?")[0],
        text=text,
        urls=urls,
        author=record.get("author"),
        created_at=created,
        comment_count=comment_count,
        kind=kind,
    )


def comments_from_dom(records: Iterable[dict], post: FacebookPost) -> list[FacebookComment]:
    comments: list[FacebookComment] = []
    seen: set[str] = set()
    for record in records:
        text = clean_facebook_text(str(record.get("text") or ""), author=record.get("author"))
        if len(text) < 8 or is_platform_chrome(text):
            continue
        hrefs = [str(item) for item in record.get("hrefs") or [] if item]
        comment_id = (
            str(record.get("comment_id") or "")
            or next(
                (comment_id_from_url(href) for href in hrefs if comment_id_from_url(href)), None
            )
            or _synthetic_comment_id(text)
        )
        if comment_id in seen:
            continue
        seen.add(comment_id)
        permalink = record.get("permalink") or next(
            (href for href in hrefs if comment_id_from_url(href)), None
        )
        comments.append(
            FacebookComment(
                comment_id=comment_id,
                text=text,
                urls=extract_urls(text, hrefs),
                author=record.get("author"),
                created_at=parse_facebook_time(record.get("created_at")),
                permalink=permalink
                or permalink_url(post.group, post.post_id, comment_id=comment_id, kind=post.kind),
            )
        )
    return comments


def merge_comments(*groups: Iterable[FacebookComment]) -> list[FacebookComment]:
    merged: dict[str, FacebookComment] = {}
    for group in groups:
        for comment in group:
            key = comment.comment_id or _synthetic_comment_id(comment.text)
            existing = merged.get(key)
            if existing is None:
                merged[key] = comment
                continue
            urls = list(dict.fromkeys([*existing.urls, *comment.urls]))
            text = existing.text if len(existing.text) >= len(comment.text) else comment.text
            merged[key] = FacebookComment(
                comment_id=existing.comment_id,
                text=text,
                urls=urls,
                author=existing.author or comment.author,
                created_at=existing.created_at or comment.created_at,
                permalink=existing.permalink or comment.permalink,
            )
    return list(merged.values())


def parse_json_payloads(text: str) -> list[Any]:
    """Decode one or more JSON values, including Facebook's concatenated GraphQL bodies."""
    payload = text.lstrip()
    if payload.startswith("for (;;);"):
        payload = payload[9:].lstrip()
    decoder = json.JSONDecoder()
    objects: list[Any] = []
    index = 0
    length = len(payload)
    while index < length:
        while index < length and payload[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            obj, end = decoder.raw_decode(payload, index)
        except json.JSONDecodeError:
            break
        objects.append(obj)
        index = end
    return objects


def _walk_collect(obj: Any, sink: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        text = None
        kind = None
        message = obj.get("message")
        body = obj.get("body")
        if isinstance(message, dict) and isinstance(message.get("text"), str):
            text = message["text"]
            kind = "post"
        elif isinstance(body, dict) and isinstance(body.get("text"), str):
            text = body["text"]
            kind = "comment"
        elif isinstance(obj.get("text"), str) and len(obj["text"]) > 40:
            text = obj["text"]
        urls: list[str] = []
        for key in (
            "url",
            "external_url",
            "unencrypted_www_url",
            "browser_native_url",
            "wwwURL",
        ):
            value = obj.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
        post_id = obj.get("post_id") or obj.get("legacy_api_post_id")
        if isinstance(post_id, (int, float)):
            post_id = str(int(post_id))
        if text and (post_id or urls or (kind == "comment" and len(text) > 20)):
            sink.append(
                {
                    "kind": kind or ("post" if post_id else "comment"),
                    "text": text,
                    "urls": urls,
                    "post_id": str(post_id) if post_id else None,
                    "comment_id": str(obj.get("id") or "") or None,
                    "created_at": obj.get("creation_time") or obj.get("created_time"),
                    "url": obj.get("url") if isinstance(obj.get("url"), str) else None,
                }
            )
        for value in obj.values():
            _walk_collect(value, sink)
    elif isinstance(obj, list):
        for item in obj:
            _walk_collect(item, sink)


def records_from_graphql(blobs: Iterable[str]) -> list[dict[str, Any]]:
    sink: list[dict[str, Any]] = []
    for blob in blobs:
        for obj in parse_json_payloads(blob):
            _walk_collect(obj, sink)
    return sink


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture = False
        self.scripts: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        mapping = {key: value or "" for key, value in attrs}
        self._capture = mapping.get("type", "").casefold() == "application/json"
        self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capture:
            text = "".join(self._buf).strip()
            if text.startswith("{") or text.startswith("["):
                self.scripts.append(text)
            self._capture = False
            self._buf = []


def records_from_html(html: str) -> list[dict[str, Any]]:
    parser = _ScriptCollector()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    sink: list[dict[str, Any]] = []
    for script in parser.scripts:
        for obj in parse_json_payloads(script):
            _walk_collect(obj, sink)
    return sink


def apply_graphql_records(post: FacebookPost, records: Iterable[dict[str, Any]]) -> FacebookPost:
    comments: list[FacebookComment] = []
    for record in records:
        text = clean_facebook_text(str(record.get("text") or ""))
        if not text or is_platform_chrome(text):
            continue
        urls = extract_urls(text, record.get("urls") or [])
        kind = record.get("kind")
        record_post_id = record.get("post_id")
        if kind == "post" and (not record_post_id or record_post_id == post.post_id):
            if len(text) > len(post.text):
                post.text = text
            post.urls = list(dict.fromkeys([*post.urls, *urls]))
            if (
                record.get("url")
                and is_facebook_url(str(record["url"]))
                and post_id_from_url(str(record["url"]))
            ):
                post.permalink = str(record["url"]).split("?")[0]
            created = parse_facebook_time(record.get("created_at"))
            if created and not post.created_at:
                post.created_at = created
            continue
        comment_id = (
            comment_id_from_url(str(record.get("url") or ""))
            or str(record.get("comment_id") or "")
            or _synthetic_comment_id(text)
        )
        comments.append(
            FacebookComment(
                comment_id=comment_id,
                text=text,
                urls=urls,
                created_at=parse_facebook_time(record.get("created_at")),
                permalink=record.get("url")
                if isinstance(record.get("url"), str)
                else permalink_url(post.group, post.post_id, comment_id=comment_id, kind=post.kind),
            )
        )
    post.comments = merge_comments(post.comments, comments)
    return post
