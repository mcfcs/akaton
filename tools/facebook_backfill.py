"""One-off philhacks scrape: July through now, proxies.txt, Discord + dashboard.

Run from the repo root:

    $env:PYTHONPATH='src'
    python tools/facebook_backfill.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from akaton.config import load_config  # noqa: E402
from akaton.discovery.facebook import FacebookGroupSource  # noqa: E402
from akaton.discovery.facebook_parse import mention_kind  # noqa: E402
from akaton.domain.models import DeliveryReceipt, NotificationPayload  # noqa: E402
from akaton.fetch.manager import FetchManager  # noqa: E402
from akaton.fetch.policies import DomainPolicyResolver  # noqa: E402
from akaton.fetch.proxy import ProxyManager, load_proxies  # noqa: E402
from akaton.persistence.database import Database, upgrade_database  # noqa: E402
from akaton.pipeline import CandidatePipeline  # noqa: E402

SINCE = datetime(2026, 7, 1, tzinfo=UTC)
DUMP = ROOT / "data" / "facebook-backfill.json"


class DiscordRestNotifier:
    """Send embeds through the bot token over HTTP so we do not steal the gateway."""

    def __init__(self, token: str, channel_id: int) -> None:
        self.token = token
        self.channel_id = channel_id
        self.http = httpx.AsyncClient(timeout=30)

    async def send(self, payload: NotificationPayload) -> DeliveryReceipt:
        color = 0xE74C3C if payload.relevance_tier == "HIGH_PRIORITY" else 0x2ECC71
        fields = [
            {"name": name[:256], "value": (value or "Not specified")[:1024], "inline": False}
            for name, value in payload.fields.items()
        ]
        links = []
        if payload.registration_url:
            links.append(f"[Register]({payload.registration_url})")
        if payload.official_url:
            links.append(f"[Official announcement]({payload.official_url})")
        if links:
            fields.append({"name": "Links", "value": " · ".join(links), "inline": False})
        fields.append({"name": "Confidence", "value": payload.confidence_label, "inline": True})
        fields.append(
            {
                "name": "Relevance",
                "value": payload.relevance_tier.replace("_", " ").title(),
                "inline": True,
            }
        )
        response = await self.http.post(
            f"https://discord.com/api/v10/channels/{self.channel_id}/messages",
            headers={"Authorization": f"Bot {self.token}", "Content-Type": "application/json"},
            json={
                "embeds": [
                    {
                        "title": payload.title[:256],
                        "description": (payload.description or "")[:4096],
                        "url": payload.official_url,
                        "color": color,
                        "fields": fields,
                        "footer": {"text": payload.footer_token},
                    }
                ],
                "allowed_mentions": {"parse": []},
            },
        )
        response.raise_for_status()
        return DeliveryReceipt(message_id=str(response.json()["id"]))

    async def send_text(self, content: str) -> str:
        response = await self.http.post(
            f"https://discord.com/api/v10/channels/{self.channel_id}/messages",
            headers={"Authorization": f"Bot {self.token}", "Content-Type": "application/json"},
            json={"content": content[:1900], "allowed_mentions": {"parse": []}},
        )
        response.raise_for_status()
        return str(response.json()["id"])

    async def aclose(self) -> None:
        await self.http.aclose()


def _thread_report(posts) -> list[dict]:
    rows = []
    for post in posts:
        comments = [
            {
                "kind": mention_kind(comment.text, comment.urls),
                "text": comment.text[:400],
                "urls": comment.urls[:8],
                "author": comment.author,
            }
            for comment in post.comments
        ]
        rows.append(
            {
                "post_id": post.post_id,
                "permalink": post.permalink,
                "kind": mention_kind(post.text, post.urls),
                "text": post.text[:500],
                "urls": post.urls[:8],
                "created_at": post.created_at.isoformat() if post.created_at else None,
                "comment_count": len(post.comments),
                "comments": comments,
            }
        )
    return rows


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config(ROOT)
    proxies, errors = load_proxies(ROOT / "proxies.txt")
    if errors:
        raise SystemExit("Invalid proxies.txt: " + "; ".join(errors[:8]))
    print(f"Loaded {len(proxies)} proxies from proxies.txt for outbound fetches", flush=True)
    creds = bool(config.runtime.facebook_email and config.runtime.facebook_password)
    print(
        "Chrome will open on your connection (no proxy). Proxy login was restarting "
        "Facebook's captcha. Credentials from .env will be typed if present. "
        "If a captcha or 2FA appears, solve it once in that window (up to 15 minutes).",
        flush=True,
    )
    print(f"Facebook credentials loaded: {creds}", flush=True)
    manager = ProxyManager(proxies, config.runtime.proxy_mode)
    facebook = config.sources.get("structured_sources", {}).get("facebook", {})
    source = FacebookGroupSource(
        proxies=manager,
        profile_dir=ROOT / facebook.get("profile_dir", "data/.facebook-profile"),
        groups=None,
        headless=False,
        max_age_days=62,
        login_wait_seconds=max(float(facebook.get("login_wait_seconds", 300)), 900),
        use_proxy=False,
        email=config.runtime.facebook_email,
        password=config.runtime.facebook_password,
        scroll_rounds=int(facebook.get("scroll_rounds", 16)),
        max_posts=int(facebook.get("max_posts", 60)),
        max_permalinks=int(facebook.get("max_permalinks", 35)),
        min_interval_seconds=4.0,
    )
    seeds = await source.discover(since=SINCE)
    report = _thread_report(source.last_posts)
    DUMP.parent.mkdir(parents=True, exist_ok=True)
    DUMP.write_text(
        json.dumps(
            {
                "scraped_at": datetime.now(UTC).isoformat(),
                "since": SINCE.isoformat(),
                "posts": report,
                "seeds": [
                    {
                        "url": str(seed.url),
                        "title": seed.title,
                        "source_key": seed.source_key,
                        "has_content": bool(seed.content),
                        "links": seed.links,
                    }
                    for seed in seeds
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    kinds = {}
    for row in report:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
        for comment in row["comments"]:
            key = f"comment:{comment['kind']}"
            kinds[key] = kinds.get(key, 0) + 1
    print(f"Posts scraped: {len(report)}", flush=True)
    print(f"Classification counts: {kinds}", flush=True)
    print(f"Seeds kept: {len(seeds)}", flush=True)
    for seed in seeds:
        print(f"  SEED {seed.title!r} -> {seed.url}", flush=True)

    token = config.runtime.discord_bot_token
    channel_id = config.runtime.discord_channel_id
    notifier = None
    if token and channel_id:
        notifier = DiscordRestNotifier(token, channel_id)

    upgrade_database(config.runtime.database_url, ROOT)
    database = Database(config.runtime.database_url)
    policies = DomainPolicyResolver(config.domains)
    from akaton.fetch.http import HttpFetcher

    fetcher = FetchManager(HttpFetcher(proxy_manager=manager), policies, proxies=manager)
    pipeline = CandidatePipeline(database, config, fetcher, notifier=notifier)
    outcomes: list[tuple[str, str, str | None]] = []
    for seed in seeds:
        outcome = await pipeline.process(seed, historical_test=True)
        outcomes.append((str(seed.url), outcome.state, outcome.reason))
        print(
            f"  PIPE {outcome.state} {outcome.reason or ''} {seed.title!r}",
            flush=True,
        )

    lines = [
        f"Facebook philhacks backfill {SINCE.date()} → today.",
        f"Posts scraped: {len(report)}. Seeds kept: {len(seeds)}.",
        "Post kinds: "
        + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()) if not k.startswith("comment:")),
        "Pipeline: "
        + ", ".join(
            f"{state}={sum(1 for _, s, _ in outcomes if s == state)}"
            for state in sorted({s for _, s, _ in outcomes})
        )
        if outcomes
        else "Pipeline: no seeds",
    ]
    if notifier:
        try:
            message_id = await notifier.send_text("\n".join(lines))
            print(f"Discord summary message id {message_id}", flush=True)
        except Exception as exc:
            print(f"Discord summary failed: {type(exc).__name__}", flush=True)
        await notifier.aclose()
    await database.close()
    print("Dump:", DUMP, flush=True)
    return 0 if seeds else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
