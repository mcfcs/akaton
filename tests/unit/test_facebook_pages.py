"""Reading organizer pages, not just the group.

Search finds these announcements constantly and cannot read one of them: on a real run,
62 of the 134 candidates rejected as SEARCH_SNIPPET_ONLY were facebook.com pages —
including the GCash post announcing ImaGnation, which is why that competition was found
and then discarded. A page is the same DOM as a group behind a different URL, so the
collector treats both alike and only the URL building and the comment policy differ.
"""

from __future__ import annotations

from akaton.discovery.facebook_parse import (
    FacebookPost,
    feed_url,
    groups_from_config,
    needs_comment_expansion,
    page_feed_url,
    pages_from_config,
    permalink_url,
    post_from_dom,
    thread_to_seeds,
)

CONFIG = {
    "groups": [{"url": "https://www.facebook.com/groups/philhacks/", "name": "philhacks"}],
    "pages": [
        {"url": "https://www.facebook.com/wearegcash", "name": "GCash"},
        {"url": "https://www.facebook.com/DICTgovph", "name": "DICT"},
    ],
}


class TestTargets:
    def test_pages_are_read_from_their_own_timeline(self):
        pages = pages_from_config(CONFIG)
        assert [page.name for page in pages] == ["GCash", "DICT"]
        assert feed_url(pages[0]) == "https://www.facebook.com/wearegcash"

    def test_a_group_still_asks_for_chronological_order(self):
        group = groups_from_config(CONFIG)[0]
        assert "groups/philhacks" in feed_url(group)
        assert "CHRONOLOGICAL" in feed_url(group)

    def test_a_bare_slug_is_accepted(self):
        assert pages_from_config({"pages": ["wearegcash"]})[0].url == (
            "https://www.facebook.com/wearegcash"
        )

    def test_no_pages_configured_is_not_an_error(self):
        assert pages_from_config({}) == ()
        assert pages_from_config(None) == ()

    def test_a_page_url_is_not_mistaken_for_a_group(self):
        assert page_feed_url("https://www.facebook.com/DICTgovph/posts/123") == (
            "https://www.facebook.com/DICTgovph"
        )


class TestPermalinks:
    def test_a_page_post_lives_under_the_page(self):
        """`/groups/<slug>/permalink/<id>` on a page resolves to nothing while still
        looking plausible, which is the worst kind of wrong link to put in an alert."""
        assert permalink_url("wearegcash", "999", kind="page") == (
            "https://www.facebook.com/wearegcash/posts/999/"
        )

    def test_a_group_post_is_unchanged(self):
        assert permalink_url("philhacks", "999") == (
            "https://www.facebook.com/groups/philhacks/permalink/999/"
        )

    def test_the_kind_travels_with_the_post(self):
        post = post_from_dom(
            {"post_id": "77", "text": "Registration is now open.", "hrefs": []},
            "wearegcash",
            "page",
        )
        assert post.kind == "page"
        assert post.group == "wearegcash"
        assert post.permalink == "https://www.facebook.com/wearegcash/posts/77/"


class TestCommentPolicy:
    def test_a_page_post_never_opens_its_comments(self):
        """A page timeline is mostly marketing, which classifies as "unrelated" — one of
        the kinds that opens a thread. Every one of those would spend a permalink and
        several seconds reading replies that say nothing."""
        marketing = FacebookPost(
            post_id="1",
            group="wearegcash",
            permalink="https://www.facebook.com/wearegcash/posts/1/",
            text="At GCash, building the future starts with empowering our people.",
            kind="page",
        )
        assert needs_comment_expansion(marketing) is False

    def test_a_group_question_still_does(self):
        """This is the philhacks pattern: someone asks, someone else replies with it."""
        question = FacebookPost(
            post_id="2",
            group="philhacks",
            permalink="https://www.facebook.com/groups/philhacks/permalink/2/",
            text="Anyone know any upcoming hackathon near Manila?",
        )
        assert needs_comment_expansion(question) is True


def test_a_page_announcement_becomes_a_candidate():
    """The whole point: the GCash post we already find, now readable."""
    post = post_from_dom(
        {
            "post_id": "4242",
            "text": (
                "Are you G to innovate for #NegosyoNation? ImaGnation 2026 is here. "
                "Registration is now open to university students in Quezon City, "
                "Philippines. Registration deadline September 30, 2026."
            ),
            "hrefs": ["https://gcash.com/imagnation"],
        },
        "wearegcash",
        "page",
    )
    seeds = thread_to_seeds(post, provider="facebook", query="GCash")
    assert seeds, "an announcement on an organizer's page must produce a candidate"
    assert any("gcash.com" in str(seed.url) or "wearegcash" in str(seed.url) for seed in seeds)
