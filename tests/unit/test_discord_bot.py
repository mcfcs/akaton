from __future__ import annotations

import asyncio

import pytest

from akaton.discord.bot import AkatonBot
from akaton.persistence.database import Database


@pytest.fixture
async def bot():
    database = Database("sqlite+aiosqlite:///:memory:")
    instance = AkatonBot(database=database, authorized_user_id=42, guild_id=7, channel_id=99)
    instance.reported: list[str] = []

    async def capture(message: str) -> None:
        instance.reported.append(message)

    instance.report_to_channel = capture
    yield instance
    await database.close()


async def test_long_job_reports_to_the_channel_instead_of_the_interaction(bot):
    """A discovery run can outlast Discord's 15-minute follow-up window."""
    released = asyncio.Event()

    async def job():
        await released.wait()
        return {"queries": 2, "candidates": 5}

    assert bot.start_job("discovery", job, lambda c: f"done {c['candidates']}") is True
    # Still running: a second invocation must not start a duplicate run.
    assert bot.start_job("discovery", job, lambda c: "duplicate") is False
    assert bot.reported == []

    released.set()
    await bot._jobs["discovery"]
    assert bot.reported == ["done 5"]


async def test_job_failure_is_reported_rather_than_swallowed(bot):
    async def job():
        raise RuntimeError("boom")

    assert bot.start_job("backfill", job, lambda c: "unreachable") is True
    await bot._jobs["backfill"]
    assert bot.reported == ["`backfill` failed: RuntimeError"]


async def test_finished_job_can_be_started_again(bot):
    async def job():
        return {"candidates": 1}

    assert bot.start_job("discovery", job, lambda c: "first") is True
    await bot._jobs["discovery"]
    assert bot.start_job("discovery", job, lambda c: "second") is True
    await bot._jobs["discovery"]
    assert bot.reported == ["first", "second"]


def test_only_the_configured_user_is_authorized(bot):
    class Interaction:
        def __init__(self, user_id: int) -> None:
            self.user = type("User", (), {"id": user_id})()

    assert bot.allowed(Interaction(42)) is True
    # The bot's own application ID is a common misconfiguration and must not pass.
    assert bot.allowed(Interaction(1543604617600827392)) is False
