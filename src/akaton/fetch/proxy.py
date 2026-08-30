from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class ProxyConfig:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)

    @property
    def proxy_id(self) -> str:
        identity = f"{self.scheme}://{self.username or ''}@{self.host}:{self.port}"
        return hashlib.sha256(identity.encode()).hexdigest()[:16]

    @property
    def server(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{self.scheme}://{host}:{self.port}"

    def as_httpx_url(self) -> str:
        if not self.username:
            return self.server
        from urllib.parse import quote

        user = quote(self.username, safe="")
        password = quote(self.password or "", safe="")
        return self.server.replace(f"{self.scheme}://", f"{self.scheme}://{user}:{password}@", 1)

    def as_browser_proxy(self) -> dict[str, str]:
        value = {"server": self.server}
        if self.username is not None:
            value["username"] = self.username
            value["password"] = self.password or ""
        return value

    def redacted(self) -> str:
        auth = "***:***@" if self.username is not None else ""
        return self.server.replace(f"{self.scheme}://", f"{self.scheme}://{auth}", 1)


@dataclass
class ProxyState:
    config: ProxyConfig
    successful_requests: int = 0
    failed_requests: int = 0
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    cooldown_until: datetime | None = None
    average_latency_ms: float | None = None
    disabled_reason: str | None = None

    def healthy(self, now: datetime) -> bool:
        return not self.disabled_reason and (not self.cooldown_until or self.cooldown_until <= now)


def parse_proxy(line: str) -> ProxyConfig:
    value = line.strip()
    if not value or value.startswith("#"):
        raise ValueError("blank or comment")
    if "](" in value or value.startswith("["):
        raise ValueError("Markdown links are not supported; use the raw proxy URI")
    if "://" not in value:
        value = f"http://{value}"
    parts = urlsplit(value)
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https", "socks5"}:
        raise ValueError(f"unsupported proxy scheme: {scheme}")
    if not parts.hostname or parts.port is None:
        raise ValueError("proxy host and port are required")
    if not 1 <= parts.port <= 65535:
        raise ValueError("proxy port is out of range")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("proxy URI cannot contain a path, query, or fragment")
    return ProxyConfig(
        scheme=scheme,
        host=parts.hostname,
        port=parts.port,
        username=unquote(parts.username) if parts.username is not None else None,
        password=unquote(parts.password) if parts.password is not None else None,
    )


def load_proxies(path: Path) -> tuple[list[ProxyConfig], list[str]]:
    if not path.exists():
        return [], []
    proxies: dict[str, ProxyConfig] = {}
    errors: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            proxy = parse_proxy(raw)
        except ValueError as exc:
            errors.append(f"line {number}: {exc}")
            continue
        proxies[proxy.proxy_id] = proxy
    return list(proxies.values()), errors


class ProxyManager:
    def __init__(
        self, proxies: list[ProxyConfig], mode: str = "auto", *, rng: random.Random | None = None
    ) -> None:
        mode = {"proxy": "proxy_only", "disabled": "direct"}.get(mode, mode)
        if mode not in {"direct", "auto", "proxy_only"}:
            raise ValueError("PROXY_MODE must be direct, auto, proxy/proxy_only, or disabled")
        self.mode = mode
        self.states = {proxy.proxy_id: ProxyState(proxy) for proxy in proxies}
        self.rng = rng or random.Random()
        self._cursor = 0

    def select(
        self, *, exclude: set[str] | None = None, now: datetime | None = None
    ) -> ProxyConfig | None:
        now = now or datetime.now(UTC)
        exclude = exclude or set()
        healthy = [
            state
            for state in self.states.values()
            if state.config.proxy_id not in exclude and state.healthy(now)
        ]
        if not healthy:
            return None
        healthy.sort(
            key=lambda item: (
                item.consecutive_failures,
                item.average_latency_ms or float("inf"),
                item.config.proxy_id,
            )
        )
        best_failure_count = healthy[0].consecutive_failures
        pool = [item for item in healthy if item.consecutive_failures == best_failure_count]
        selected = pool[self._cursor % len(pool)]
        self._cursor += 1
        return selected.config

    def report_success(
        self, proxy_id: str, latency_ms: float, *, now: datetime | None = None
    ) -> None:
        state = self.states[proxy_id]
        state.successful_requests += 1
        state.consecutive_failures = 0
        state.last_success = now or datetime.now(UTC)
        state.cooldown_until = None
        state.average_latency_ms = (
            latency_ms
            if state.average_latency_ms is None
            else 0.2 * latency_ms + 0.8 * state.average_latency_ms
        )

    def report_failure(
        self,
        proxy_id: str,
        *,
        proxy_attributable: bool,
        auth_failure: bool = False,
        now: datetime | None = None,
    ) -> None:
        if not proxy_attributable:
            return
        current = now or datetime.now(UTC)
        state = self.states[proxy_id]
        state.failed_requests += 1
        state.consecutive_failures += 1
        state.last_failure = current
        if auth_failure:
            state.disabled_reason = "proxy authentication failed"
            return
        seconds = min(3600, 60 * (2 ** (state.consecutive_failures - 1)))
        seconds *= self.rng.uniform(0.8, 1.2)
        state.cooldown_until = current + timedelta(seconds=seconds)
