"""One-time Facebook login for the philhacks scraper.

Complete captcha and 2FA in the headed Chrome window. The session is stored in
`data/.facebook-profile` and reused by later scrapes, so you should not have to
approve a new device on every run.

    $env:PYTHONPATH='src'
    python tools/facebook_login.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from akaton.config import load_config  # noqa: E402
from akaton.discovery.facebook import FacebookGroupSource  # noqa: E402
from akaton.fetch.proxy import ProxyManager, load_proxies  # noqa: E402


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config(ROOT)
    proxies, errors = load_proxies(ROOT / "proxies.txt")
    if errors:
        raise SystemExit("Invalid proxies.txt: " + "; ".join(errors[:8]))
    if not proxies:
        raise SystemExit("proxies.txt is empty")
    facebook = config.sources.get("structured_sources", {}).get("facebook", {})
    source = FacebookGroupSource(
        proxies=ProxyManager(proxies, "proxy_only"),
        profile_dir=ROOT / facebook.get("profile_dir", "data/.facebook-profile"),
        headless=False,
        login_wait_seconds=max(float(facebook.get("login_wait_seconds", 300)), 900),
        use_proxy=False,
        email=config.runtime.facebook_email,
        password=config.runtime.facebook_password,
    )
    print(
        "Chrome will open. Finish captcha and two-step verification in that window. "
        "The logged-in profile is saved for later scrapes.",
        flush=True,
    )
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        raise SystemExit('patchright is not installed; pip install -e ".[browser]"') from None
    from akaton.discovery.facebook import _BrowserSession

    async with async_playwright() as playwright:
        session = _BrowserSession(source, playwright)
        try:
            ok = await session.ensure_logged_in()
        finally:
            await session.close()
    print("Facebook session saved." if ok else "Facebook session was not established.", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
