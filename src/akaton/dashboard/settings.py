"""The notification settings a person may change from the dashboard, and how they persist.

Two things have to happen for a change here to mean anything. The running process holds
`ConfigBundle` objects that the pipeline reads on every candidate, so a change has to be
written into those in place — the pipeline is handed the same object, not a copy. And it
has to reach disk, or the next restart silently undoes it.

Only settings that genuinely govern *notifications* are exposed. Discovery cadence, model
hosts and search budgets are deliberately absent: they belong to how the scraper works
rather than to what it tells you about, and the dashboard already has controls for the
ones worth touching by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from akaton.config import ConfigBundle


@dataclass(frozen=True)
class Setting:
    """One editable knob, described well enough for the page to render it unaided."""

    key: str
    label: str
    help: str
    kind: Literal["toggle", "score", "count"]
    # Where the value lives. "app" is config/settings.yaml; "scoring" is the thresholds
    # block of config/scoring.yaml, which is what the pipeline actually gates on.
    store: Literal["app", "scoring"]
    minimum: int = 0
    maximum: int = 100
    unit: str = ""


SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="notifications_enabled",
        label="Send alerts to Discord",
        help=(
            "Off is shadow mode: everything is still discovered, scored and stored, and "
            "you can read it here, but nothing is posted to the channel."
        ),
        kind="toggle",
        store="app",
    ),
    Setting(
        key="recommended",
        label="Alert above this score",
        help=(
            "A competition scoring below this is kept and shown here, but never posted "
            "automatically. Raise it if the channel is noisy; lower it to see more."
        ),
        kind="score",
        store="scoring",
        minimum=0,
        maximum=100,
    ),
    Setting(
        key="high",
        label="Treat as high priority above",
        help="High-priority alerts are posted in red so they stand out from the rest.",
        kind="score",
        store="scoring",
        minimum=0,
        maximum=100,
    ),
    Setting(
        key="possible",
        label="Worth keeping above",
        help=(
            "Below this a competition is scored as a poor match. It is still recorded, "
            "so you can find it under Rejected, but it is not treated as a real lead."
        ),
        kind="score",
        store="scoring",
        minimum=0,
        maximum=100,
    ),
    Setting(
        key="mention_leads_per_run",
        label="Mentions chased per run",
        help=(
            "Someone naming a competition without linking to it costs one search to "
            "track down. This caps how many a single run will spend."
        ),
        kind="count",
        store="app",
        minimum=0,
        maximum=25,
        unit="per run",
    ),
    Setting(
        key="notification_threshold",
        label="Legacy alert threshold",
        help=(
            "Kept in step with the alert score above so the two cannot disagree. "
            "Nothing reads this independently."
        ),
        kind="score",
        store="app",
        minimum=0,
        maximum=100,
    ),
)

BY_KEY = {setting.key: setting for setting in SETTINGS}
# Shown as one group; the legacy mirror is not something to hand-edit.
VISIBLE = tuple(setting for setting in SETTINGS if setting.key != "notification_threshold")


def current_settings(config: ConfigBundle) -> dict[str, Any]:
    """What the page should show as the settings in force right now."""
    thresholds = config.scoring.get("thresholds", {})
    return {
        "notifications_enabled": bool(config.app.notifications_enabled),
        "recommended": int(thresholds.get("recommended", 65)),
        "high": int(thresholds.get("high", 80)),
        "possible": int(thresholds.get("possible", 50)),
        "mention_leads_per_run": int(config.app.mention_leads_per_run),
    }


def describe_settings() -> list[dict[str, Any]]:
    """The settings, as the page needs them to render controls and explanations."""
    return [
        {
            "key": setting.key,
            "label": setting.label,
            "help": setting.help,
            "kind": setting.kind,
            "min": setting.minimum,
            "max": setting.maximum,
            "unit": setting.unit,
        }
        for setting in VISIBLE
    ]


class SettingsError(ValueError):
    """A rejected edit, with a message written for the person who made it."""


def _coerce(setting: Setting, value: Any) -> Any:
    if setting.kind == "toggle":
        if not isinstance(value, bool):
            raise SettingsError(f"{setting.label} is on or off")
        return value
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{setting.label} must be a whole number") from exc
    if not setting.minimum <= number <= setting.maximum:
        raise SettingsError(
            f"{setting.label} must be between {setting.minimum} and {setting.maximum}"
        )
    return number


def validate(edits: dict[str, Any], config: ConfigBundle) -> dict[str, Any]:
    """Coerce and range-check an edit, then check the thresholds still make sense together.

    The ordering rule is the point of doing this here rather than per field: a "high
    priority" score under the score that alerts at all is not a typo the pipeline would
    catch, it just quietly makes every alert high priority.
    """
    if not edits:
        raise SettingsError("Nothing to change")
    unknown = sorted(set(edits) - set(BY_KEY))
    if unknown:
        raise SettingsError(f"Not a setting: {', '.join(unknown)}")
    clean = {key: _coerce(BY_KEY[key], value) for key, value in edits.items()}
    merged = {**current_settings(config), **clean}
    if not merged["possible"] <= merged["recommended"] <= merged["high"]:
        raise SettingsError(
            "Scores must rise in order: worth keeping ≤ alert above ≤ high priority "
            f"(you asked for {merged['possible']}, {merged['recommended']}, {merged['high']})"
        )
    return clean


def apply(clean: dict[str, Any], config: ConfigBundle) -> list[str]:
    """Write the edit into the live configuration the pipeline is already holding.

    `config.scoring` and `config.app` are mutated in place rather than replaced, because
    the pipeline, the scorer and the dashboard were each handed this same object at
    startup and would otherwise keep reading the old one.
    """
    changed: list[str] = []
    before = current_settings(config)
    thresholds = config.scoring.setdefault("thresholds", {})
    for key, value in clean.items():
        if before.get(key) == value:
            continue
        if BY_KEY[key].store == "scoring":
            thresholds[key] = value
            # The pipeline gates on scoring.thresholds.recommended; AppSettings carries a
            # second copy that predates it. Keeping them in step means the two can never
            # disagree about when to alert.
            if key == "recommended":
                config.app.notification_threshold = value
            elif key == "high":
                config.app.high_priority_threshold = value
            elif key == "possible":
                config.app.possible_threshold = value
        else:
            setattr(config.app, key, value)
        changed.append(key)
    return changed


def _replace_scalar(text: str, key: str, value: Any, *, indent: str = "") -> str | None:
    """Rewrite `key: value` in place, leaving the comments around it alone.

    A full YAML round-trip would drop every comment in these files, and those comments
    are the reasoning behind the numbers. Returns None when the key is not present, which
    tells the caller to append it instead.
    """
    rendered = "true" if value is True else "false" if value is False else str(value)
    pattern = re.compile(
        rf"^(?P<head>{re.escape(indent)}{re.escape(key)}:[ \t]*)(?P<value>[^\n#]*)(?P<tail>.*)$",
        re.MULTILINE,
    )
    if not pattern.search(text):
        return None
    return pattern.sub(lambda m: f"{m.group('head')}{rendered}{m.group('tail')}", text, count=1)


def persist(clean: dict[str, Any], config: ConfigBundle) -> list[str]:
    """Write the edit back to config/*.yaml so it survives a restart.

    Values are substituted line by line rather than re-serialised, so the explanatory
    comments in both files — which are most of what makes them readable — survive.
    """
    written: list[str] = []
    app_keys = {key: value for key, value in clean.items() if BY_KEY[key].store == "app"}
    scoring_keys = {key: value for key, value in clean.items() if BY_KEY[key].store == "scoring"}
    if app_keys:
        path = config.root / "config" / "settings.yaml"
        text = path.read_text(encoding="utf-8")
        for key, value in app_keys.items():
            updated = _replace_scalar(text, key, value)
            text = updated if updated is not None else f"{text.rstrip()}\n{key}: {value}\n"
        path.write_text(text, encoding="utf-8")
        written.append(str(path.relative_to(config.root)))
    if scoring_keys:
        path = config.root / "config" / "scoring.yaml"
        text = path.read_text(encoding="utf-8")
        for key, value in scoring_keys.items():
            # Nested under `thresholds:`, so the two-space indent is what distinguishes
            # `high:` there from any other `high:` the file might grow later.
            updated = _replace_scalar(text, key, value, indent="  ")
            if updated is None:
                raise SettingsError(f"config/scoring.yaml has no thresholds.{key} to update")
            text = updated
        path.write_text(text, encoding="utf-8")
        written.append(str(path.relative_to(config.root)))
    # settings.yaml carries its own copy of the alert threshold; keep the file honest.
    if "recommended" in clean:
        path = config.root / "config" / "settings.yaml"
        text = path.read_text(encoding="utf-8")
        updated = _replace_scalar(text, "notification_threshold", clean["recommended"])
        if updated is not None:
            path.write_text(updated, encoding="utf-8")
    return written


def update_settings(edits: dict[str, Any], config: ConfigBundle) -> dict[str, Any]:
    """Validate, apply to the running process, and write to disk."""
    clean = validate(edits, config)
    changed = apply(clean, config)
    written = persist({key: clean[key] for key in changed}, config) if changed else []
    return {"changed": changed, "written": written, "settings": current_settings(config)}


def _describe_change(keys: list[str]) -> str:
    if not keys:
        return "Nothing to change"
    labels = [BY_KEY[key].label.lower() for key in keys]
    return f"Saved {', '.join(labels)}"
