from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DomainPolicy:
    requests_per_minute: int = 6
    concurrency: int = 1
    timeout_seconds: float = 15.0
    retries: int = 2
    browser: str = "js_evidence"
    proxy: str = "direct"
    fetch: str = "enabled"


class DomainPolicyResolver:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.default = DomainPolicy(
            **{k: v for k, v in config.get("default", {}).items() if k != "match"}
        )

    def for_url(self, url: str) -> DomainPolicy:
        host = (urlsplit(url).hostname or "").casefold()
        best: tuple[int, dict] | None = None
        for domain, overrides in self.config.get("domains", {}).items():
            domain = domain.casefold()
            match = overrides.get("match", "exact")
            matched = host == domain or (
                match == "suffix" and (host == domain or host.endswith(f".{domain}"))
            )
            if matched and (best is None or len(domain) > best[0]):
                best = (len(domain), overrides)
        if not best:
            return self.default
        values = {key: value for key, value in best[1].items() if key != "match"}
        return replace(self.default, **values)
