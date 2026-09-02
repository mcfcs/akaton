"""Changing how the scraper notifies, from the dashboard.

A setting that only takes effect until the next restart is a setting that quietly lies,
so these check both halves: the running `ConfigBundle` the pipeline is holding, and the
YAML on disk that the next start reads.
"""

from __future__ import annotations

import shutil

import httpx
import pytest
import yaml

from akaton.config import load_config
from akaton.dashboard.runtime import BotController
from akaton.dashboard.settings import current_settings, update_settings
from akaton.dashboard.web import create_dashboard
from akaton.persistence.database import Database


class FakeController:
    def __init__(self) -> None:
        self.sources = ["search"]

    def status(self) -> dict:
        return {"scheduler": "STOPPED", "jobs": [], "running": {}, "last_runs": {}, "sources": []}

    def trigger(self, name, job=None) -> bool:
        return True


@pytest.fixture
def config(tmp_path, project_root):
    """A throwaway copy of the real configuration.

    These tests write to config/*.yaml by design, and the shared `config` fixture is
    session-scoped and rooted in the working tree — using it would edit the repository's
    own settings and leak the mutation into every test that ran afterwards.
    """
    shutil.copytree(project_root / "config", tmp_path / "config")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    return load_config(tmp_path, allow_example_profile=True)


@pytest.fixture
async def client(config):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    app = create_dashboard(database, FakeController(), config, bot=BotController())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dashboard"
    ) as http:
        yield http
    await database.close()


async def test_the_page_offers_every_setting_it_can_change(client):
    body = (await client.get("/api/settings")).json()
    keys = {control["key"] for control in body["controls"]}
    assert keys == {
        "notifications_enabled",
        "recommended",
        "high",
        "possible",
        "mention_leads_per_run",
    }
    # Every control carries what the page needs to render it without guessing.
    for control in body["controls"]:
        assert control["label"] and control["help"] and control["kind"]
        assert control["key"] in body["values"]


async def test_turning_alerts_on_reaches_the_running_pipeline(client, config):
    assert config.app.notifications_enabled is False
    response = await client.patch(
        "/api/settings", json={"values": {"notifications_enabled": True}}
    )
    assert response.status_code == 200
    # The object the pipeline holds, not a reloaded copy.
    assert config.app.notifications_enabled is True
    assert response.json()["settings"]["notifications_enabled"] is True


async def test_changing_the_alert_score_is_written_to_disk(client, config):
    response = await client.patch("/api/settings", json={"values": {"recommended": 72}})
    assert response.status_code == 200
    assert config.scoring["thresholds"]["recommended"] == 72
    on_disk = yaml.safe_load((config.root / "config" / "scoring.yaml").read_text(encoding="utf-8"))
    assert on_disk["thresholds"]["recommended"] == 72
    assert "config" in response.json()["written"][0]


async def test_rewriting_a_config_file_keeps_its_comments(client, config):
    path = config.root / "config" / "scoring.yaml"
    before = path.read_text(encoding="utf-8")
    await client.patch("/api/settings", json={"values": {"recommended": 71}})
    after = path.read_text(encoding="utf-8")
    # The comments in these files are the reasoning behind the numbers; a YAML round-trip
    # would drop every one of them.
    assert before.count("#") == after.count("#")
    assert "recommended: 71" in after


async def test_thresholds_must_stay_in_order(client, config):
    """A high-priority score under the alert score silently makes every alert urgent."""
    response = await client.patch("/api/settings", json={"values": {"high": 40}})
    assert response.status_code == 422
    assert "rise in order" in response.json()["detail"]
    # Nothing was applied.
    assert config.scoring["thresholds"]["high"] == 80


async def test_a_score_outside_the_range_is_refused(client):
    response = await client.patch("/api/settings", json={"values": {"recommended": 400}})
    assert response.status_code == 422
    assert "between 0 and 100" in response.json()["detail"]


async def test_an_unknown_setting_is_refused(client):
    response = await client.patch("/api/settings", json={"values": {"ollama_base_url": "x"}})
    assert response.status_code == 422
    assert "Not a setting" in response.json()["detail"]


async def test_the_legacy_threshold_is_kept_in_step(config):
    """Two copies of the alert threshold exist; they must never disagree."""
    update_settings({"recommended": 70}, config)
    assert config.app.notification_threshold == 70
    assert config.scoring["thresholds"]["recommended"] == 70


async def test_setting_a_value_it_already_has_changes_nothing(config):
    before = current_settings(config)
    outcome = update_settings({"recommended": before["recommended"]}, config)
    assert outcome["changed"] == []
    assert outcome["written"] == []


async def test_settings_require_the_token_when_one_is_configured(config):
    config.runtime.dashboard_token = "let-me-in"
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    app = create_dashboard(database, FakeController(), config, bot=BotController())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://dashboard"
        ) as http:
            assert (await http.get("/api/settings")).status_code == 401
            assert (
                await http.patch("/api/settings", json={"values": {"recommended": 60}})
            ).status_code == 401
    finally:
        config.runtime.dashboard_token = None
        await database.close()
