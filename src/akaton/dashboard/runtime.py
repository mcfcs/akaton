from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING


class MonitorController:
    def __init__(
        self,
        scheduler: AsyncIOScheduler,
        discovery: Callable[[], Awaitable[dict[str, int]]],
        refresh: Callable[[], Awaitable[dict[str, int]]],
    ) -> None:
        self.scheduler = scheduler
        self.discovery = discovery
        self.refresh = refresh
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_runs: dict[str, dict[str, Any]] = {}

    def trigger(self, name: str) -> bool:
        existing = self.tasks.get(name)
        if existing and not existing.done():
            return False
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
        }
