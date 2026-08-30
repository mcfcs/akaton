from __future__ import annotations

import asyncio
import ssl
import time
from datetime import UTC, datetime

import httpx

from akaton.domain.enums import FailureCode
from akaton.domain.models import FetchAttempt, FetchResult
from akaton.fetch.documents import extract_html, extract_pdf, hash_content
from akaton.fetch.policies import DomainPolicy
from akaton.fetch.proxy import ProxyConfig, ProxyManager
from akaton.fetch.safety import validate_public_url


class HttpFetcher:
    def __init__(
        self,
        *,
        max_download_bytes: int = 5 * 1024 * 1024,
        proxy_manager: ProxyManager | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve_dns: bool = True,
    ) -> None:
        self.max_download_bytes = max_download_bytes
        self.proxy_manager = proxy_manager
        self.transport = transport
        self.resolve_dns = resolve_dns

    async def fetch(
        self,
        url: str,
        policy: DomainPolicy,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        try:
            await validate_public_url(url, resolve_dns=self.resolve_dns)
        except (ValueError, OSError):
            return FetchResult(
                requested_url=url, fetch_method="http", failure=FailureCode.UNSAFE_URL
            )
        if policy.fetch == "disabled":
            return FetchResult(
                requested_url=url, fetch_method="policy", failure=FailureCode.FETCH_DISABLED
            )

        headers = {"User-Agent": "Akaton/0.1 (+personal competition monitor)"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        attempts: list[FetchAttempt] = []
        use_proxy = self.proxy_manager and self.proxy_manager.mode == "proxy_only"
        proxy = self.proxy_manager.select() if use_proxy and self.proxy_manager else None
        final_result: FetchResult | None = None
        max_attempts = policy.retries + 1
        for index in range(max_attempts):
            result = await self._attempt(url, policy, headers, proxy)
            attempts.extend(result.attempts)
            result.attempts = list(attempts)
            final_result = result
            if result.failure is None or result.status_code in {304, 401, 403, 404, 429}:
                break
            if result.failure not in {
                FailureCode.DNS_ERROR,
                FailureCode.TIMEOUT,
                FailureCode.TLS_ERROR,
                FailureCode.CONNECTION_ERROR,
                FailureCode.SERVER_ERROR,
            }:
                break
            if index < max_attempts - 1:
                await asyncio.sleep((1 if index == 0 else 4) + index * 0.1)

        if (
            final_result
            and final_result.failure
            in {
                FailureCode.DNS_ERROR,
                FailureCode.TIMEOUT,
                FailureCode.TLS_ERROR,
                FailureCode.CONNECTION_ERROR,
            }
            and self.proxy_manager
            and self.proxy_manager.mode == "auto"
            and policy.proxy in {"auto", "proxy_preferred"}
        ):
            proxy = self.proxy_manager.select()
            if proxy:
                proxied = await self._attempt(url, policy, headers, proxy)
                proxied.attempts = [*attempts, *proxied.attempts]
                final_result = proxied
        return final_result or FetchResult(
            requested_url=url, fetch_method="http", failure=FailureCode.CONNECTION_ERROR
        )

    async def _attempt(
        self, url: str, policy: DomainPolicy, headers: dict[str, str], proxy: ProxyConfig | None
    ) -> FetchResult:
        started = datetime.now(UTC)
        timer = time.perf_counter()
        attempt = FetchAttempt(
            method="http", started_at=started, proxy_id=proxy.proxy_id if proxy else None
        )
        client_kwargs = {
            "timeout": httpx.Timeout(policy.timeout_seconds),
            "follow_redirects": True,
            "max_redirects": 5,
            "headers": headers,
            "transport": self.transport,
        }
        if self.transport is None:

            async def validate_request(request: httpx.Request) -> None:
                await validate_public_url(str(request.url), resolve_dns=self.resolve_dns)

            client_kwargs["event_hooks"] = {"request": [validate_request]}
        if proxy and self.transport is None:
            client_kwargs["proxy"] = proxy.as_httpx_url()
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream("GET", url) as response:
                    attempt.status_code = response.status_code
                    if response.status_code == 304:
                        attempt.elapsed_ms = round((time.perf_counter() - timer) * 1000)
                        return FetchResult(
                            requested_url=url,
                            final_url=str(response.url),
                            fetch_method="http",
                            status_code=304,
                            unchanged=True,
                            usable=True,
                            proxy_used=bool(proxy),
                            attempts=[attempt],
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_download_bytes:
                            raise ContentTooLarge
                    content = bytes(body)
                    failure = _status_failure(response.status_code)
                    content_type = (
                        response.headers.get("content-type", "").split(";", 1)[0].casefold()
                    )
                    title = None
                    text = ""
                    links: list[str] = []
                    metadata: dict = {}
                    if not failure and response.status_code < 400:
                        if content_type in {"text/html", "application/xhtml+xml", ""}:
                            title, text, links, metadata = extract_html(content, str(response.url))
                        elif content_type == "application/pdf":
                            try:
                                title, text, links, metadata = extract_pdf(content)
                            except Exception:
                                failure = FailureCode.PDF_UNREADABLE
                        elif content_type.startswith("text/"):
                            text = content.decode("utf-8", errors="replace")
                        else:
                            failure = (
                                FailureCode.IMAGE_ONLY
                                if content_type.startswith("image/")
                                else FailureCode.PARSING_FAILED
                            )
                    signal_text = "\n".join(filter(None, (title, text)))
                    usable = bool(
                        text
                        and (
                            len(text.strip()) >= 400 or _event_signal(signal_text, metadata, links)
                        )
                    )
                    if not failure and not usable:
                        failure = (
                            FailureCode.JS_REQUIRED
                            if content_type in {"text/html", ""}
                            else FailureCode.CONTENT_EMPTY
                        )
                    elapsed = (time.perf_counter() - timer) * 1000
                    attempt.elapsed_ms = round(elapsed)
                    attempt.failure = failure
                    if proxy and self.proxy_manager:
                        if failure in {
                            FailureCode.DNS_ERROR,
                            FailureCode.TIMEOUT,
                            FailureCode.TLS_ERROR,
                            FailureCode.CONNECTION_ERROR,
                        }:
                            self.proxy_manager.report_failure(
                                proxy.proxy_id, proxy_attributable=True
                            )
                        elif response.status_code == 407:
                            self.proxy_manager.report_failure(
                                proxy.proxy_id, proxy_attributable=True, auth_failure=True
                            )
                        else:
                            self.proxy_manager.report_success(proxy.proxy_id, elapsed)
                    return FetchResult(
                        requested_url=url,
                        final_url=str(response.url),
                        fetch_method="http",
                        status_code=response.status_code,
                        content_type=content_type,
                        title=title,
                        text=text,
                        links=links,
                        metadata=metadata,
                        headers={key.casefold(): value for key, value in response.headers.items()},
                        content_hash=hash_content(content),
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        proxy_used=bool(proxy),
                        usable=usable,
                        failure=failure,
                        attempts=[attempt],
                    )
        except ContentTooLarge:
            failure = FailureCode.CONTENT_TOO_LARGE
        except httpx.TimeoutException:
            failure = FailureCode.TIMEOUT
        except httpx.ConnectError as exc:
            failure = (
                FailureCode.TLS_ERROR
                if isinstance(exc.__cause__, ssl.SSLError)
                else FailureCode.CONNECTION_ERROR
            )
        except httpx.HTTPError:
            failure = FailureCode.CONNECTION_ERROR
        except Exception:
            failure = FailureCode.PARSING_FAILED
        attempt.elapsed_ms = round((time.perf_counter() - timer) * 1000)
        attempt.failure = failure
        if proxy and self.proxy_manager:
            self.proxy_manager.report_failure(
                proxy.proxy_id,
                proxy_attributable=failure
                in {FailureCode.TIMEOUT, FailureCode.TLS_ERROR, FailureCode.CONNECTION_ERROR},
            )
        return FetchResult(
            requested_url=url,
            fetch_method="http",
            proxy_used=bool(proxy),
            failure=failure,
            attempts=[attempt],
        )


class ContentTooLarge(Exception):
    pass


def _status_failure(status: int) -> FailureCode | None:
    if status == 401:
        return FailureCode.HTTP_401
    if status == 403:
        return FailureCode.HTTP_403
    if status == 404:
        return FailureCode.HTTP_404
    if status == 429:
        return FailureCode.HTTP_429
    if status >= 500:
        return FailureCode.SERVER_ERROR
    return None


def _event_signal(text: str, metadata: dict, links: list[str]) -> bool:
    lowered = text.casefold()
    has_topic = any(
        term in lowered
        for term in ("hackathon", "competition", "ideathon", "datathon", "challenge")
    )
    has_action = any(
        term in lowered for term in ("register", "registration", "apply", "deadline")
    ) or any("forms.gle" in link for link in links)
    return bool(metadata.get("event_json_ld") or (has_topic and has_action))
