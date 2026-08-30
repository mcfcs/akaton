from __future__ import annotations

import json
from pathlib import Path

import pytest

from akaton.config import load_config


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def config(project_root):
    return load_config(project_root, allow_example_profile=True)


@pytest.fixture(scope="session")
def event_cases(project_root):
    return json.loads(
        (project_root / "tests" / "fixtures" / "events.json").read_text(encoding="utf-8")
    )
