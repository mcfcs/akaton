"""Reddit discovery through a real Chrome session.

Ported from the uyam project's shreddit collector and narrowed to what discovery needs:
listing pages only, no comments, no corpus storage. Akaton's own fetcher cannot read
Reddit at all — a permalink returns a JavaScript shell to a logged-out client and
`old.reddit.com` redirects to a login — so the only way to see a Philippine subreddit is
to drive a browser the way a logged-out person would.

The approach, in order of importance:

1. Headed Chrome (`channel="chrome"`) through Patchright, which patches Playwright's
   automation fingerprints at the protocol level. Headless is a common block.
2. A persistent profile, so a challenge solved once survives relaunches.
3. A proxy per session from `proxies.txt`. A blocked proxy is put on cooldown and the
   browser relaunches on the next one.
4. No custom User-Agent or extra headers: Chrome's own are less of a fingerprint tell.

Captchas are never auto-solved. If Reddit challenges and nobody answers, the run gives up
on that listing, rotates, and moves on, so an unattended monitor degrades to finding
nothing rather than hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from akaton.discovery.shreddit_parse import parse_shreddit_html
from akaton.domain.models import CandidateSeed
from akaton.fetch.proxy import ProxyManager

logger = logging.getLogger(__name__)

BASE = "https://www.reddit.com"
POST_SELECTOR = "shreddit-post"
BLOCK_SNIPPETS = (
    "whoa there, pardner",
    "whoa there",
    "request blocked",
    "unusual traffic",
    "verify you are human",
    "prove your humanity",
    "access denied",
    "checking your browser",
    "just a moment",
    # Reddit's current interstitial for a headless session, seen as a js_challenge
    # redirect. Without it the block reads as an empty listing and no rotation happens.
    "blocked by network security",
    "file a ticket",
)
RATE_LIMIT_SNIPPETS = ("too many requests", "err_http_response_code_failure", " 429 ")
HYDRATE_WAIT_SECONDS = 20.0
PERMALINK_RE = re.compile(r'href="(/r/[^"]+/comments/[^"?]+)"')

# Philippine subreddits where competitions actually get posted.
DEFAULT_SUBREDDITS = ("PinoyProgrammer", "ITPhilippines", "ProgrammerPH")
DEFAULT_TERMS = (
    "hackathon",
    "business case",
    "case competition",
    "competition",
    "challenge",
    "ideathon",
    "datathon",
)


def listing_url(subreddit: str, *, listing: str = "new") -> str:
    """A subreddit listing, which is what actually renders <shreddit-post> elements.

    Reddit's search results page does not: it is built from `search-telemetry-tracker`
    wrappers with no post custom element, so the attribute parser finds nothing there.
    Walking /new and filtering locally is both parseable and fewer requests than one
    search per term.
    """
    sub = subreddit.strip().lstrip("/").removeprefix("r/")
    kind = listing if listing in {"new", "hot", "rising", "top"} else "new"
    return f"{BASE}/r/{sub}/{kind}/"


def search_url(subreddit: str, query: str, *, sort: str = "new", time_filter: str = "month") -> str:
    sub = subreddit.strip().lstrip("/").removeprefix("r/")
    params = {"q": query, "restrict_sr": "1", "sort": sort, "t": time_filter}
    return f"{BASE}/r/{sub}/search/?{urlencode(params)}"


def post_id_from_permalink(permalink: str) -> str | None:
    match = re.search(r"/comments/([0-9a-z]+)", permalink)
    return match.group(1) if match else None


def matches_terms(record: dict, terms: tuple[str, ...]) -> bool:
    haystack = f"{record.get('title') or ''}\n{record.get('selftext') or ''}".casefold()
    return any(term in haystack for term in terms)


class ShredditSource:
    """Collects competition posts from Philippine subreddits with a headed browser."""

    name = "reddit"

    def __init__(
        self,
        *,
        proxies: ProxyManager | None = None,
        profile_dir: Path | None = None,
        subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
        terms: tuple[str, ...] = DEFAULT_TERMS,
        headless: bool = False,
        max_age_days: int = 90,
        nav_timeout_ms: int = 45_000,
        min_interval_seconds: float = 5.0,
        challenge_wait_seconds: float = 0.0,
        scroll_rounds: int = 8,
        max_posts_per_term: int = 8,
    ) -> None:
        self.proxies = proxies
        self.profile_dir = profile_dir or Path("data/.browser-profile")
        self.subreddits = subreddits
        self.terms = terms
        self.headless = headless
        self.max_age_days = max_age_days
        self.nav_timeout_ms = nav_timeout_ms
        self.min_interval_seconds = min_interval_seconds
        self.challenge_wait_seconds = challenge_wait_seconds
        self.scroll_rounds = scroll_rounds
        self.max_posts_per_term = max_posts_per_term
        self._last_request_at: float | None = None
        # See FacebookGroupSource.last_error: an empty list has to be able to mean either
        # "nothing was posted" or "the collector never got off the ground".
        self.last_error: str | None = None

    async def discover(
        self, since: datetime | None = None, cursor: str | None = None
    ) -> list[CandidateSeed]:
        self.last_error = None
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            logger.warning("shreddit_patchright_missing")
            self.last_error = "patchright is not installed"
            return []

        cutoff = datetime.now(UTC) - timedelta(days=self.max_age_days)
        if since:
            cutoff = max(cutoff, since)
        seeds: dict[str, CandidateSeed] = {}
        searches = hits = 0
        async with async_playwright() as playwright:
            session = _BrowserSession(self, playwright)
            try:
                for subreddit in self.subreddits:
                    for term in self.terms:
                        await self._throttle()
                        permalinks = await session.search_permalinks(search_url(subreddit, term))
                        searches += 1
                        hits += len(permalinks)
                        logger.info(
                            "shreddit_search",
                            extra={"subreddit": subreddit, "term": term, "hits": len(permalinks)},
                        )
                        for permalink in permalinks[: self.max_posts_per_term]:
                            if permalink in session.visited:
                                continue
                            await self._throttle()
                            record = await session.post_record(permalink, subreddit)
                            if not record or not matches_terms(record, self.terms):
                                continue
                            seed = _to_seed(record, cutoff, self.name)
                            if seed:
                                seeds.setdefault(str(seed.url), seed)
            finally:
                await session.close()
        if searches and not hits:
            # Searching a busy subreddit for "hackathon" over 90 days always matches
            # something. Every search coming back empty means the result list never
            # rendered — a block or a challenge page — not that Reddit went quiet.
            self.last_error = f"no results from any of {searches} searches; likely blocked"
        logger.info("shreddit_discovered", extra={"seeds": len(seeds)})
        return list(seeds.values())

    async def _throttle(self) -> None:
        if self._last_request_at is not None and self.min_interval_seconds > 0:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.4))
        self._last_request_at = time.monotonic()


class _BrowserSession:
    """One Chrome window, relaunched on a different proxy when Reddit blocks it."""

    def __init__(self, source: ShredditSource, playwright) -> None:
        self.source = source
        self.playwright = playwright
        self.context = None
        self.page = None
        self.proxy = None
        self.visited: set[str] = set()

    async def close(self) -> None:
        # The persistent profile is kept, so a solved challenge survives.
        for target in (self.page, self.context):
            if target is not None:
                with contextlib.suppress(Exception):
                    await target.close()
        self.page = self.context = None

    async def _launch(self) -> bool:
        manager = self.source.proxies
        self.proxy = manager.select() if manager and manager.mode != "direct" else None
        if manager and manager.mode == "proxy_only" and self.proxy is None:
            logger.warning("shreddit_no_healthy_proxy")
            return False
        profile = self.source.profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        options = {
            "user_data_dir": str(profile),
            "headless": self.source.headless,
            "no_viewport": True,
            "ignore_https_errors": True,
            "channel": "chrome",
        }
        if self.proxy:
            options["proxy"] = self.proxy.as_browser_proxy()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(**options)
        except Exception:
            options.pop("channel", None)  # fall back to bundled Chromium
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(**options)
            except Exception:
                logger.exception("shreddit_launch_failed")
                return False
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(self.source.nav_timeout_ms)
        return True

    async def _rotate(self, reason: str) -> None:
        logger.warning("shreddit_rotate", extra={"reason": reason})
        if self.proxy and self.source.proxies:
            self.source.proxies.report_failure(self.proxy.proxy_id, proxy_attributable=True)
        await self.close()

    async def search_permalinks(self, url: str) -> list[str]:
        """Collect post permalinks from a search results page.

        Search results are not <shreddit-post> elements, so their attributes cannot be
        read here. The anchors are stable though, and a post's own page does render the
        custom element, so the permalinks are followed one by one.
        """
        html = await self.page_html(url, wait_for_posts=False)
        if not html:
            return []
        found = PERMALINK_RE.findall(html)
        ordered: list[str] = []
        for path in found:
            absolute = f"{BASE}{path}"
            if absolute not in ordered:
                ordered.append(absolute)
        return ordered

    async def post_record(self, permalink: str, subreddit: str) -> dict | None:
        self.visited.add(permalink)
        html = await self.page_html(permalink, wait_for_posts=True)
        if not html:
            return None
        records = _submissions(html, subreddit)
        # A comments page also renders recommended posts, so the first element is not
        # necessarily the one that was asked for. Match on the id inside the permalink.
        wanted = post_id_from_permalink(permalink)
        for record in records:
            if wanted and record.get("id") == wanted:
                return record
        return None

    async def listing_html(self, url: str) -> str | None:
        return await self.page_html(url, wait_for_posts=True)

    async def page_html(self, url: str, *, wait_for_posts: bool) -> str | None:
        for _ in range(2):
            if self.page is None and not await self._launch():
                return None
            try:
                await self.page.goto(url, wait_until="commit")
            except Exception as exc:
                await self._rotate(type(exc).__name__)
                continue
            state = await self._wait_for_feed(wait_for_posts=wait_for_posts)
            if state == "feed":
                if wait_for_posts:
                    await self._load_more()
                return await self.page.content()
            if state == "blocked":
                await self._rotate("challenge")
                continue
            return None
        return None

    async def _load_more(self) -> None:
        """Scroll to pull in lazily loaded posts, stopping once the count settles."""
        previous = 0
        for _ in range(self.source.scroll_rounds):
            with contextlib.suppress(Exception):
                count = await self.page.locator(POST_SELECTOR).count()
                if count and count == previous:
                    return
                previous = count
                await self.page.mouse.wheel(0, 3000)
            await asyncio.sleep(1.5)

    async def _page_text(self) -> str:
        if self.page is None:
            return ""
        title = ""
        with contextlib.suppress(Exception):
            title = (await self.page.title() or "").lower()
        body = ""
        with contextlib.suppress(Exception):
            body = ((await self.page.inner_text("body", timeout=2000)) or "")[:1500].lower()
        return f"{title}\n{body}"

    async def _has_feed(self) -> bool:
        if self.page is None:
            return False
        with contextlib.suppress(Exception):
            return await self.page.locator(POST_SELECTOR).count() > 0
        return False

    async def _ready(self, wait_for_posts: bool) -> bool:
        if wait_for_posts:
            return await self._has_feed()
        # A search page never renders a post element, so readiness is its result anchors.
        with contextlib.suppress(Exception):
            return bool(PERMALINK_RE.search(await self.page.content()))
        return False

    async def _wait_for_feed(self, *, wait_for_posts: bool = True) -> str:
        """Return "feed", "blocked", or "empty"."""
        deadline = time.monotonic() + HYDRATE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if await self._ready(wait_for_posts):
                return "feed"
            text = await self._page_text()
            if any(snippet in text for snippet in BLOCK_SNIPPETS + RATE_LIMIT_SNIPPETS):
                break
            await asyncio.sleep(1)
        if await self._ready(wait_for_posts):
            return "feed"
        text = await self._page_text()
        if not any(snippet in text for snippet in BLOCK_SNIPPETS + RATE_LIMIT_SNIPPETS):
            return "empty"
        # A challenge is only solvable by a person in the visible window. Wait only if
        # somebody was told to expect it; otherwise rotate rather than block the monitor.
        if self.source.challenge_wait_seconds > 0 and not self.source.headless:
            logger.warning(
                "shreddit_challenge_waiting",
                extra={"seconds": self.source.challenge_wait_seconds},
            )
            challenge_deadline = time.monotonic() + self.source.challenge_wait_seconds
            while time.monotonic() < challenge_deadline:
                if await self._has_feed():
                    return "feed"
                await asyncio.sleep(1)
        return "blocked"


def _submissions(html: str, subreddit: str) -> list[dict]:
    try:
        submissions, _ = parse_shreddit_html(html, fallback_subreddit=subreddit)
    except Exception:
        logger.exception("shreddit_parse_failed", extra={"subreddit": subreddit})
        return []
    return submissions


def _to_seed(record: dict, cutoff: datetime, provider: str) -> CandidateSeed | None:
    created = record.get("created_utc")
    if isinstance(created, (int, float)):
        created_at = datetime.fromtimestamp(created, tz=UTC)
        if created_at < cutoff:
            return None
    else:
        created_at = None
    permalink = record.get("permalink") or ""
    if permalink.startswith("/"):
        permalink = f"{BASE}{permalink}"
    link = record.get("url") or ""
    outbound = link if link and "reddit.com" not in link else None
    target = outbound or permalink
    if not target:
        return None
    title = record.get("title") or ""
    selftext = record.get("selftext") or ""
    try:
        return CandidateSeed(
            url=target,
            title=title or None,
            snippet=selftext[:500] or None,
            discovery_channel="reddit",
            provider=provider,
            source_key=record.get("name"),
            published_hint=created_at,
            # A link post is fetched from the page it points at, which is authoritative.
            # A self-post has nothing to fetch, so its body travels with the candidate.
            content=None if outbound else f"{title}\n\n{selftext}".strip() or None,
        )
    except ValueError:
        return None
