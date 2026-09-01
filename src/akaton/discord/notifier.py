from __future__ import annotations

import discord

from akaton.discord.embeds import embed_dict
from akaton.domain.models import DeliveryReceipt, NotificationPayload
from akaton.persistence.database import Database
from akaton.persistence.models import NotificationRow
from akaton.persistence.repository import Repository


class DiscordNotifier:
    def __init__(self, client: discord.Client, channel_id: int) -> None:
        self.client = client
        self.channel_id = channel_id

    async def send(self, payload: NotificationPayload) -> DeliveryReceipt:
        channel = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(
            self.channel_id
        )
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
            raise TypeError("Configured Discord destination is not a message channel")
        # Built by discord.embeds.embed_dict so the gateway path and the REST backfill
        # tool cannot drift apart on escaping or link trust.
        embed = discord.Embed.from_dict(embed_dict(payload))
        # Alerts never ping. AllowedMentions.none() also neutralises any mention that a
        # scraped title or description happens to contain.
        message = await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return DeliveryReceipt(message_id=str(message.id))

    async def find_footer_token(self, token: str, *, limit: int = 100) -> str | None:
        channel = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(
            self.channel_id
        )
        if not hasattr(channel, "history"):
            return None
        async for message in channel.history(limit=limit):
            if message.author.id != self.client.user.id:  # type: ignore[union-attr]
                continue
            # Substring, not equality: the footer also carries the relevance tier and
            # confidence for the reader. The token is unique enough to identify the
            # message on its own, and this keeps how the footer *reads* independent of
            # what it is *for*.
            if any(embed.footer and token in (embed.footer.text or "") for embed in message.embeds):
                return str(message.id)
        return None


async def reconcile_pending_notifications(database: Database, notifier: DiscordNotifier) -> int:
    async with database.session() as session:
        pending = [
            (row.id, NotificationPayload.model_validate(row.payload_json))
            for row in await Repository(session).pending_notifications()
        ]
    reconciled = 0
    for notification_id, payload in pending:
        try:
            message_id = await notifier.find_footer_token(payload.footer_token)
            if message_id is None:
                message_id = (await notifier.send(payload)).message_id
            async with database.session() as session:
                row = await session.get(NotificationRow, notification_id)
                if row:
                    await Repository(session).mark_notification_sent(row, message_id)
            reconciled += 1
        except Exception as exc:
            async with database.session() as session:
                row = await session.get(NotificationRow, notification_id)
                if row:
                    await Repository(session).mark_notification_failed(
                        row, f"reconciliation {type(exc).__name__}: {exc}"
                    )
    return reconciled
