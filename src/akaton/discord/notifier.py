from __future__ import annotations

import discord

from akaton.domain.models import DeliveryReceipt, NotificationPayload
from akaton.persistence.database import Database
from akaton.persistence.models import NotificationRow
from akaton.persistence.repository import Repository


class DiscordNotifier:
    def __init__(
        self, client: discord.Client, channel_id: int, *, user_id: int | None = None
    ) -> None:
        self.client = client
        self.channel_id = channel_id
        self.user_id = user_id

    async def send(self, payload: NotificationPayload) -> DeliveryReceipt:
        channel = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(
            self.channel_id
        )
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
            raise TypeError("Configured Discord destination is not a message channel")
        color = (
            discord.Color.red()
            if payload.relevance_tier == "HIGH_PRIORITY"
            else discord.Color.green()
        )
        embed = discord.Embed(
            title=payload.title[:256],
            description=(payload.description or "")[:4096],
            color=color,
            url=payload.official_url,
        )
        for name, value in payload.fields.items():
            embed.add_field(name=name[:256], value=value[:1024] or "Not specified", inline=False)
        links = []
        if payload.registration_url:
            links.append(f"[Register]({payload.registration_url})")
        if payload.official_url:
            links.append(f"[Official announcement]({payload.official_url})")
        if links:
            embed.add_field(name="Links", value=" · ".join(links), inline=False)
        embed.add_field(name="Confidence", value=payload.confidence_label, inline=True)
        embed.add_field(
            name="Relevance", value=payload.relevance_tier.replace("_", " ").title(), inline=True
        )
        embed.set_footer(text=payload.footer_token)
        content = f"<@{self.user_id}>" if self.user_id else None
        message = await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
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
            if any(embed.footer and embed.footer.text == token for embed in message.embeds):
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
