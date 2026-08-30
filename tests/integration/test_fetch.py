from __future__ import annotations

import httpx

from akaton.domain.enums import FailureCode
from akaton.domain.models import FetchResult
from akaton.fetch.http import HttpFetcher
from akaton.fetch.manager import FetchManager
from akaton.fetch.policies import DomainPolicyResolver

HTML = b"""<html><head><title>Manila Hackathon</title></head><body>
<main><h1>Manila Hackathon</h1><p>Registration is now open for university students
in the Philippines. Registration deadline October 5, 2026.
Event date October 20, 2026 in Manila.</p>
<a href="https://forms.gle/abc">Register</a></main></body></html>"""


async def test_http_fetch_extracts_html():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)
    )
    fetcher = HttpFetcher(transport=transport, resolve_dns=False)
    policy = DomainPolicyResolver({"default": {}}).for_url("https://example.com")
    result = await fetcher.fetch("https://example.com/event", policy)
    assert result.usable
    assert result.title == "Manila Hackathon"
    assert "https://forms.gle/abc" in result.links


async def test_403_never_uses_browser():
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    http = HttpFetcher(transport=transport, resolve_dns=False)

    class Browser:
        called = False

        async def render(self, url, policy, proxy=None):
            self.called = True
            return FetchResult(requested_url=url, fetch_method="browser", usable=True)

    browser = Browser()
    manager = FetchManager(http, DomainPolicyResolver({"default": {}}), browser=browser)
    result = await manager.fetch("https://example.com/event")
    assert result.failure is FailureCode.HTTP_403
    assert browser.called is False


async def test_js_shell_uses_browser_fallback():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<div id='app'></div>"
        )
    )
    http = HttpFetcher(transport=transport, resolve_dns=False)

    class Browser:
        called = False

        async def render(self, url, policy, proxy=None):
            self.called = True
            return FetchResult(
                requested_url=url, fetch_method="browser", text="rendered " * 100, usable=True
            )

    browser = Browser()
    manager = FetchManager(http, DomainPolicyResolver({"default": {}}), browser=browser)
    result = await manager.fetch("https://example.com/event")
    assert browser.called is True
    assert result.fetch_method == "browser"
