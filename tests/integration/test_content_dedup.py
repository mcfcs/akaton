from __future__ import annotations

from sqlalchemy import func, select

from akaton.domain.models import CandidateSeed, DeliveryReceipt, FetchResult
from akaton.persistence.database import Database
from akaton.persistence.models import EventRow, NotificationRow
from akaton.pipeline import CandidatePipeline
from akaton.processing.dedup import content_similarity, is_same_announcement

# The three real philhacks posts for one DOST-NCR event, each on its own URL. Two share
# an opening; the third has a clause prepended, which shifts every token.
DOST_BODY = (
    "Your next web application could help build a more sustainable Philippines. As part "
    "of the 2026 Regional Science, Technology and Innovation Week, join the "
    "Hack4Sustainability hackathon in Manila. Registration is now open until September "
    "30, 2026. Open to professional developers. Cash prizes await the winning teams."
)
POSTS = {
    "https://www.facebook.com/groups/philhacks/permalink/4125912344210844/": (
        f"Calling all professional developers! {DOST_BODY}"
    ),
    "https://www.facebook.com/dost.ncr/posts/pfbid0NVNP7t5vQ1jJ2yYWCMphgT6pWxSNiKSA": (
        f"Calling all professional developers! {DOST_BODY}"
    ),
    "https://www.facebook.com/groups/philhacks/permalink/4121798424622236/": (
        f"The DOST- National Capital Region is calling all professional developers! {DOST_BODY}"
    ),
}


class RepostFetcher:
    """Serves whichever repost was asked for, as the Facebook adapter would."""

    async def fetch(self, url, **kwargs):
        text = POSTS[url]
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="prefetched",
            status_code=200,
            title=text.split(".")[0][:180],
            text=text,
            content_hash=url,
            usable=True,
        )


class CountingNotifier:
    def __init__(self) -> None:
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)
        return DeliveryReceipt(message_id=str(len(self.payloads)))


def test_reposts_are_recognised_as_the_same_announcement():
    texts = list(POSTS.values())
    assert is_same_announcement(texts[0], texts[1])
    assert is_same_announcement(texts[0], texts[2])


def test_a_genuinely_different_event_is_not_a_duplicate():
    other = (
        "Join us for the ClimateLaunchpad Philippines National Final on August 6 at 3PM "
        "via Zoom. Watch the finalists pitch their climate ventures."
    )
    assert content_similarity(list(POSTS.values())[0], other) < 85
    assert not is_same_announcement(list(POSTS.values())[0], other)


async def test_one_event_reposted_three_times_alerts_once(config, tmp_path):
    from dataclasses import replace

    enabled = replace(config, app=config.app.model_copy(update={"notifications_enabled": True}))
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dedup.db').as_posix()}")
    await database.create_schema()
    notifier = CountingNotifier()
    pipeline = CandidatePipeline(database, enabled, RepostFetcher(), notifier=notifier)

    for url in POSTS:
        await pipeline.process(
            CandidateSeed(
                url=url,
                discovery_channel="facebook",
                provider="facebook",
                query="philhacks",
            ),
            historical_test=True,
        )

    async with database.session() as session:
        events = await session.scalar(select(func.count(EventRow.id)))
        notifications = await session.scalar(select(func.count(NotificationRow.id)))
    assert events == 1, "three URLs, one announcement, one event"
    assert notifications == 1
    assert len(notifier.payloads) == 1
    await database.close()
