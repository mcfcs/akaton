"""Parse Reddit Shreddit HTML into raw submission dicts.

Ported from the uyam project's `uyam/sources/shreddit_parse.py`, which was derived from a
live www.reddit.com HAR. Kept close to the original so it can be re-synced when Reddit
changes its markup.

Reddit's unauthenticated `.json` endpoints are blocked or CAPTCHA-gated, and a permalink
serves a JavaScript shell to a logged-out client. The current web UI server-renders the
fields we need as attributes on custom elements, so those attributes are the source of
truth:

  <shreddit-post id="t3_..." post-title="..." score="..." author="..." permalink="..."
                 created-timestamp="2026-08-20T12:22:10.758000+0000" ...>
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

_BASE = "https://www.reddit.com"

# HTMLParser lowercases attribute names; Playwright may not. Always normalize.
_POST_TYPE_SELF = frozenset({"text", "self"})
_ISO_OFFSET_RE = re.compile(r"([+-])(\d{2})(\d{2})$")
_DIGITS_RE = re.compile(r"^\d+$")


def listing_url(
    *,
    subreddit: str,
    listing_type: str,
    search_query: str | None = None,
    sort: str | None = None,
    time_filter: str = "all",
) -> str:
    """Build a Shreddit listing or search URL for a subreddit."""
    sub = subreddit.strip().lstrip("/").removeprefix("r/")
    if listing_type == "search" and search_query:
        from urllib.parse import urlencode

        params = {
            "q": search_query,
            "restrict_sr": "1",
            "sort": sort or "relevance",
            "t": time_filter or "all",
        }
        return f"{_BASE}/r/{sub}/search/?{urlencode(params)}"
    if listing_type == "top":
        return f"{_BASE}/r/{sub}/top/?t={time_filter or 'all'}"
    kind = listing_type if listing_type in {"new", "hot", "rising"} else "new"
    return f"{_BASE}/r/{sub}/{kind}/"


def comments_url(permalink: str, *, sort: str = "confidence") -> str:
    """Build a comments-page URL, preserving an already-absolute permalink."""
    if permalink.startswith("http://") or permalink.startswith("https://"):
        url = permalink
    else:
        url = urljoin(_BASE, permalink)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}sort={sort}"


def parse_shreddit_timestamp(value: str | int | float | None) -> float:
    """Convert a Shreddit timestamp to Unix seconds.

    Accepts ISO-8601 (`2026-08-20T12:22:10.758000+0000`), Unix seconds, or
    Unix milliseconds (values > 1e12).
    """
    if value is None or value == "":
        raise ValueError("empty timestamp")
    if isinstance(value, (int, float)):
        n = float(value)
        return n / 1000.0 if n > 10_000_000_000 else n
    raw = str(value).strip()
    if _DIGITS_RE.match(raw):
        n = float(raw)
        return n / 1000.0 if n > 10_000_000_000 else n
    iso = raw.replace("Z", "+00:00")
    iso = _ISO_OFFSET_RE.sub(r"\1\2:\3", iso)
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def normalize_attrs(attrs: dict[str, Any]) -> dict[str, str]:
    """Lowercase keys; drop Nones; stringify values."""
    out: dict[str, str] = {}
    for key, val in attrs.items():
        if val is None:
            continue
        out[str(key).lower()] = str(val)
    return out


def _strip_kind_prefix(fullname: str, prefix: str) -> str:
    if fullname.startswith(prefix):
        return fullname[len(prefix) :]
    return fullname


def _as_bool(attrs: dict[str, str], flags: set[str], *names: str) -> bool:
    for name in names:
        if name in flags:
            return True
        val = attrs.get(name)
        if val is None:
            continue
        if val.lower() in {"", "true", "1", "yes"}:
            return True
    return False


def _as_int(attrs: dict[str, str], key: str, default: int = 0) -> int:
    raw = attrs.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _as_float(attrs: dict[str, str], key: str, default: float = 0.0) -> float:
    raw = attrs.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def submission_raw_from_shreddit(
    *,
    attrs: dict[str, Any],
    selftext: str = "",
    flair_text: str | None = None,
    nview: dict[str, Any] | None = None,
    flags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a mapping.py-compatible submission dict from a shreddit-post."""
    a = normalize_attrs(attrs)
    present = {f.lower() for f in (flags or [])} | {k for k, v in a.items() if v == ""}

    post_nview = (nview or {}).get("post") if isinstance(nview, dict) else None
    if not isinstance(post_nview, dict):
        post_nview = {}

    fullname = a.get("id") or post_nview.get("id") or ""
    fullname = str(fullname)
    if fullname and not fullname.startswith("t3_"):
        fullname = f"t3_{fullname}"
    reddit_id = _strip_kind_prefix(fullname, "t3_")

    permalink = a.get("permalink") or f"/r/{a.get('subreddit-name', '')}/comments/{reddit_id}/"
    if not permalink.startswith("/"):
        permalink = "/" + permalink.lstrip("/")

    content_href = a.get("content-href") or post_nview.get("url") or ""
    url = str(content_href) if content_href else f"{_BASE}{permalink}"

    created_raw = (
        a.get("created-timestamp") or a.get("created") or post_nview.get("created_timestamp")
    )
    created_utc = parse_shreddit_timestamp(created_raw)

    post_type = (a.get("post-type") or str(post_nview.get("type") or "")).lower()
    is_self = post_type in _POST_TYPE_SELF

    nsfw = _as_bool(a, present, "nsfw", "over-18", "over_18")
    if "nsfw" in post_nview:
        nsfw = bool(post_nview["nsfw"])

    subreddit = a.get("subreddit-name") or (nview or {}).get("subreddit", {}).get("name") or ""
    if isinstance(subreddit, str):
        subreddit = subreddit.removeprefix("r/")

    score = _as_int(a, "score")
    if not score and "score" in post_nview:
        score = int(post_nview["score"])

    upvote_ratio = _as_float(a, "upvote-ratio")
    if not upvote_ratio and "upvote_ratio" in post_nview:
        upvote_ratio = float(post_nview["upvote_ratio"])

    num_comments = _as_int(a, "comment-count")
    if not num_comments and "number_comments" in post_nview:
        num_comments = int(post_nview["number_comments"])

    flair = (flair_text or "").strip() or None
    if flair == "":
        flair = None

    return {
        "id": reddit_id,
        "name": fullname,
        "subreddit": subreddit,
        "title": a.get("post-title") or a.get("title") or "",
        "selftext": selftext or "",
        "created_utc": created_utc,
        "score": score,
        "upvote_ratio": upvote_ratio,
        "num_comments": num_comments,
        "permalink": permalink,
        "url": url,
        "is_self": is_self,
        "over_18": nsfw,
        "spoiler": _as_bool(a, present, "spoiler", "spoiler-tag"),
        "stickied": _as_bool(a, present, "stickied", "pinned", "is-stickied"),
        "locked": _as_bool(a, present, "locked", "is-locked"),
        "archived": _as_bool(a, present, "archived", "is-archived"),
        "distinguished": a.get("distinguished") or None,
        "link_flair_text": flair,
        "is_original_content": _as_bool(a, present, "is-original-content", "oc"),
        "num_crossposts": _as_int(a, "num-crossposts", 0),
        "gilded": _as_int(a, "award-count", 0),
        "author": a.get("author"),
    }


