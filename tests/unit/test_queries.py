from __future__ import annotations

from datetime import UTC, datetime, timedelta

from akaton.discovery.queries import ScheduledQuery, choose_due_queries


def test_query_rotation_only_selects_due_queries():
    now = datetime(2026, 8, 30, tzinfo=UTC)
    hot = ScheduledQuery("hot", "hot", "pw", 6, 5)
    cold = ScheduledQuery("cold", "cold", "pm", 24, 1)
    history = {("cold", "cold"): now - timedelta(hours=1)}
    assert choose_due_queries([cold, hot], history, 8, now=now) == [hot]
