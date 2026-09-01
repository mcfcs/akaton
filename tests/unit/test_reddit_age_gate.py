"""Skipping Reddit's over-18 interstitial instead of waiting it out.

Some subreddits and some individual posts are marked NSFW, and a logged-out visitor gets
an age gate rather than the feed. It is not a block: no proxy rotation, retry or wait gets
past it, and the only way through is to assert an age and log in — which the monitor has
no business doing. So it is detected and skipped.

The trap this guards against is the obvious implementation. `_page_text` includes the
navigation bar, and every Reddit page ever served has "Log in" in it, so keying the gate
on login wording would match the entire site and silently skip all of it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akaton.discovery.shreddit import page_state

# Reddit's actual over-18 interstitial.
AGE_GATE = (
    "reddit - the heart of the internet\n"
    "skip to main content\nlog in\nget app\n"
    "you must be 18+ to view this community\n"
    "r/example is a community for adults. you must be 18 or older to continue.\n"
    "yes, i'm over 18   no, take me back"
)
# A perfectly ordinary logged-out page. Note the navigation.
NORMAL = (
    "r/pinoyprogrammer on reddit: anyone joining the egov hackathon?\n"
    "skip to main content\nopen menu\nlog in\nsign up\nget app\n"
    "posted by u/someone 3 days ago\nanyone joining the egov hackathon this year?"
)
BLOCKED = (
    "blocked\nwhoa there, pardner\n"
    "your request has been blocked due to a network policy.\nfile a ticket"
)


class TestDetection:
    def test_the_age_gate_is_recognised(self):
        assert page_state(AGE_GATE) == "gated"

    def test_an_ordinary_page_is_not(self):
        """The whole risk: "Log in" is in the nav bar of every page on the site."""
        assert page_state(NORMAL) == "empty"

    def test_a_real_block_is_still_a_block(self):
        """Rotating the proxy is right for this and wrong for a gate, so they must differ."""
        assert page_state(BLOCKED) == "blocked"

    @pytest.mark.parametrize(
        "wording",
        [
            "you must be 18+ to view this community",
            "are you over 18?",
            "yes, i'm over 18",
            "no, take me back",
            "this community is nsfw",
            "this post contains adult content",
            "mature content warning",
        ],
    )
    def test_the_wordings_reddit_actually_uses(self, wording):
        assert page_state(f"reddit\nskip to main content\nlog in\n{wording}") == "gated"

    def test_case_does_not_matter(self):
        assert page_state("YOU MUST BE 18+ TO VIEW THIS COMMUNITY") == "gated"

    def test_an_empty_page_is_empty(self):
        assert page_state("") == "empty"

    def test_a_gate_wins_over_a_block_phrase(self):
        """A gate page can also mention verification; rotating past it achieves nothing."""
        assert page_state("you must be 18+ to view this community\nverify you are human") == "gated"


class FakeSession:
    """Stands in for the browser: every navigation reports the state we choose."""

    def __init__(self, gated_subs=(), gated_posts=()) -> None:
        self.gated_subs = set(gated_subs)
        self.gated_posts = set(gated_posts)
        self.visited: set[str] = set()
        self.last_state = "empty"
        self.searches: list[str] = []
        self.posts: list[str] = []

    async def search_permalinks(self, url: str) -> list[str]:
        self.searches.append(url)
        if any(f"r/{sub}/" in url for sub in self.gated_subs):
            self.last_state = "gated"
            return []
        self.last_state = "feed"
        return [f"https://www.reddit.com/r/x/comments/{len(self.searches)}/post/"]

    async def post_record(self, permalink: str, subreddit: str):
        self.posts.append(permalink)
        self.visited.add(permalink)
        if permalink in self.gated_posts:
            self.last_state = "gated"
            return None
        self.last_state = "feed"
        return {
            "id": "1",
            "title": "Manila hackathon 2026 registration is now open",
            "selftext": "Join the hackathon.",
            "permalink": "/r/x/comments/1/post/",
            "url": "https://dict.gov.ph/hackathon",
            "subreddit": subreddit,
            "created_utc": datetime.now(UTC).timestamp(),
            "name": "t3_1",
        }

    async def close(self) -> None:
        return None


async def _run(monkeypatch, source, session):
    """Drive `discover()` with the browser replaced."""
    import contextlib as _contextlib

    import akaton.discovery.shreddit as module

    @_contextlib.asynccontextmanager
    async def fake_playwright():
        yield object()

    monkeypatch.setattr("patchright.async_api.async_playwright", fake_playwright)
    monkeypatch.setattr(module, "_BrowserSession", lambda *a, **k: session)
    return await source.discover()


class TestSkipping:
    async def test_a_gated_subreddit_is_abandoned_after_one_search(self, monkeypatch):
        """Every remaining term would hit the same wall, so paying for them is waste."""
        from akaton.discovery.shreddit import ShredditSource

        source = ShredditSource(
            subreddits=("PinoyProgrammer", "SomeNSFWSub"),
            terms=("hackathon", "ideathon", "datathon"),
            min_interval_seconds=0,
        )
        session = FakeSession(gated_subs={"SomeNSFWSub"})
        await _run(monkeypatch, source, session)

        assert source.gated_subreddits == ["SomeNSFWSub"]
        gated_searches = [url for url in session.searches if "SomeNSFWSub" in url]
        assert len(gated_searches) == 1, "one navigation, not one per term"
        assert len([u for u in session.searches if "PinoyProgrammer" in u]) == 3

    async def test_a_gated_post_skips_only_itself(self, monkeypatch):
        from akaton.discovery.shreddit import ShredditSource

        source = ShredditSource(
            subreddits=("PinoyProgrammer",),
            terms=("hackathon", "ideathon"),
            min_interval_seconds=0,
        )
        first = "https://www.reddit.com/r/x/comments/1/post/"
        session = FakeSession(gated_posts={first})
        seeds = await _run(monkeypatch, source, session)

        assert source.gated_subreddits == [], "the subreddit itself was readable"
        assert len(session.posts) == 2, "the second post was still read"
        assert seeds, "and still produced a candidate"

    async def test_every_subreddit_gated_is_reported(self, monkeypatch):
        """Not a collector fault, but the run could never have found anything and the
        fix is a config change rather than a retry."""
        from akaton.discovery.shreddit import ShredditSource

        source = ShredditSource(
            subreddits=("OneNSFW", "AnotherNSFW"), terms=("hackathon",), min_interval_seconds=0
        )
        await _run(monkeypatch, source, FakeSession(gated_subs={"OneNSFW", "AnotherNSFW"}))

        assert source.gated_subreddits == ["OneNSFW", "AnotherNSFW"]
        assert "age-gated" in (source.last_error or "")
        assert "OneNSFW" in source.last_error

    async def test_a_readable_run_reports_no_error(self, monkeypatch):
        from akaton.discovery.shreddit import ShredditSource

        source = ShredditSource(
            subreddits=("PinoyProgrammer",), terms=("hackathon",), min_interval_seconds=0
        )
        await _run(monkeypatch, source, FakeSession())
        assert source.last_error is None
        assert source.gated_subreddits == []


class TestVocabularySafety:
    """These are the phrases that would have made the collector skip everything."""

    @pytest.mark.parametrize(
        "phrase",
        ["log in", "sign up", "log in to reddit", "get app", "continue with google"],
    )
    def test_ordinary_navigation_never_reads_as_a_gate(self, phrase):
        assert page_state(f"r/itphilippines\nskip to main content\n{phrase}\nsome post") == "empty"

    def test_a_post_that_merely_discusses_a_topic_is_not_gated(self):
        """A subreddit about competitions will contain the word "challenge" constantly."""
        text = "r/pinoyprogrammer\nlog in\nis the hackathon open to adults or students only?"
        assert page_state(text) == "empty"
