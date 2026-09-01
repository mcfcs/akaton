from __future__ import annotations

import discord

from akaton.discord.notifier import DiscordNotifier
from akaton.domain.models import NotificationPayload

PAYLOAD = NotificationPayload(
    dedupe_key="k",
    notification_type="NEW_EVENT",
    event_id=1,
    event_version=1,
    title="ImaGnation 2026",
    # A scraped page can carry a mention of its own; it must not become a ping.
    description="Run by @everyone at <@123456789012345678> in Taguig.",
    fields={"Category": "BUSINESS_CASE"},
    official_url="https://gcash.com/imagnation",
    registration_url="https://gcash.com/imagnation",
    footer_token="tok",
    relevance_tier="RECOMMENDED",
    confidence_label="High",
)


class RecordingChannel(discord.TextChannel):
    def __init__(self) -> None:  # deliberately skips discord's __init__
        self.sent: dict = {}

    async def send(self, **kwargs):
        self.sent = kwargs
        return type("Message", (), {"id": 123})()


class StubClient:
    def __init__(self, channel) -> None:
        self._channel = channel

    def get_channel(self, _id):
        return self._channel


async def test_alerts_never_mention_anyone():
    channel = RecordingChannel()
    notifier = DiscordNotifier(StubClient(channel), 99)

    receipt = await notifier.send(PAYLOAD)

    assert receipt.message_id == "123"
    assert not channel.sent.get("content"), "an alert must not carry mention content"
    mentions = channel.sent["allowed_mentions"]
    assert mentions.users in (False, None, [])
    assert mentions.roles in (False, None, [])
    assert mentions.everyone in (False, None)


async def test_embed_still_carries_the_event_detail():
    channel = RecordingChannel()
    await DiscordNotifier(StubClient(channel), 99).send(PAYLOAD)
    embed = channel.sent["embed"]
    assert embed.title == "ImaGnation 2026"
    assert embed.url == "https://gcash.com/imagnation"
    # The footer also carries the relevance tier and confidence for the reader, so the
    # token is contained rather than equal — which is how reconciliation matches it too.
    assert "tok" in embed.footer.text
