from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit


def _safe_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def validate_public_url(url: str, *, resolve_dns: bool = True) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise ValueError("credential-bearing URLs are not allowed")
    if not parts.hostname:
        raise ValueError("URL host is missing")
    try:
        if not _safe_ip(parts.hostname):
            raise ValueError("private or unsafe IP address")
        return
    except ValueError as exc:
        if "does not appear" not in str(exc):
            raise
    if not resolve_dns:
        return
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    )
    if not infos or any(not _safe_ip(item[4][0]) for item in infos):
        raise ValueError("host resolves to a private or unsafe address")
