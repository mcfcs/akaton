from __future__ import annotations

from dataclasses import replace

import httpx

from akaton.dashboard.web import create_dashboard
from akaton.persistence.database import Database


class FakeController:
    def __init__(self) -> None:
        self.triggered: list[str] = []
        self.scheduler = "PAUSED"

    def trigger(self, name: str) -> bool:
        self.triggered.append(name)
        return True

    def start_scheduler(self) -> bool:
        self.scheduler = "RUNNING"
        return True

    def pause_scheduler(self) -> bool:
        self.scheduler = "PAUSED"
        return True

    def status(self) -> dict:
        return {
            "scheduler": self.scheduler,
            "jobs": [],
            "running": {},
            "last_runs": {},
        }


async def test_dashboard_status_and_monitor_controls(config):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    controller = FakeController()
    app = create_dashboard(database, controller, config)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard"
    ) as client:
        page = await client.get("/")
        status = await client.get("/api/status")
        discover = await client.post("/api/actions/discover")
        started = await client.post("/api/actions/scheduler/start")
    assert page.status_code == 200
    # The page is served whole and mounts its own script; asserting on the shell rather
    # than on a headline keeps this from breaking every time the wording is edited.
    assert "<title>Akaton" in page.text
    assert 'id="detections-grid"' in page.text
    assert page.text.rstrip().endswith("</html>")
    assert status.json()["counts"] == {
        "candidates": 0,
        "events": 0,
        "notifications": 0,
    }
    assert discover.json()["accepted"] is True
    assert controller.triggered == ["discovery"]
    assert started.json()["state"] == "RUNNING"
    await database.close()


async def test_dashboard_token_protects_monitor_data(config):
    protected = replace(
        config,
        runtime=config.runtime.model_copy(update={"dashboard_token": "correct-horse"}),
    )
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    app = create_dashboard(database, FakeController(), protected)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard"
    ) as client:
        denied = await client.get("/api/status")
        allowed = await client.get("/api/status", headers={"X-Akaton-Token": "correct-horse"})
    assert denied.status_code == 401
    assert allowed.status_code == 200
    await database.close()
