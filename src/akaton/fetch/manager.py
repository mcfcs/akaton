from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from urllib.parse import urlsplit

from akaton.domain.enums import FailureCode
from akaton.domain.models import FetchResult
from akaton.fetch.browser import BrowserRenderer
from akaton.fetch.http import HttpFetcher
from akaton.fetch.policies import DomainPolicyResolver
from akaton.fetch.proxy import ProxyManager


class FetchManager:
    def __init__(
        self,
        http: HttpFetcher,
        policies: DomainPolicyResolver,
        *,
        browser: BrowserRenderer | None = None,
        proxies: ProxyManager | None = None,
    ) -> None:
        self.http = http
        self.policies = policies
        self.browser = browser
        self.proxies = proxies
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_request: defaultdict[str, float] = defaultdict(float)
        self._rate_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._systemic_failures: defaultdict[str, set[str]] = defaultdict(set)
        self._cooldown_until: defaultdict[str, float] = defaultdict(float)

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        policy = self.policies.for_url(url)
        host = (urlsplit(url).hostname or "").casefold()
        if policy.fetch == "disabled":
            # Refuse before the rate-limit wait: a blocked domain must never consume
            # a request slot or delay the queue behind it.
            return FetchResult(
                requested_url=url, fetch_method="policy", failure=FailureCode.FETCH_DISABLED
            )
        if self._cooldown_until[host] > time.monotonic():
            return FetchResult(
                requested_url=url,
                fetch_method="circuit_breaker",
                failure=FailureCode.RATE_LIMITED,
            )
        semaphore = self._semaphores.setdefault(host, asyncio.Semaphore(policy.concurrency))
        async with semaphore:
            async with self._rate_locks[host]:
                minimum_delay = 60.0 / max(1, policy.requests_per_minute)
                remaining = minimum_delay - (time.monotonic() - self._last_request[host])
                if remaining > 0:
                    await asyncio.sleep(remaining)
                self._last_request[host] = time.monotonic()
            result = await self.http.fetch(url, policy, etag=etag, last_modified=last_modified)
        if not self._browser_allowed(result, policy.browser):
            self._record_result(host, url, result)
            return result
        proxy = None
        if self.proxies and self.proxies.mode == "proxy_only":
            proxy = self.proxies.select()
            if proxy is None:
                self._record_result(host, url, result)
                return result
        browser_result = await self.browser.render(url, policy, proxy=proxy)  # type: ignore[union-attr]
        browser_result.attempts = [*result.attempts, *browser_result.attempts]
        self._record_result(host, url, browser_result)
        return browser_result

    def _record_result(self, host: str, url: str, result: FetchResult) -> None:
        if result.failure is FailureCode.HTTP_429:
            delay = _retry_after_seconds(result.headers.get("retry-after"))
            self._cooldown_until[host] = max(self._cooldown_until[host], time.monotonic() + delay)
            return
        systemic = {
            FailureCode.DNS_ERROR,
            FailureCode.TIMEOUT,
            FailureCode.TLS_ERROR,
            FailureCode.CONNECTION_ERROR,
            FailureCode.SERVER_ERROR,
        }
        if result.failure in systemic:
            self._systemic_failures[host].add(url)
            if len(self._systemic_failures[host]) >= 5:
                self._cooldown_until[host] = time.monotonic() + 60 * 60
                self._systemic_failures[host].clear()
        elif result.failure is None:
            self._systemic_failures[host].clear()

    def _browser_allowed(self, result: FetchResult, browser_policy: str) -> bool:
        if not self.browser or browser_policy == "disabled":
            return False
        if result.status_code in {401, 403, 404, 429}:
            return False
        if result.failure in {
            FailureCode.HTTP_401,
            FailureCode.HTTP_403,
            FailureCode.HTTP_404,
            FailureCode.HTTP_429,
        }:
            return False
        if browser_policy == "preferred":
            return True
        return browser_policy == "js_evidence" and result.failure is FailureCode.JS_REQUIRED


def _retry_after_seconds(value: str | None) -> int:
    if not value:
        return 60 * 60
    try:
        return max(1, min(int(value), 24 * 60 * 60))
    except ValueError:
        return 60 * 60
