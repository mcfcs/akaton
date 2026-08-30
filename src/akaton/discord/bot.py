from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from akaton.persistence.database import Database
from akaton.persistence.models import CandidateRow, EventRow, SearchRunRow

logger = logging.getLogger(__name__)


class AkatonBot(commands.Bot):
    def __init__(
        self,
        *,
        database: Database,
        authorized_user_id: int | None = None,
        guild_id: int | None = None,
        channel_id: int | None = None,
    ) -> None:
        # `guilds` is non-privileged and keeps the guild and channel cache warm, so the
        # notifier resolves its destination locally instead of over REST on every send.
        # No message, member, or presence intent is requested.
        super().__init__(command_prefix="!", intents=discord.Intents(guilds=True))
        self.database = database
        self.authorized_user_id = authorized_user_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.run_discovery = None
        self.run_backfill = None
        self._jobs: dict[str, asyncio.Task] = {}

    async def setup_hook(self) -> None:
        _register_commands(self)
        if self.guild_id:
            # Guild-scoped commands register immediately. A global sync can take up to an
            # hour to appear in the client, which looks like a bot that does not work.
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def allowed(self, interaction: discord.Interaction) -> bool:
        return self.authorized_user_id is None or interaction.user.id == self.authorized_user_id

    def start_job(self, name: str, coro_factory, report) -> bool:
        """Run a long job in the background and report to the channel when it finishes.

        A discovery cycle can outlast Discord's 15-minute interaction webhook, so the
        result is delivered as a channel message instead of an interaction follow-up.
        """
        existing = self._jobs.get(name)
        if existing and not existing.done():
            return False

        async def runner() -> None:
            try:
                summary = report(await coro_factory())
            except Exception as exc:
                logger.exception("discord_job_failed", extra={"job": name})
                summary = f"`{name}` failed: {type(exc).__name__}"
            await self.report_to_channel(summary)

        self._jobs[name] = asyncio.create_task(runner(), name=f"akaton-discord-{name}")
        return True

    async def report_to_channel(self, message: str) -> None:
        if not self.channel_id:
            return
        try:
            channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
            if hasattr(channel, "send"):
                await channel.send(message[:2000])
        except Exception:
            logger.exception("discord_report_failed", extra={"channel_id": self.channel_id})


