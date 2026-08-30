from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from akaton.fetch.proxy import ProxyManager, load_proxies, parse_proxy


@pytest.mark.parametrize(
    "raw,scheme,username",
    [
        ("1.2.3.4:8080", "http", None),
        ("user:pass@1.2.3.4:8080", "http", "user"),
        ("https://user:pass@proxy.example:443", "https", "user"),
        ("socks5://user:pass@proxy.example:1080", "socks5", "user"),
    ],
)
def test_proxy_formats(raw, scheme, username):
    proxy = parse_proxy(raw)
    assert proxy.scheme == scheme
    assert proxy.username == username
    assert "pass" not in proxy.redacted()


def test_malformed_proxy_lines_do_not_abort(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "# comment\ninvalid\nhttp://ok.example:8080\n[bad](http://bad:1)\n", encoding="utf-8"
    )
    proxies, errors = load_proxies(path)
    assert len(proxies) == 1
    assert len(errors) == 2


def test_dead_proxy_cools_down_and_healthy_proxy_is_selected():
    dead = parse_proxy("dead.example:8080")
    healthy = parse_proxy("healthy.example:8080")
    manager = ProxyManager([dead, healthy], rng=random.Random(0))
    now = datetime(2026, 8, 30, tzinfo=UTC)
    manager.report_failure(dead.proxy_id, proxy_attributable=True, now=now)
    assert manager.states[dead.proxy_id].cooldown_until > now
    assert manager.select(now=now).proxy_id == healthy.proxy_id
    manager.report_success(dead.proxy_id, 100, now=now + timedelta(hours=1))
    assert manager.states[dead.proxy_id].consecutive_failures == 0


def test_no_proxies_returns_none():
    assert ProxyManager([], mode="auto").select() is None


def test_documented_proxy_mode_aliases():
    assert ProxyManager([], mode="proxy").mode == "proxy_only"
    assert ProxyManager([], mode="disabled").mode == "direct"
