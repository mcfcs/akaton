from __future__ import annotations

from datetime import UTC, datetime, timedelta

from akaton.discovery.facebook import (
    clear_sticky_proxy_id,
    load_sticky_proxy_id,
    save_sticky_proxy_id,
)
from akaton.discovery.facebook_parse import (
    FacebookComment,
    FacebookPost,
    apply_graphql_records,
    comments_from_dom,
    group_feed_url,
    groups_from_config,
    is_platform_chrome,
    mention_kind,
    needs_comment_expansion,
    post_from_dom,
    records_from_graphql,
    records_from_html,
    thread_to_seeds,
    unwrap_facebook_url,
)

NOW = datetime(2026, 8, 31, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=90)


def _post(**overrides) -> FacebookPost:
    data = dict(
        post_id="4116540755148003",
        group="philhacks",
        permalink="https://www.facebook.com/groups/philhacks/permalink/4116540755148003/",
        text="hi is there any upcoming hackathon events near manila",
        created_at=NOW,
        comments=[],
    )
    data.update(overrides)
    return FacebookPost(**data)


def test_question_post_is_not_an_event_on_its_own():
    assert mention_kind("hi is there any upcoming hackathon events near manila") == "question"
    seeds = thread_to_seeds(_post(), cutoff=CUTOFF)
    assert seeds == []


def test_reply_with_a_listing_becomes_the_candidate():
    """The philhacks pattern: the post is a question, the event is in a comment."""
    post = _post(
        comments=[
            FacebookComment(
                comment_id="999",
                text="Hack4Gov 2026 registration is now open",
                urls=["https://devpost.com/hack4gov-2026"],
                author="someone",
            )
        ]
    )
    seeds = thread_to_seeds(post, cutoff=CUTOFF)
    assert len(seeds) == 1
    seed = seeds[0]
    assert str(seed.url) == "https://devpost.com/hack4gov-2026"
    assert seed.content is None
    assert seed.discovery_channel == "facebook"
    assert seed.source_key.startswith("fb:philhacks:4116540755148003:c:")


def test_direct_announcement_without_an_outbound_page_is_prefetched():
    post = _post(
        text="DICT Hack4Gov 2026 registration is now open. Deadline 30 September 2026. Manila.",
        urls=[],
    )
    seeds = thread_to_seeds(post, cutoff=CUTOFF)
    assert len(seeds) == 1
    seed = seeds[0]
    assert "facebook.com/groups/philhacks/permalink/4116540755148003" in str(seed.url)
    assert seed.content and "DICT Hack4Gov" in seed.content
    assert "philhacks" in seed.content
    # The group's configured country must not be written into the document: extraction
    # would then read the location off this harness text instead of off the post, which
    # is what made a Malaysian announcement come out as Philippine.
    assert "Philippines" not in seed.content.split("\n", 1)[0]


def test_google_form_stays_on_the_facebook_document():
    """forms.gle is a registration URL but authority 30, so it must not be followed."""
    post = _post(
        text="Shopee Code League 2026 applications now open",
        urls=["https://forms.gle/abc123"],
    )
    seeds = thread_to_seeds(post, cutoff=CUTOFF)
    assert len(seeds) == 1
    seed = seeds[0]
    assert "facebook.com" in str(seed.url)
    assert seed.links == ["https://forms.gle/abc123"]
    assert seed.content


def test_a_question_that_mentions_register_is_still_a_question():
    assert mention_kind("pwede po ba manuod if hindi naka register sa egov hackaton?") == "question"


def test_meta_account_notices_are_not_comments():
    """A real run scraped 40 of these, dragging accountscenter.facebook.com links along."""
    records = [
        {"text": "You're now using a Meta Account on Facebook.", "hrefs": []},
        {
            "text": (
                "We noticed a new login from a device or location you don't usually use. "
                "Please review it."
            ),
            "hrefs": ["https://accountscenter.facebook.com/password_and_security"],
        },
        {
            "text": "Meta Accounts are coming to Facebook. You'll be updated to yours soon.",
            "hrefs": [],
        },
        {"text": "Hack4Gov 2026 registration is now open", "hrefs": []},
    ]
    comments = comments_from_dom(records, _post())
    assert [comment.text for comment in comments] == ["Hack4Gov 2026 registration is now open"]


def test_platform_chrome_predicate_leaves_real_replies_alone():
    assert is_platform_chrome("You're now using a Meta Account on Facebook.") is True
    assert is_platform_chrome("Sino may alam na hackathon this month?") is False


def test_unrelated_comments_do_not_contribute_links_to_the_document():
    """Marketplace and promo replies were importing their URLs into seed.links."""
    post = _post(
        text="DICT Hack4Gov 2026 registration is now open. Deadline 30 September 2026.",
        comments=[
            FacebookComment(
                comment_id="1",
                text="Adidas Evo SL BAPE Mismatched Sz 7 / 9.5m PHP 13,999 pandacan, Manila",
                urls=["https://jollibee.onelink.me/U65H/ppu7ow43"],
            )
        ],
    )
    seeds = thread_to_seeds(post, cutoff=CUTOFF)
    assert len(seeds) == 1
    assert "jollibee.onelink.me" not in (seeds[0].content or "")
    assert "Adidas" not in (seeds[0].content or "")


