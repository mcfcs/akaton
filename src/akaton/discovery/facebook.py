"""Facebook group discovery through a real Chrome session.

The HTTP fetcher cannot read Facebook: a logged-out request gets a JavaScript
shell, and `config/domains.yaml` disables fetch on facebook.com so search
snippets never try. The only way to see philhacks is the same headed Patchright
approach uyam uses for Reddit: system Chrome, a persistent profile, and a
person solving a captcha or checkpoint once.

Bypass strategy, in order:

1. Headed Chrome (`channel="chrome"`) through Patchright.
2. A persistent profile so a login survives relaunches.
3. One sticky proxy from `proxies.txt` for the whole session. Rotating after
   login is a checkpoint; treating a captcha as a block and rotating is worse.
4. Optional FACEBOOK_EMAIL / FACEBOOK_PASSWORD typed like a person, not pasted.
5. No custom User-Agent. Chrome's own headers are less of a fingerprint tell.

Captchas are not auto-solved. A visible recaptcha checkbox is clicked; Arkose
and image puzzles stay in the headed window for a person. If nobody answers,
the run returns nothing rather than hanging an unattended monitor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from akaton.discovery.facebook_parse import (
    FacebookPost,
    GroupTarget,
    apply_graphql_records,
    comments_from_dom,
    group_feed_url,
    groups_from_config,
    merge_comments,
    needs_comment_expansion,
    post_from_dom,
    records_from_graphql,
    records_from_html,
    thread_to_seeds,
)
from akaton.domain.models import CandidateSeed
from akaton.fetch.proxy import ProxyManager

logger = logging.getLogger(__name__)

DEFAULT_GROUPS = groups_from_config(None)
STICKY_PROXY_FILE = "sticky-proxy.json"
STORAGE_STATE_FILE = "storage_state.json"
ARTICLE_SELECTOR = '[role="article"]'


def load_sticky_proxy_id(profile_dir: Path) -> str | None:
    path = profile_dir / STICKY_PROXY_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    value = payload.get("proxy_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


def save_sticky_proxy_id(profile_dir: Path, proxy_id: str) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / STICKY_PROXY_FILE).write_text(
        json.dumps({"proxy_id": proxy_id}), encoding="utf-8"
    )


def clear_sticky_proxy_id(profile_dir: Path) -> None:
    path = profile_dir / STICKY_PROXY_FILE
    with contextlib.suppress(OSError):
        path.unlink()


HYDRATE_WAIT_SECONDS = 20.0
LOGIN_SNIPPETS = (
    "log in to facebook",
    "log into facebook",
    "email or phone",
    "create new account",
    "forgotten password",
    "you must log in",
)
CHECKPOINT_SNIPPETS = (
    "we need to confirm it's you",
    "we need to confirm it is you",
    "enter the code we sent",
    "identify your account",
    "your account has been locked",
    "suspicious activity",
)
CAPTCHA_SNIPPETS = (
    "confirm you are human",
    "confirm you're human",
    "i'm not a robot",
    "im not a robot",
    "why am i seeing this",
    "enter the characters you see",
    "security check",
    "captcha",
)
BLOCK_SNIPPETS = (
    "you can't use this feature right now",
    "we limit how often you can post",
    "temporarily blocked",
    "request blocked",
    "unusual traffic",
)
COOKIE_BUTTON_RE = re.compile(
    r"(Allow all cookies|Decline optional cookies|Accept all|Only allow essential cookies)",
    re.IGNORECASE,
)
LOGIN_BUTTON_RE = re.compile(r"^(Log in|Log In|Sign in)$", re.IGNORECASE)
JOIN_GROUP_RE = re.compile(r"^(Join group|Join)$", re.IGNORECASE)
SEE_MORE_RE = re.compile(r"^See more$", re.IGNORECASE)
VIEW_COMMENTS_RE = re.compile(
    r"(View( more| all)? comments|See (previous|more) comments|All comments)",
    re.IGNORECASE,
)

EXTRACT_POSTS_JS = """
() => {
  const feed = document.querySelector('[role="feed"]') || document.body;
  const articles = Array.from(feed.querySelectorAll('[role="article"]'));
  const results = [];
  const seen = new Set();
  for (const el of articles) {
    const aria = el.getAttribute("aria-label") || "";
    if (/^Comment by/i.test(aria)) continue;
    const parent = el.parentElement ? el.parentElement.closest('[role="article"]') : null;
    if (parent) continue;
    const text = (el.innerText || "").trim();
    if (text.length < 20) continue;
    const hrefs = Array.from(el.querySelectorAll("a[href]"))
      .map((a) => a.href)
      .filter(Boolean);
    const permalink =
      hrefs.find((h) => /\\/permalink\\/\\d+/.test(h))
      || hrefs.find((h) => /\\/posts\\/\\d+/.test(h))
      || hrefs.find((h) => /story_fbid=/.test(h));
    const key = permalink || text.slice(0, 160);
    if (seen.has(key)) continue;
    seen.add(key);
    const timeEl = el.querySelector("a[href*='/permalink/'], a[href*='/posts/']");
    results.push({
      text,
      hrefs,
      permalink: permalink || "",
      author: "",
      comment_count: (text.match(/(\\d+)\\s+comments?/i) || [, "0"])[1],
      time_label: timeEl ? (timeEl.getAttribute("aria-label") || "") : "",
    });
  }
  return results;
}
"""

EXTRACT_COMMENTS_JS = """
() => {
  const nodes = Array.from(
    document.querySelectorAll('[role="article"][aria-label^="Comment by"]')
  ).filter((el) => !el.closest(
    // Meta's account and security dialogs render as articles too, and were being
    // scraped as replies. Anything inside site chrome is not part of the thread.
    '[role="dialog"], [role="banner"], [role="navigation"], [role="complementary"]'
  ));
  return nodes.map((el) => {
    const aria = el.getAttribute("aria-label") || "";
    const author = aria.replace(/^Comment by\\s+/i, "").split(/\\s+in\\s+/)[0].trim();
    const hrefs = Array.from(el.querySelectorAll("a[href]")).map((a) => a.href);
    const permalink = hrefs.find((h) => /comment_id=/.test(h)) || "";
    return {
      author,
      text: (el.innerText || "").trim(),
      hrefs,
      permalink,
      comment_id: (permalink.match(/comment_id=([^&]+)/) || [, ""])[1],
    };
  });
}
"""


class FacebookGroupSource:
    """Collects competition posts from configured Facebook groups with a headed browser."""

    name = "facebook"

    def __init__(
        self,
        *,
        proxies: ProxyManager | None = None,
        profile_dir: Path | None = None,
        groups: tuple[GroupTarget, ...] | None = None,
        headless: bool = False,
        max_age_days: int = 90,
        nav_timeout_ms: int = 45_000,
        min_interval_seconds: float = 4.0,
        login_wait_seconds: float = 0.0,
        scroll_rounds: int = 10,
        max_posts: int = 40,
        max_permalinks: int = 25,
        use_proxy: bool = False,
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        self.proxies = proxies
        self.profile_dir = profile_dir or Path("data/.facebook-profile")
        self.groups = groups or DEFAULT_GROUPS
        self.headless = headless
        self.max_age_days = max_age_days
        self.nav_timeout_ms = nav_timeout_ms
        self.min_interval_seconds = min_interval_seconds
        self.login_wait_seconds = login_wait_seconds
        self.scroll_rounds = scroll_rounds
        self.max_posts = max_posts
        self.max_permalinks = max_permalinks
        self.use_proxy = use_proxy
        self.email = email
        self.password = password
        self._last_request_at: float | None = None
        self.last_posts: list[FacebookPost] = []
        self._sticky_proxy = None

    async def discover(
        self, since: datetime | None = None, cursor: str | None = None
    ) -> list[CandidateSeed]:
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            logger.warning("facebook_patchright_missing")
            return []

        cutoff = datetime.now(UTC) - timedelta(days=self.max_age_days)
        if since:
            cutoff = max(cutoff, since)
        self.last_posts = []
        seeds: dict[str, CandidateSeed] = {}
        async with async_playwright() as playwright:
            session = _BrowserSession(self, playwright)
            try:
                if not await session.ensure_logged_in():
                    logger.warning("facebook_not_logged_in")
                    return []
                for group in self.groups:
                    await self._throttle()
                    posts = await session.collect_group(group)
                    permalinks_used = 0
                    for post in posts[: self.max_posts]:
                        if permalinks_used < self.max_permalinks and needs_comment_expansion(post):
                            await self._throttle()
                            post = await session.expand_thread(post)
                            permalinks_used += 1
                        self.last_posts.append(post)
                        for seed in thread_to_seeds(
                            post,
                            cutoff=cutoff,
                            location=group.location,
                            provider=self.name,
                            query=group.name,
                        ):
                            seeds.setdefault(str(seed.url), seed)
            finally:
                await session.close()
        logger.info("facebook_discovered", extra={"seeds": len(seeds)})
        return list(seeds.values())

    async def _throttle(self) -> None:
        if self._last_request_at is not None and self.min_interval_seconds > 0:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.5))
        self._last_request_at = time.monotonic()


class _BrowserSession:
    """One Chrome window, persistent Facebook login, optional proxy rotation."""

    def __init__(self, source: FacebookGroupSource, playwright) -> None:
        self.source = source
        self.playwright = playwright
        self.context = None
        self.page = None
        self.proxy = None
        self._graphql: list[str] = []
        self._recaptcha_clicked = False

    async def close(self) -> None:
        for target in (self.page, self.context):
            if target is not None:
                with contextlib.suppress(Exception):
                    await target.close()
        self.page = self.context = None

    async def _launch(self) -> bool:
        manager = self.source.proxies
        self.proxy = None
        if self.source.use_proxy:
            # One proxy for login and scraping. Switching IPs after a captcha or
            # login is how Facebook turns a solvable challenge into a lockout.
            if manager is None:
                logger.warning("facebook_no_proxy_manager")
                return False
            sticky = self.source._sticky_proxy
            if sticky is None:
                saved = load_sticky_proxy_id(self.source.profile_dir)
                saved_state = manager.states.get(saved) if saved else None
                if saved_state is not None and saved_state.healthy(datetime.now(UTC)):
                    sticky = saved_state.config
                    self.source._sticky_proxy = sticky
                    logger.info("facebook_sticky_proxy_restored")
            state = manager.states.get(sticky.proxy_id) if sticky is not None else None
            if sticky is not None and state is not None and state.healthy(datetime.now(UTC)):
                self.proxy = sticky
            else:
                if sticky is not None:
                    logger.warning("facebook_sticky_proxy_unhealthy")
                    clear_sticky_proxy_id(self.source.profile_dir)
                self.proxy = manager.select()
                self.source._sticky_proxy = self.proxy
            if self.proxy is None:
                logger.warning("facebook_no_healthy_proxy")
                return False
            logger.info("facebook_proxy_selected", extra={"proxy": self.proxy.redacted()})
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
            options.pop("channel", None)
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(**options)
            except Exception:
                logger.exception("facebook_launch_failed")
                return False
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(self.source.nav_timeout_ms)
        self.page.on("response", self._capture_graphql)
        return True

    async def _capture_graphql(self, response) -> None:
        url = getattr(response, "url", "") or ""
        if "/api/graphql" not in url:
            return
        try:
            if response.status != 200:
                return
            text = await response.text()
        except Exception:
            return
        if text:
            self._graphql.append(text)

    async def _rotate(self, reason: str) -> None:
        logger.warning("facebook_rotate", extra={"reason": reason})
        if self.proxy and self.source.proxies:
            self.source.proxies.report_failure(self.proxy.proxy_id, proxy_attributable=True)
        self.source._sticky_proxy = None
        clear_sticky_proxy_id(self.source.profile_dir)
        await self.close()

    async def ensure_logged_in(self) -> bool:
        html = await self.page_html("https://www.facebook.com/login/", wait_for="home")
        if html is None:
            html = await self.page_html("https://www.facebook.com/", wait_for="home")
        if html is None:
            return False
        state = await self._page_state()
        if state == "ready":
            await self._persist_session()
            return True
        if self.source.email and self.source.password and state != "ready":
            await self._login_with_credentials()
            state = await self._page_state()
            if state == "ready":
                await self._persist_session()
                return True
        if state != "ready" and self.source.login_wait_seconds > 0:
            if self.source.headless:
                logger.warning("facebook_login_needed_headless")
                return False
            logger.warning(
                "facebook_challenge_waiting",
                extra={"state": state, "seconds": self.source.login_wait_seconds},
            )
            await self._snapshot_challenge()
            deadline = time.monotonic() + self.source.login_wait_seconds
            while time.monotonic() < deadline:
                await self._try_captcha_checkbox()
                state = await self._page_state()
                if state == "ready":
                    await self._persist_session()
                    return True
                await asyncio.sleep(2)
            await self._snapshot_challenge()
            logger.warning("facebook_login_unresolved", extra={"state": await self._page_state()})
        if await self._page_state() == "ready":
            await self._persist_session()
            return True
        return False

    async def _persist_session(self) -> None:
        """Keep the logged-in profile and the proxy that Facebook already accepted."""
        if self.proxy:
            save_sticky_proxy_id(self.source.profile_dir, self.proxy.proxy_id)
        if self.context is None:
            return
        with contextlib.suppress(Exception):
            await self.context.storage_state(path=str(self.source.profile_dir / STORAGE_STATE_FILE))
        logger.info("facebook_session_persisted")

    async def _login_with_credentials(self) -> None:
        """Fill the Facebook login form the way a person would.

        Instant `fill()` is a fingerprint tell. Credentials are never logged.
        """
        if self.page is None:
            return
        await self._dismiss_cookies()
        email_box = await self._first_locator(
            'input[name="email"]',
            'input[id="email"]',
            'input[type="text"][name="email"]',
        )
        pass_box = await self._first_locator(
            'input[name="pass"]',
            'input[id="pass"]',
            'input[type="password"]',
        )
        if email_box is None or pass_box is None:
            logger.warning("facebook_login_form_missing")
            return
        try:
            await self._type_human(email_box, self.source.email or "")
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await self._type_human(pass_box, self.source.password or "")
            await asyncio.sleep(random.uniform(0.4, 1.0))
            submit = self.page.locator('button[name="login"]')
            if await submit.count() == 0:
                submit = self.page.get_by_role("button", name=LOGIN_BUTTON_RE)
            if await submit.count():
                await submit.first.click()
            logger.info("facebook_login_submitted")
        except Exception:
            logger.warning("facebook_login_submit_failed")
            return
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = await self._page_state()
            if state in {"ready", "captcha", "checkpoint"}:
                logger.info("facebook_login_state", extra={"state": state})
                return
            await asyncio.sleep(0.8)
        logger.info("facebook_login_state", extra={"state": await self._page_state()})

    async def _type_human(self, locator, value: str) -> None:
        await locator.click()
        with contextlib.suppress(Exception):
            await locator.fill("")
        delay = random.randint(45, 110)
        with contextlib.suppress(Exception):
            await locator.press_sequentially(value, delay=delay)
            return
        await locator.type(value, delay=delay)

    async def _first_locator(self, *selectors: str):
        if self.page is None:
            return None
        for selector in selectors:
            locator = self.page.locator(selector)
            with contextlib.suppress(Exception):
                if await locator.count():
                    return locator.first
        return None

    async def _join_group(self) -> None:
        if self.page is None:
            return
        with contextlib.suppress(Exception):
            button = self.page.get_by_role("button", name=JOIN_GROUP_RE)
            if await button.count():
                await button.first.click()
                logger.info("facebook_join_group_clicked")
                await asyncio.sleep(2.5)

    async def _dismiss_cookies(self) -> None:
        if self.page is None:
            return
        with contextlib.suppress(Exception):
            button = self.page.get_by_role("button", name=COOKIE_BUTTON_RE)
            if await button.count():
                await button.first.click()
                await asyncio.sleep(0.6)

    async def _try_captcha_checkbox(self) -> None:
        """Click the recaptcha checkbox once. Clicking again restarts the puzzle."""
        if self.page is None or self._recaptcha_clicked:
            return
        with contextlib.suppress(Exception):
            if await self.page.locator('iframe[src*="bframe"]').count():
                self._recaptcha_clicked = True
                return
        frame_selectors = (
            'iframe[title="reCAPTCHA"]',
            'iframe[title*="reCAPTCHA" i]',
            'iframe[src*="recaptcha"]',
            'iframe[src*="google.com/recaptcha"]',
        )
        for selector in frame_selectors:
            with contextlib.suppress(Exception):
                frame = self.page.frame_locator(selector).first
                box = frame.locator("#recaptcha-anchor, .recaptcha-checkbox-border")
                await box.first.click(timeout=2000)
                self._recaptcha_clicked = True
                logger.info("facebook_recaptcha_clicked")
                return
        for selector in frame_selectors:
            with contextlib.suppress(Exception):
                iframe = self.page.locator(selector).first
                geom = await iframe.bounding_box()
                if not geom:
                    continue
                await self.page.mouse.click(geom["x"] + 22, geom["y"] + geom["height"] / 2)
                self._recaptcha_clicked = True
                logger.info("facebook_recaptcha_clicked")
                return

    async def collect_group(self, group: GroupTarget) -> list[FacebookPost]:
        url = group_feed_url(group.url)
        html = await self.page_html(url, wait_for="feed")
        if not html:
            return []
        await self._join_group()
        await asyncio.sleep(2)
        await self._scroll_feed()
        records = await self._eval_posts()
        posts: dict[str, FacebookPost] = {}
        for record in records:
            post = post_from_dom(record, group.name)
            if post:
                posts[post.post_id] = post
        for record in records_from_html(await self.page.content()) + records_from_graphql(
            self._graphql
        ):
            post_id = record.get("post_id")
            if post_id and post_id in posts:
                apply_graphql_records(posts[post_id], [record])
            elif post_id:
                built = post_from_dom(
                    {
                        "post_id": post_id,
                        "text": record.get("text") or "",
                        "hrefs": record.get("urls") or [],
                        "permalink": record.get("url"),
                        "created_at": record.get("created_at"),
                    },
                    group.name,
                )
                if built:
                    posts.setdefault(built.post_id, built)
        logger.info(
            "facebook_group_collected",
            extra={"group": group.name, "posts": len(posts)},
        )
        return list(posts.values())

    async def expand_thread(self, post: FacebookPost) -> FacebookPost:
        self._graphql = []
        html = await self.page_html(post.permalink, wait_for="feed")
        if not html:
            return post
        await self._expand_truncated()
        await self._expand_comments()
        dom_comments = comments_from_dom(await self._eval_comments(), post)
        post.comments = list(post.comments)
        apply_graphql_records(post, records_from_graphql(self._graphql))
        apply_graphql_records(post, records_from_html(await self.page.content()))
        post.comments = merge_comments(post.comments, dom_comments)
        if not post.text:
            records = await self._eval_posts()
            if records:
                rebuilt = post_from_dom(records[0], post.group)
                if rebuilt:
                    post.text = rebuilt.text or post.text
                    post.urls = list(dict.fromkeys([*post.urls, *rebuilt.urls]))
        return post

    async def page_html(self, url: str, *, wait_for: str) -> str | None:
        for _ in range(2):
            if self.page is None and not await self._launch():
                return None
            self._graphql = []
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                logger.warning("facebook_goto_failed", extra={"error": type(exc).__name__})
                if wait_for == "home":
                    return None
                await asyncio.sleep(2)
                continue
            state = await self._wait_for(wait_for)
            if state == "ready":
                return await self.page.content()
            if state in {"login", "captcha", "checkpoint"}:
                # A challenge is solvable in this window. Rotating would throw it away.
                return await self.page.content()
            if state == "blocked":
                await self._rotate(state)
                continue
            return None
        return None

    async def _scroll_feed(self) -> None:
        previous = 0
        idle = 0
        for _ in range(self.source.scroll_rounds):
            with contextlib.suppress(Exception):
                count = await self.page.locator(ARTICLE_SELECTOR).count()
                if count and count == previous:
                    idle += 1
                    # A single preview post is not a feed. Keep scrolling until
                    # several cards appear or the rounds run out.
                    if idle >= 3 and count >= 6:
                        return
                else:
                    idle = 0
                previous = count
                await self.page.mouse.wheel(0, 2800)
            await asyncio.sleep(1.4)

    async def _expand_truncated(self) -> None:
        for _ in range(8):
            clicked = False
            locator = self.page.get_by_role("button", name=SEE_MORE_RE)
            with contextlib.suppress(Exception):
                count = await locator.count()
                if count:
                    await locator.first.click()
                    clicked = True
            if not clicked:
                return
            await asyncio.sleep(0.35)

    async def _expand_comments(self) -> None:
        for _ in range(5):
            locator = self.page.get_by_text(VIEW_COMMENTS_RE)
            with contextlib.suppress(Exception):
                if await locator.count():
                    await locator.first.click()
                    await asyncio.sleep(0.8)
                    continue
            return

    async def _eval_posts(self) -> list[dict]:
        with contextlib.suppress(Exception):
            records = await self.page.evaluate(EXTRACT_POSTS_JS)
            if isinstance(records, list):
                return [item for item in records if isinstance(item, dict)]
        return []

    async def _eval_comments(self) -> list[dict]:
        with contextlib.suppress(Exception):
            records = await self.page.evaluate(EXTRACT_COMMENTS_JS)
            if isinstance(records, list):
                return [item for item in records if isinstance(item, dict)]
        return []

    async def _page_text(self) -> str:
        if self.page is None:
            return ""
        title = ""
        with contextlib.suppress(Exception):
            title = (await self.page.title() or "").lower()
        body = ""
        with contextlib.suppress(Exception):
            body = ((await self.page.inner_text("body", timeout=2000)) or "")[:2000].lower()
        url = ""
        with contextlib.suppress(Exception):
            url = (self.page.url or "").lower()
        # Include other tabs Facebook may have opened during login.
        extra = ""
        if self.context is not None:
            for opened in self.context.pages:
                with contextlib.suppress(Exception):
                    extra += f"\n{opened.url or ''}"
        return f"{url}\n{title}\n{body}{extra.lower()}"

    async def _has_cookie(self, name: str) -> bool:
        if self.context is None:
            return False
        with contextlib.suppress(Exception):
            cookies = await self.context.cookies()
            return any(cookie.get("name") == name for cookie in cookies)
        return False

    async def _has_feed(self) -> bool:
        if self.page is None:
            return False
        with contextlib.suppress(Exception):
            if await self.page.locator('[role="feed"]').count() > 0:
                return True
            if await self.page.locator(ARTICLE_SELECTOR).count() > 0:
                return True
        return False

    async def _snapshot_challenge(self) -> None:
        if self.page is None:
            return
        path = self.source.profile_dir.parent / "facebook-challenge.png"
        with contextlib.suppress(Exception):
            path.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(path), full_page=False)
        url = ""
        with contextlib.suppress(Exception):
            url = (self.page.url or "")[:120]
        logger.warning("facebook_challenge_snapshot", extra={"url": url})

    async def _page_state(self) -> str:
        url_and_text = await self._page_text()
        if await self._has_captcha_frame() or any(
            snippet in url_and_text for snippet in CAPTCHA_SNIPPETS
        ):
            return "captcha"
        if "/checkpoint" in url_and_text or "two_factor" in url_and_text:
            return "checkpoint"
        if any(snippet in url_and_text for snippet in CHECKPOINT_SNIPPETS):
            return "checkpoint"
        if any(snippet in url_and_text for snippet in BLOCK_SNIPPETS):
            return "blocked"
        logged_in = await self._has_cookie("c_user")
        if "/login" in url_and_text or (
            any(snippet in url_and_text for snippet in LOGIN_SNIPPETS) and not logged_in
        ):
            return "login"
        if logged_in or await self._has_feed():
            return "ready"
        return "empty"

    async def _has_captcha_frame(self) -> bool:
        if self.page is None:
            return False
        with contextlib.suppress(Exception):
            frames = self.page.locator(
                'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[src*="arkose"], '
                'iframe[title*="captcha" i]'
            )
            return await frames.count() > 0
        return False

    async def _wait_for(self, wait_for: str) -> str:
        deadline = time.monotonic() + HYDRATE_WAIT_SECONDS
        while time.monotonic() < deadline:
            state = await self._page_state()
            if state in {"ready", "login", "checkpoint", "blocked", "captcha"}:
                if wait_for == "feed" and state == "ready" and not await self._has_feed():
                    await asyncio.sleep(0.8)
                    continue
                return state
            await asyncio.sleep(1)
        return await self._page_state()