def comment_raw_from_shreddit(
    *,
    attrs: dict[str, Any],
    body: str = "",
    flags: Iterable[str] | None = None,
    fallback_subreddit: str = "",
) -> dict[str, Any]:
    """Build a mapping.py-compatible comment dict from a shreddit-comment."""
    a = normalize_attrs(attrs)
    present = {f.lower() for f in (flags or [])} | {k for k, v in a.items() if v == ""}

    fullname = a.get("thingid") or a.get("thing-id") or a.get("id") or ""
    fullname = str(fullname)
    if fullname and not fullname.startswith("t1_"):
        fullname = f"t1_{fullname}"
    reddit_id = _strip_kind_prefix(fullname, "t1_")

    post_id = a.get("postid") or a.get("post-id") or ""
    if post_id and not post_id.startswith("t3_"):
        post_id = f"t3_{post_id}"
    link_id = post_id or f"t3_{a.get('submission-id', '')}"

    parent_id = a.get("parentid") or a.get("parent-id") or ""
    if not parent_id:
        parent_id = link_id
    elif parent_id.startswith("t1_") or parent_id.startswith("t3_"):
        pass
    else:
        parent_id = f"t1_{parent_id}"

    permalink = a.get("permalink") or ""
    if permalink and not permalink.startswith("/"):
        permalink = "/" + permalink.lstrip("/")
    if not permalink and reddit_id:
        sub = a.get("subredditname") or fallback_subreddit
        permalink = f"/r/{sub}/comments/{_strip_kind_prefix(link_id, 't3_')}/comment/{reddit_id}/"

    created_raw = a.get("created") or a.get("created-timestamp")
    created_utc = parse_shreddit_timestamp(created_raw)

    distinguished = a.get("distinguished") or None
    if distinguished == "":
        distinguished = None

    subreddit = a.get("subredditname") or a.get("subreddit-name") or fallback_subreddit
    subreddit = subreddit.removeprefix("r/") if isinstance(subreddit, str) else fallback_subreddit

    return {
        "id": reddit_id,
        "name": fullname,
        "link_id": link_id,
        "parent_id": parent_id,
        "subreddit": subreddit,
        "body": body or "",
        "created_utc": created_utc,
        "score": _as_int(a, "score"),
        "depth": _as_int(a, "depth", 0),
        "is_submitter": _as_bool(a, present, "is-op", "is-submitter"),
        "distinguished": distinguished,
        "stickied": _as_bool(a, present, "stickied", "pinned", "is-stickied"),
        "gilded": _as_int(a, "award-count", 0),
        "controversiality": _as_int(a, "controversiality", 0),
        "permalink": permalink,
        "author": a.get("author"),
    }


