from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol

from akaton.domain.enums import FailureCode
from akaton.domain.models import FetchAttempt, FetchResult
from akaton.fetch.documents import extract_html, hash_content
from akaton.fetch.policies import DomainPolicy
from akaton.fetch.proxy import ProxyConfig


class BrowserRenderer(Protocol):
    async def render(
        self, url: str, policy: DomainPolicy, *, proxy: ProxyConfig | None = None
    ) -> FetchResult: ...


class PatchrightRenderer:
    async def render(
        self, url: str, policy: DomainPolicy, *, proxy: ProxyConfig | None = None
    ) -> FetchResult:
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            return FetchResult(
                requested_url=url, fetch_method="browser", failure=FailureCode.BROWSER_FAILED
            )
        started = datetime.now(UTC)
        timer = time.perf_counter()
        attempt = FetchAttempt(
            method="browser", started_at=started, proxy_id=proxy.proxy_id if proxy else None
        )
        browser = context = page = None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                options = {"proxy": proxy.as_browser_proxy()} if proxy else {}
                context = await browser.new_context(**options)
                page = await context.new_page()
                page.set_default_navigation_timeout(min(15_000, int(policy.timeout_seconds * 1000)))
                page.set_default_timeout(10_000)
                response = await page.goto(url, wait_until="domcontentloaded")
                try:
                    await page.locator("body").wait_for(state="visible", timeout=5_000)
                except Exception:
                    pass
                html = await page.content()
                body = html.encode("utf-8")
                title, text, links, metadata = extract_html(body, page.url)
                attempt.status_code = response.status if response else None
                attempt.elapsed_ms = round((time.perf_counter() - timer) * 1000)
                usable = bool(text and len(text.strip()) >= 400)
                attempt.failure = None if usable else FailureCode.CONTENT_EMPTY
                return FetchResult(
                    requested_url=url,
                    final_url=page.url,
                    fetch_method="browser",
                    status_code=attempt.status_code,
                    content_type="text/html",
                    title=title,
                    text=text,
                    links=links,
                    metadata=metadata,
                    content_hash=hash_content(body),
                    proxy_used=bool(proxy),
                    usable=usable,
                    failure=attempt.failure,
                    attempts=[attempt],
                )
        except Exception as exc:
            attempt.elapsed_ms = round((time.perf_counter() - timer) * 1000)
            attempt.failure = FailureCode.BROWSER_FAILED
            attempt.detail = type(exc).__name__
            return FetchResult(
                requested_url=url,
                fetch_method="browser",
                proxy_used=bool(proxy),
                failure=FailureCode.BROWSER_FAILED,
                attempts=[attempt],
            )
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