def _register_commands(bot: AkatonBot) -> None:
    @bot.tree.command(name="upcoming", description="List upcoming competitions")
    @app_commands.describe(
        days="Maximum days ahead",
        category="Optional category such as HACKATHON",
        location="Optional city text",
    )
    async def upcoming(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = 30,
        category: str | None = None,
        location: str | None = None,
    ) -> None:
        if not bot.allowed(interaction):
            await interaction.response.send_message(
                "This personal bot is not configured for your user.", ephemeral=True
            )
            return
        async with bot.database.session() as session:
            query = select(EventRow).where(
                EventRow.event_phase.in_(["ANNOUNCED", "UPCOMING", "ONGOING"])
            )
            if category:
                query = query.where(EventRow.category == category.upper())
            rows = list(
                (
                    await session.scalars(query.order_by(EventRow.relevance_score.desc()).limit(20))
                ).all()
            )
        if location:
            rows = [
                row
                for row in rows
                if location.casefold() in str(row.current_facts.get("location", {})).casefold()
            ]
        now = datetime.now(UTC)
        cutoff = now + timedelta(days=days)
        dated_rows = []
        for row in rows:
            start_text = row.current_facts.get("event_start", {}).get("value")
            if not start_text:
                dated_rows.append(row)
                continue
            start = _as_aware(datetime.fromisoformat(start_text))
            if now <= start <= cutoff:
                dated_rows.append(row)
        rows = dated_rows
        content = _event_list(rows, f"Upcoming competitions (next {days} days)")
        await interaction.response.send_message(content, ephemeral=True)

    @bot.tree.command(name="deadlines", description="List approaching registration deadlines")
    async def deadlines(
        interaction: discord.Interaction, days: app_commands.Range[int, 1, 90] = 14
    ) -> None:
        if not bot.allowed(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        now = datetime.now(UTC)
        cutoff = now + timedelta(days=days)
        async with bot.database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(EventRow).where(EventRow.registration_state == "OPEN")
                    )
                ).all()
            )
        filtered = []
        for row in rows:
            deadline = row.current_facts.get("registration_deadline", {}).get("value")
            if deadline:
                parsed = _as_aware(datetime.fromisoformat(deadline))
                if now <= parsed <= cutoff:
                    filtered.append(row)
        await interaction.response.send_message(
            _event_list(filtered, f"Deadlines within {days} days"), ephemeral=True
        )

    @bot.tree.command(name="search-now", description="Run one discovery cycle")
    async def search_now(interaction: discord.Interaction) -> None:
        if not bot.allowed(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        if bot.run_discovery is None:
            await interaction.response.send_message("Discovery is not configured.", ephemeral=True)
            return
        started = bot.start_job(
            "discovery",
            bot.run_discovery,
            lambda counts: (
                f"Discovery complete: {counts['queries']} queries, "
                f"{counts['candidates']} candidates, {counts['processed']} processed, "
                f"{counts['errors']} errors."
            ),
        )
        await interaction.response.send_message(
            "Discovery started; the summary will be posted in this channel."
            if started
            else "Discovery is already running.",
            ephemeral=True,
        )

    @bot.tree.command(name="backfill", description="Test discovery against historical events")
    @app_commands.describe(
        since="Earliest search date in YYYY-MM-DD format",
        queries="Number of rotated queries to test",
    )
    async def backfill(
        interaction: discord.Interaction,
        since: str = "2026-08-01",
        queries: app_commands.Range[int, 1, 20] = 4,
    ) -> None:
        if not bot.allowed(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        if bot.run_backfill is None:
            await interaction.response.send_message("Backfill is not configured.", ephemeral=True)
            return
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            await interaction.response.send_message(
                "Use an ISO date such as `2026-08-01`.", ephemeral=True
            )
            return
        started = bot.start_job(
            "backfill",
            lambda: bot.run_backfill(since_date, queries),
            lambda counts: (
                "Historical test complete. Past-event and closed-registration gates were "
                f"bypassed for this run only: {counts['queries']} queries, "
                f"{counts['candidates']} candidates, {counts['processed']} processed, "
                f"{counts['errors']} errors."
            ),
        )
        await interaction.response.send_message(
            f"Historical test from {since_date.isoformat()} started with {queries} queries; "
            "the summary will be posted in this channel."
            if started
            else "A historical test is already running.",
            ephemeral=True,
        )

    @bot.tree.command(name="status", description="Show pipeline status")
    async def status(interaction: discord.Interaction) -> None:
        if not bot.allowed(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with bot.database.session() as session:
            candidate_count = int(await session.scalar(select(func.count(CandidateRow.id))) or 0)
            event_count = int(await session.scalar(select(func.count(EventRow.id))) or 0)
            last_search = await session.scalar(
                select(SearchRunRow).order_by(SearchRunRow.started_at.desc()).limit(1)
            )
        last = last_search.started_at.isoformat() if last_search else "never"
        await interaction.response.send_message(
            f"Candidates: {candidate_count}\nEvents: {event_count}\nLast search: {last}",
            ephemeral=True,
        )

    @bot.tree.command(name="why", description="Explain why an event was accepted")
    async def why(interaction: discord.Interaction, event_id: int) -> None:
        if not bot.allowed(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        async with bot.database.session() as session:
            event = await session.get(EventRow, event_id)
            candidates = list(
                (
                    await session.scalars(
                        select(CandidateRow).where(CandidateRow.event_id == event_id)
                    )
                ).all()
            )
        if not event:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        trace = candidates[-1].trace[-3:] if candidates and candidates[-1].trace else []
        summary = (
            f"**{event.title}**\nScore: {event.relevance_score}\n"
            f"Confidence: {event.confidence_score:.2f}\n"
            f"State: {event.event_phase}/{event.registration_state}\n"
            f"Recent trace: `{trace}`"
        )
        await interaction.response.send_message(
            summary[:1900],
            ephemeral=True,
        )


def _event_list(rows: list[EventRow], heading: str) -> str:
    if not rows:
        return f"**{heading}**\nNo matching events."
    lines = [f"**{heading}**"]
    for row in rows[:15]:
        link = f" — {row.canonical_url}" if row.canonical_url else ""
        category = row.category.replace("_", " ").title()
        lines.append(f"• **{row.title}** ({category}, {row.relevance_score}){link}")
    return "\n".join(lines)[:2000]


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