class _ShredditHTMLParser(HTMLParser):
    """Stream parser for a Shreddit listing or comments page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.nview: dict[str, Any] | None = None

        self._post_stack: list[dict[str, Any]] = []
        self._comment_stack: list[dict[str, Any]] = []
        self._flair_depth = 0
        self._flair_parts: list[str] = []
        self._body_depth = 0
        self._body_parts: list[str] = []
        self._body_target: str | None = None  # "post" | "comment"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v if v is not None else "") for k, v in attrs}
        flags = {k for k, v in attrs if v is None or v == ""}

        if tag == "shreddit-screenview-data" or tag == "nview-data":
            raw = ad.get("data")
            if raw:
                try:
                    self.nview = json.loads(raw)
                except json.JSONDecodeError:
                    self.nview = None

        if tag == "shreddit-post":
            self._post_stack.append({"attrs": ad, "flags": flags, "selftext": "", "flair_text": ""})
            return

        if tag == "shreddit-post-flair":
            self._flair_depth += 1
            return

        if tag == "shreddit-comment":
            self._comment_stack.append({"attrs": ad, "flags": flags, "body": ""})
            return

        if tag in {"svg", "span"} and self._post_stack:
            label = (ad.get("aria-label") or "").lower()
            cls = (ad.get("class") or "").lower()
            hidden = "hidden" in cls.split()
            if not hidden:
                if "stickied post" in label:
                    self._post_stack[-1]["flags"].add("stickied")
                elif "locked post" in label:
                    self._post_stack[-1]["flags"].add("locked")
                elif "archived post" in label:
                    self._post_stack[-1]["flags"].add("archived")
                elif label in {"nsfw", "nsfw post"}:
                    self._post_stack[-1]["flags"].add("nsfw")
                elif "spoiler" in label:
                    self._post_stack[-1]["flags"].add("spoiler")
                elif label in {"original content", "oc"}:
                    self._post_stack[-1]["flags"].add("is-original-content")

        if tag == "div":
            div_id = ad.get("id") or ""
            if div_id.endswith("-post-rtjson-content"):
                self._body_depth += 1
                if self._body_depth == 1:
                    self._body_parts = []
                    if self._comment_stack:
                        self._body_target = "comment"
                    elif self._post_stack:
                        self._body_target = "post"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "shreddit-post-flair" and self._flair_depth:
            self._flair_depth -= 1
            if self._flair_depth == 0 and self._post_stack:
                self._post_stack[-1]["flair_text"] = "".join(self._flair_parts).strip()
                self._flair_parts = []

        if tag == "div" and self._body_depth:
            self._body_depth -= 1
            if self._body_depth == 0:
                text = "".join(self._body_parts).strip()
                if self._body_target == "comment" and self._comment_stack:
                    # Only fill if this comment doesn't already have a body:
                    # the first rtjson in tree order is the comment's own body.
                    if not self._comment_stack[-1]["body"]:
                        self._comment_stack[-1]["body"] = text
                elif (
                    self._body_target == "post"
                    and self._post_stack
                    and not self._post_stack[-1]["selftext"]
                ):
                    self._post_stack[-1]["selftext"] = text
                self._body_parts = []
                self._body_target = None

        if tag == "shreddit-comment" and self._comment_stack:
            self.comments.append(self._comment_stack.pop())

        if tag == "shreddit-post" and self._post_stack:
            self.posts.append(self._post_stack.pop())

    def handle_data(self, data: str) -> None:
        if self._flair_depth:
            self._flair_parts.append(data)
        elif self._body_depth:
            self._body_parts.append(data)


def parse_shreddit_html(
    html: str, *, fallback_subreddit: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse a Shreddit HTML document into raw submission/comment dicts.

    Returns (submissions, comments) ready for map_submission_dict / map_comment_dict.
    Promoted/ad posts are skipped.
    """
    parser = _ShredditHTMLParser()
    parser.feed(html)
    parser.close()

    submissions: list[dict[str, Any]] = []
    for post in parser.posts:
        attrs = post["attrs"]
        cls = (attrs.get("class") or "").lower()
        if "promoted" in cls or attrs.get("ad-type") or "promoted" in post["flags"]:
            continue
        try:
            raw = submission_raw_from_shreddit(
                attrs=attrs,
                selftext=post["selftext"],
                flair_text=post["flair_text"] or None,
                nview=parser.nview,
                flags=post["flags"],
            )
        except (ValueError, KeyError):
            continue
        if not raw.get("id"):
            continue
        if fallback_subreddit and not raw.get("subreddit"):
            raw["subreddit"] = fallback_subreddit
        submissions.append(raw)

    comments: list[dict[str, Any]] = []
    for com in parser.comments:
        attrs = com["attrs"]
        thing = attrs.get("thingid") or attrs.get("thing-id")
        cid = attrs.get("id", "")
        if not (thing or cid.startswith("t1_")):
            continue
        try:
            raw = comment_raw_from_shreddit(
                attrs=attrs,
                body=com["body"],
                flags=com["flags"],
                fallback_subreddit=fallback_subreddit,
            )
        except (ValueError, KeyError):
            continue
        if not raw.get("id"):
            continue
        comments.append(raw)

    return submissions, comments
