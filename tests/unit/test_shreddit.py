from __future__ import annotations

from datetime import UTC, datetime, timedelta

from akaton.discovery.shreddit import _to_seed, search_url
from akaton.discovery.shreddit_parse import parse_shreddit_html

NOW = datetime.now(UTC)
RECENT = NOW.strftime("%Y-%m-%dT%H:%M:%S.000000+0000")

# Shaped like a www.reddit.com search listing: the fields live on the custom element's
# attributes, because the page is server-rendered and the JSON endpoints are blocked.
LISTING = f"""
<html><body>
<shreddit-post id="t3_abc123" post-title="Shopee AI Hackathon 2026 applications now open"
  score="128" comment-count="14" author="someone" subreddit-name="Philippines"
  permalink="/r/Philippines/comments/abc123/shopee_ai_hackathon/"
  created-timestamp="{RECENT}" post-type="text">
  <div id="t3_abc123-post-rtjson-content">Open to students. Deadline September 30, 2026.</div>
</shreddit-post>
<shreddit-post id="t3_def456" post-title="GCash ImaGnation case competition"
  score="42" comment-count="3" author="other" subreddit-name="Philippines"
  permalink="/r/Philippines/comments/def456/imagnation/"
  content-href="https://gcash.com/imagnation"
  created-timestamp="{RECENT}" post-type="link">
</shreddit-post>
<shreddit-post id="t3_ad999" post-title="Buy something" class="promoted" ad-type="banner"
  subreddit-name="Philippines" permalink="/r/Philippines/comments/ad999/ad/"
  created-timestamp="{RECENT}" post-type="link"></shreddit-post>
</body></html>
"""


def test_listing_posts_are_parsed_from_custom_elements():
    submissions, _ = parse_shreddit_html(LISTING, fallback_subreddit="Philippines")
    titles = [s["title"] for s in submissions]
    assert "Shopee AI Hackathon 2026 applications now open" in titles
    assert "GCash ImaGnation case competition" in titles


def test_promoted_posts_are_skipped():
    submissions, _ = parse_shreddit_html(LISTING, fallback_subreddit="Philippines")
    assert all("Buy something" != s["title"] for s in submissions)


def test_selftext_is_recovered_from_the_rendered_body():
    submissions, _ = parse_shreddit_html(LISTING, fallback_subreddit="Philippines")
    post = next(s for s in submissions if s["id"] == "abc123")
    assert "Deadline September 30, 2026" in post["selftext"]


def _cutoff() -> datetime:
    return NOW - timedelta(days=90)


def test_self_post_carries_its_body_because_reddit_cannot_be_fetched():
    submissions, _ = parse_shreddit_html(LISTING, fallback_subreddit="Philippines")
    post = next(s for s in submissions if s["id"] == "abc123")
    seed = _to_seed(post, _cutoff(), "reddit")
    assert seed is not None
    assert str(seed.url).startswith("https://www.reddit.com/r/Philippines/comments/abc123")
    assert seed.content and "Deadline September 30, 2026" in seed.content
    assert seed.discovery_channel == "reddit"
    assert seed.source_key == "t3_abc123"


def test_link_post_points_at_the_event_page_and_is_fetched():
    submissions, _ = parse_shreddit_html(LISTING, fallback_subreddit="Philippines")
    post = next(s for s in submissions if s["id"] == "def456")
    seed = _to_seed(post, _cutoff(), "reddit")
    assert seed is not None
    assert str(seed.url) == "https://gcash.com/imagnation"
    # The linked page is authoritative, so nothing is carried from the post body.
    assert seed.content is None


def test_posts_older_than_the_window_are_dropped():
    submissions, _ = parse_shreddit_html(LISTING, fallback_subreddit="Philippines")
    post = next(s for s in submissions if s["id"] == "abc123")
    assert _to_seed(post, NOW + timedelta(days=1), "reddit") is None


def test_search_url_restricts_to_the_subreddit():
    url = search_url("Philippines", "case competition")
    assert url.startswith("https://www.reddit.com/r/Philippines/search/")
    assert "restrict_sr=1" in url
    assert "case+competition" in url


def test_malformed_html_yields_nothing_rather_than_raising():
    submissions, _ = parse_shreddit_html("<html><body><shreddit-post", fallback_subreddit="x")
    assert submissions == []