def test_a_research_conference_is_not_treated_as_a_hackathon():
    assert mention_kind("REGISTRATION IS NOW OPEN: RESEARCH CONFERENCE 2026!") == "unrelated"


def test_teammate_and_recap_posts_are_dropped():
    teammate = _post(text="Looking for teammates for a hackathon this weekend, anyone joining?")
    recap = _post(text="Congratulations to the winners of last weekend's hackathon!")
    assert mention_kind(teammate.text) == "teammate"
    assert mention_kind(recap.text) == "recap"
    assert thread_to_seeds(teammate, cutoff=CUTOFF) == []
    assert thread_to_seeds(recap, cutoff=CUTOFF) == []


def test_two_replies_with_distinct_listings_become_two_seeds():
    post = _post(
        comments=[
            FacebookComment(
                comment_id="1",
                text="This one: https://devpost.com/alpha-hack",
                urls=["https://devpost.com/alpha-hack"],
            ),
            FacebookComment(
                comment_id="2",
                text="And also https://unstop.com/hackathons/beta",
                urls=["https://unstop.com/hackathons/beta"],
            ),
        ]
    )
    seeds = thread_to_seeds(post, cutoff=CUTOFF)
    urls = {str(seed.url) for seed in seeds}
    assert urls == {
        "https://devpost.com/alpha-hack",
        "https://unstop.com/hackathons/beta",
    }


def test_old_threads_are_dropped():
    post = _post(created_at=NOW - timedelta(days=120), text="DICT Hack4Gov registration now open")
    assert thread_to_seeds(post, cutoff=CUTOFF) == []


def test_lphp_wrapper_is_unwrapped_to_the_real_url():
    wrapped = "https://l.facebook.com/l.php?u=https%3A%2F%2Fdevpost.com%2Fhack4gov&h=AT"
    assert unwrap_facebook_url(wrapped) == "https://devpost.com/hack4gov"


def test_group_feed_url_is_chronological():
    url = group_feed_url("https://www.facebook.com/groups/philhacks/")
    assert url.startswith("https://www.facebook.com/groups/philhacks/")
    assert "sorting_setting=CHRONOLOGICAL" in url


def test_default_config_targets_philhacks():
    groups = groups_from_config(None)
    assert any(group.name == "philhacks" for group in groups)


def test_question_posts_always_open_comments():
    post = _post()
    assert needs_comment_expansion(post) is True
    announcement = _post(
        text="DICT Hack4Gov 2026 registration is now open",
        comment_count=0,
        comments=[],
    )
    assert needs_comment_expansion(announcement) is False


def test_dom_record_recovers_permalink_and_outbound_link():
    record = {
        "text": "Jane Dela Cruz\n2d\nDICT Hack4Gov 2026 registration is now open\nLike\nReply",
        "hrefs": [
            "https://www.facebook.com/groups/philhacks/permalink/4116540755148003/",
            "https://l.facebook.com/l.php?u=https%3A%2F%2Fdict.gov.ph%2Fhack4gov",
        ],
        "author": "Jane Dela Cruz",
    }
    post = post_from_dom(record, "philhacks")
    assert post is not None
    assert post.post_id == "4116540755148003"
    assert "DICT Hack4Gov" in post.text
    assert "Like" not in post.text
    assert "https://dict.gov.ph/hack4gov" in post.urls


def test_graphql_story_and_comment_are_walked_out_of_nested_payloads():
    blob = """
    {"data":{"node":{"post_id":"4116540755148003","message":{"text":"any upcoming hackathon?"},
      "url":"https://www.facebook.com/groups/philhacks/permalink/4116540755148003/",
      "creation_time":1756620000}}}
    {"data":{"node":{"body":{"text":"Hack4Gov 2026 is open https://devpost.com/hack4gov-2026"}}}}
    """
    records = records_from_graphql([blob])
    kinds = {record["kind"] for record in records}
    assert "post" in kinds
    assert "comment" in kinds
    post = _post(text="")
    apply_graphql_records(post, records)
    assert "any upcoming hackathon" in post.text
    assert any("devpost.com/hack4gov-2026" in comment.text for comment in post.comments)


def test_hydration_script_tags_are_parsed_from_html():
    html = """
    <html><head></head><body>
    <script type="application/json">
    {"require":[{"message":{"text":"Shopee Code League 2026 registration is now open"},
      "post_id":"111","url":"https://www.facebook.com/groups/philhacks/posts/111/"}]}
    </script>
    </body></html>
    """
    records = records_from_html(html)
    assert any(record.get("post_id") == "111" for record in records)


def test_sticky_proxy_id_survives_a_restart(tmp_path):
    save_sticky_proxy_id(tmp_path, "abc123")
    assert load_sticky_proxy_id(tmp_path) == "abc123"
    clear_sticky_proxy_id(tmp_path)
    assert load_sticky_proxy_id(tmp_path) is None
