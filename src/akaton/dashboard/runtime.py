from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING

logger = logging.getLogger(__name__)


class BotController:
    """Starts and stops the Discord connection without taking the dashboard down.

    discord.py clients cannot be reused once closed, so each start builds a fresh one
    through the factory. `on_start` re-wires whatever holds a reference to the client —
    the notifier, and the slash commands' job callables — onto the new instance.
    """

    def __init__(
        self,
        factory: Callable[[], Any] | None = None,
        token: str | None = None,
        *,
        on_start: Callable[[Any], None] | None = None,
    ) -> None:
        self.factory = factory
        self.token = token
        self.on_start = on_start
        self.bot: Any | None = None
        self.task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.started_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.factory and self.token)

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    async def start(self) -> bool:
        if not self.configured or self.running:
            return False
        self.last_error = None
        bot = self.factory()
        if self.on_start:
            self.on_start(bot)
        self.bot = bot
        self.started_at = datetime.now(UTC)

        async def runner() -> None:
            try:
                await bot.start(self.token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("discord_bot_stopped")

        self.task = asyncio.create_task(runner(), name="discord")
        return True

    async def stop(self) -> bool:
        if not self.running:
            return False
        task, bot = self.task, self.bot
        self.task = None
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.close()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.bot = None
        self.started_at = None
        return True

    def status(self) -> dict[str, Any]:
        if not self.configured:
            state = "NOT_CONFIGURED"
        elif self.running:
            state = "RUNNING"
        else:
            state = "STOPPED"
        return {
            "state": state,
            "user": str(getattr(self.bot, "user", "") or "") if self.running else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_error": self.last_error,
        }


class MonitorController:
    def __init__(
        self,
        scheduler: AsyncIOScheduler,
        discovery: Callable[[], Awaitable[dict[str, int]]],
        refresh: Callable[[], Awaitable[dict[str, int]]],
        *,
        sources: list[str] | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.discovery = discovery
        self.refresh = refresh
        # Collectors a backdate may name. "search" is always available; the rest are
        # whichever adapters this deployment actually enabled, so the dashboard offers
        # exactly what exists rather than a hardcoded list.
        self.sources = sources or ["search"]
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_runs: dict[str, dict[str, Any]] = {}

    def trigger(
        self, name: str, job: Callable[[], Awaitable[dict[str, int]]] | None = None
    ) -> bool:
        """Start a named job unless one of that name is already running.

        `job` overrides the default for the name, which is how a backdate passes its
        date and collector list while still sharing the single-flight guard: two
        backfills cannot overlap, and neither can a backfill with itself.
        """
        existing = self.tasks.get(name)
        if existing and not existing.done():
            return False
        if job is None:
            job = self.discovery if name == "discovery" else self.refresh
        self.tasks[name] = asyncio.create_task(self._run(name, job), name=f"akaton-{name}")
        return True

    async def _run(self, name: str, job: Callable[[], Awaitable[dict[str, int]]]) -> None:
        record: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "status": "RUNNING",
        }
        self.last_runs[name] = record
        try:
            record["result"] = await job()
            record["status"] = "SUCCEEDED"
        except Exception as exc:
            record["status"] = "FAILED"
            record["error"] = type(exc).__name__
        finally:
            record["completed_at"] = datetime.now(UTC).isoformat()

    def start_scheduler(self) -> bool:
        if self.scheduler.state == STATE_RUNNING:
            return False
        if self.scheduler.state == STATE_PAUSED:
            self.scheduler.resume()
        else:
            self.scheduler.start()
        return True

    def pause_scheduler(self) -> bool:
        if self.scheduler.state != STATE_RUNNING:
            return False
        self.scheduler.pause()
        return True

    def status(self) -> dict[str, Any]:
        state = {
            STATE_RUNNING: "RUNNING",
            STATE_PAUSED: "PAUSED",
        }.get(self.scheduler.state, "STOPPED")
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )
        running = {name: bool(task and not task.done()) for name, task in self.tasks.items()}
        return {
            "scheduler": state,
            "jobs": jobs,
            "running": running,
            "last_runs": self.last_runs,
            "sources": self.sources,
        }
