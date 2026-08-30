from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from akaton.domain.models import ParticipantProfile


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    discord_bot_token: str | None = None
    discord_channel_id: int | None = None
    discord_user_id: int | None = None
    search_provider: str = "brave"
    brave_search_api_key: str | None = None
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    openai_model: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    proxy_mode: str = "auto"
    database_url: str = "sqlite+aiosqlite:///data/akaton.db"
    notifications_enabled: bool = False
    log_level: str = "INFO"


class AppSettings(BaseModel):
    timezone: str = "Asia/Manila"
    discovery_interval_hours: int = Field(default=6, ge=1)
    discovery_queries_per_run: int = Field(default=8, ge=1)
    monthly_search_budget: int = Field(default=950, ge=1)
    refresh_interval_hours: int = Field(default=24, ge=1)
    notification_threshold: int = Field(default=65, ge=0, le=100)
    high_priority_threshold: int = Field(default=80, ge=0, le=100)
    possible_threshold: int = Field(default=50, ge=0, le=100)
    snapshot_retention_days: int = Field(default=90, ge=1)
    max_download_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    notifications_enabled: bool = False


@dataclass(frozen=True)
class ConfigBundle:
    runtime: RuntimeSettings
    app: AppSettings
    profile: ParticipantProfile
    queries: dict[str, Any]
    scoring: dict[str, Any]
    domains: dict[str, Any]
    sources: dict[str, Any]
    root: Path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required configuration file is missing: {path}")
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must be a mapping: {path}")
    return value


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current


def load_config(root: Path | None = None, *, allow_example_profile: bool = False) -> ConfigBundle:
    project_root = (root or find_project_root()).resolve()
    config_dir = project_root / "config"
    profile_path = config_dir / "profile.yaml"
    if allow_example_profile and not profile_path.exists():
        profile_path = config_dir / "profile.example.yaml"
    runtime = RuntimeSettings(_env_file=project_root / ".env")
    app = AppSettings.model_validate(_read_yaml(config_dir / "settings.yaml"))
    if os.getenv("NOTIFICATIONS_ENABLED") is not None:
        app.notifications_enabled = runtime.notifications_enabled
    try:
        profile = ParticipantProfile.model_validate(_read_yaml(profile_path))
    except ValidationError as exc:
        raise ValueError(f"Invalid participant profile: {exc}") from exc
    return ConfigBundle(
        runtime=runtime,
        app=app,
        profile=profile,
        queries=_read_yaml(config_dir / "queries.yaml"),
        scoring=_read_yaml(config_dir / "scoring.yaml"),
        domains=_read_yaml(config_dir / "domains.yaml"),
        sources=_read_yaml(config_dir / "sources.yaml"),
        root=project_root,
    )
