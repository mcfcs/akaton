from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from akaton.domain.models import ParticipantProfile


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    discord_bot_token: str | None = None
    discord_channel_id: int | None = None
    discord_user_id: int | None = None
    discord_guild_id: int | None = None
    search_provider: Literal["brave", "searxng"] = "searxng"
    brave_search_api_key: str | None = None
    searxng_base_url: str = "http://127.0.0.1:8888"
    llm_provider: Literal["ollama", "openai", "disabled"] = "ollama"
    openai_api_key: str | None = None
    openai_model: str | None = None
    # No default host. This is a private Tailscale address in every real deployment, and
    # committing one publishes the network layout of whoever set it up; it belongs in
    # .env. `validate_config` reports a missing value when LLM_PROVIDER=ollama.
    ollama_base_url: str = ""
    # Chosen on measurement, not preference — see tools/llm_bench.py and the README table.
    # It is the only ~9B candidate that matches deterministic extraction on every column
    # instead of regressing one, and it is trained for Southeast Asian languages, which
    # the English-only fixtures cannot show but the Taglish group posts need.
    ollama_model: str = "aisingapore/Gemma-SEA-LION-v3-9B-IT:q4_k_m"
    # Optional second host, tried when the first answers thinly or cannot be reached.
    # Empty means a single-tier ladder, which is what a one-host deployment wants.
    ollama_escalation_url: str = ""
    ollama_escalation_model: str = "qwen2.5vl:7b"
    proxy_mode: str = "auto"
    database_url: str = "sqlite+aiosqlite:///data/akaton.db"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8765, ge=1, le=65535)
    dashboard_token: str | None = None
    dashboard_auto_start: bool = True
    notifications_enabled: bool = False
    log_level: str = "INFO"
    facebook_email: str | None = None
    facebook_password: str | None = Field(default=None, repr=False)


class AppSettings(BaseModel):
    timezone: str = "Asia/Manila"
    discovery_interval_hours: int = Field(default=6, ge=1)
    discovery_queries_per_run: int = Field(default=12, ge=1)
    discovery_concurrency: int = Field(default=6, ge=1, le=32)
    llm_concurrency: int = Field(default=1, ge=1, le=8)
    search_interval_seconds: float = Field(default=6.0, ge=0, le=300)
    monthly_search_budget: int = Field(default=2000, ge=1)
    # Searches a run may spend resolving social mentions. Taken out of the run's own
    # allocation, never added to it, and capped at a third of it so the scheduled
    # rotation is never crowded out by a busy week in the group.
    mention_leads_per_run: int = Field(default=3, ge=0)
    # Below this merged confidence the everyday model is judged not to have answered, and
    # the second host is asked. One notch under the 0.75 that summons a model at all.
    llm_escalation_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    # The fallback host is shared and reloads models between callers — measured at 5.8,
    # 16.1 and 39.9 seconds. Without a cap, a run of thin pages would serialise dozens.
    llm_escalations_per_run: int = Field(default=5, ge=0)
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
