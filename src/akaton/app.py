from __future__ import annotations

import argparse
import asyncio
import importlib.util
from datetime import date
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from akaton.config import ConfigBundle, load_config
from akaton.dashboard.runtime import MonitorController
from akaton.dashboard.web import create_dashboard
from akaton.discord.bot import AkatonBot
from akaton.discord.notifier import DiscordNotifier, reconcile_pending_notifications
from akaton.discovery.adapters import DevpostAdapter, KaggleAdapter
from akaton.discovery.brave import BraveSearchProvider
from akaton.discovery.searxng import SearXNGSearchProvider
from akaton.discovery.shreddit import DEFAULT_SUBREDDITS, DEFAULT_TERMS, ShredditSource
from akaton.fetch.browser import PatchrightRenderer
from akaton.fetch.http import HttpFetcher
from akaton.fetch.manager import FetchManager
from akaton.fetch.policies import DomainPolicyResolver
from akaton.fetch.proxy import ProxyManager, load_proxies
from akaton.jobs.discovery import DiscoveryJob
from akaton.jobs.maintenance import MaintenanceJob
from akaton.jobs.refresh import RefreshJob
from akaton.observability import configure_logging
from akaton.persistence.database import Database, upgrade_database
from akaton.pipeline import CandidatePipeline
from akaton.processing.llm import OllamaLLMProvider, OpenAILLMProvider


def _dependencies(config: ConfigBundle, notifier=None, *, database: Database | None = None):
    database = database or Database(config.runtime.database_url)
    proxies, errors = load_proxies(config.root / "proxies.txt")
    if errors:
        raise ValueError("Invalid proxies.txt entries: " + "; ".join(errors))
    proxy_manager = ProxyManager(proxies, config.runtime.proxy_mode)
    policies = DomainPolicyResolver(config.domains)
    http = HttpFetcher(
        max_download_bytes=config.app.max_download_bytes, proxy_manager=proxy_manager
    )
    browser = PatchrightRenderer() if importlib.util.find_spec("patchright") else None
    fetcher = FetchManager(http, policies, browser=browser, proxies=proxy_manager)
    llm = None
    if (
        config.runtime.llm_provider == "openai"
        and config.runtime.openai_api_key
        and config.runtime.openai_model
    ):
        llm = OpenAILLMProvider(config.runtime.openai_api_key, config.runtime.openai_model)
    elif config.runtime.llm_provider == "ollama":
        llm = OllamaLLMProvider(
            config.runtime.ollama_base_url,
            config.runtime.ollama_model,
        )
    pipeline = CandidatePipeline(database, config, fetcher, llm=llm, notifier=notifier)
    return database, fetcher, pipeline


def _search_provider(config: ConfigBundle):
    if config.runtime.search_provider == "brave":
        if not config.runtime.brave_search_api_key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required when SEARCH_PROVIDER=brave")
        return BraveSearchProvider(config.runtime.brave_search_api_key)
    return SearXNGSearchProvider(config.runtime.searxng_base_url)


def _source_adapters(config: ConfigBundle, fetcher: FetchManager):
    structured = config.sources.get("structured_sources", {})
    adapters = []
    if structured.get("devpost", {}).get("enabled", True):
        adapters.append(DevpostAdapter(fetcher))
    if structured.get("kaggle", {}).get("enabled"):
        adapters.append(KaggleAdapter())
    reddit = structured.get("reddit", {})
    if reddit.get("enabled"):
        adapters.append(
            ShredditSource(
                proxies=getattr(fetcher, "proxies", None),
                profile_dir=config.root / reddit.get("profile_dir", "data/.browser-profile"),
                subreddits=tuple(reddit.get("subreddits") or DEFAULT_SUBREDDITS),
                terms=tuple(reddit.get("terms") or DEFAULT_TERMS),
                headless=bool(reddit.get("headless", False)),
                max_age_days=int(reddit.get("max_age_days", 90)),
                challenge_wait_seconds=float(reddit.get("challenge_wait_seconds", 0)),
                max_posts_per_term=int(reddit.get("max_posts_per_term", 5)),
            )
        )
    return adapters


def _monitor_jobs(
    config: ConfigBundle,
    database: Database,
    pipeline: CandidatePipeline,
    fetcher: FetchManager,
):
    provider = _search_provider(config)
    discovery = DiscoveryJob(
        database, config, provider, pipeline, _source_adapters(config, fetcher)
    )
    refresh = RefreshJob(database, pipeline)
    maintenance = MaintenanceJob(database, config.app.snapshot_retention_days)
    return discovery, refresh, maintenance


def _scheduler(
    config: ConfigBundle,
    discovery: DiscoveryJob,
    refresh: RefreshJob,
    maintenance: MaintenanceJob,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.app.timezone))
    scheduler.add_job(
        discovery.run,
        "interval",
        hours=config.app.discovery_interval_hours,
        jitter=15 * 60,
        max_instances=1,
        coalesce=True,
        id="discovery",
    )
    scheduler.add_job(
        refresh.run,
        "interval",
        hours=config.app.refresh_interval_hours,
        jitter=10 * 60,
        max_instances=1,
        coalesce=True,
        id="refresh",
    )
    scheduler.add_job(
        maintenance.run,
        "cron",
        hour=3,
        minute=30,
        max_instances=1,
        coalesce=True,
        id="maintenance",
    )
    return scheduler


def _web_server(
    config: ConfigBundle, database: Database, controller: MonitorController
) -> uvicorn.Server:
    application = create_dashboard(database, controller, config)
    server_config = uvicorn.Config(
        application,
        host=config.runtime.dashboard_host,
        port=config.runtime.dashboard_port,
        log_config=None,
        access_log=False,
    )
    return uvicorn.Server(server_config)


async def _discover_once(config: ConfigBundle) -> None:
    database, fetcher, pipeline = _dependencies(config)
    await asyncio.to_thread(upgrade_database, config.runtime.database_url, config.root)
    discovery, _, _ = _monitor_jobs(config, database, pipeline, fetcher)
    counts = await discovery.run()
    print(counts)
    await database.close()


async def _backfill(config: ConfigBundle, since: date, query_limit: int) -> None:
    database, fetcher, pipeline = _dependencies(config)
    await asyncio.to_thread(upgrade_database, config.runtime.database_url, config.root)
    discovery, _, _ = _monitor_jobs(config, database, pipeline, fetcher)
    print(
        "HISTORICAL TEST MODE: past-event and registration-deadline gates are bypassed; "
        "normal scheduled discovery remains strict."
    )
    counts = await discovery.run(
        since=since,
        historical_test=True,
        query_limit=query_limit,
    )
    print(counts)
    await database.close()


async def _dashboard(config: ConfigBundle) -> None:
    database, fetcher, pipeline = _dependencies(config)
    await asyncio.to_thread(upgrade_database, config.runtime.database_url, config.root)
    discovery, refresh, maintenance = _monitor_jobs(config, database, pipeline, fetcher)
    scheduler = _scheduler(config, discovery, refresh, maintenance)
    scheduler.start(paused=not config.runtime.dashboard_auto_start)
    controller = MonitorController(scheduler, discovery.run, refresh.run)
    server = _web_server(config, database, controller)
    try:
        await server.serve()
    finally:
        scheduler.shutdown(wait=False)
        await database.close()


async def _run(config: ConfigBundle) -> None:
    if not config.runtime.discord_bot_token or not config.runtime.discord_channel_id:
        raise ValueError("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID are required to run the bot")
    database = Database(config.runtime.database_url)
    await asyncio.to_thread(upgrade_database, config.runtime.database_url, config.root)
    bot = AkatonBot(
        database=database,
        authorized_user_id=config.runtime.discord_user_id,
        guild_id=config.runtime.discord_guild_id,
        channel_id=config.runtime.discord_channel_id,
    )
    notifier = DiscordNotifier(bot, config.runtime.discord_channel_id)
    _, fetcher, pipeline = _dependencies(config, notifier=notifier, database=database)
    discovery, refresh, maintenance = _monitor_jobs(config, database, pipeline, fetcher)
    bot.run_discovery = discovery.run
    bot.run_backfill = lambda since, queries: discovery.run(
        since=since,
        historical_test=True,
        query_limit=queries,
    )
    reconciliation_complete = False

    @bot.event
    async def on_ready() -> None:
        nonlocal reconciliation_complete
        if not reconciliation_complete:
            await reconcile_pending_notifications(database, notifier)
            reconciliation_complete = True

    scheduler = _scheduler(config, discovery, refresh, maintenance)
    scheduler.start(paused=not config.runtime.dashboard_auto_start)
    controller = MonitorController(scheduler, discovery.run, refresh.run)
    server = _web_server(config, database, controller)
    bot_task = asyncio.create_task(bot.start(config.runtime.discord_bot_token), name="discord")
    web_task = asyncio.create_task(server.serve(), name="dashboard")
    try:
        done, _ = await asyncio.wait({bot_task, web_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if error := task.exception():
                raise error
    finally:
        server.should_exit = True
        if not bot.is_closed():
            await bot.close()
        for task in (bot_task, web_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(bot_task, web_task, return_exceptions=True)
        scheduler.shutdown(wait=False)
        await database.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akaton")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--allow-example-profile", action="store_true")
    subparsers.add_parser("init-db")
    subparsers.add_parser("discover-once")
    backfill = subparsers.add_parser("backfill")
    backfill.add_argument("--since", type=date.fromisoformat, required=True)
    backfill.add_argument("--queries", type=int, default=16)
    subparsers.add_parser("dashboard")
    subparsers.add_parser("run")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    allow_example = getattr(args, "allow_example_profile", False) or args.command == "init-db"
    config = load_config(allow_example_profile=allow_example)
    configure_logging(config.runtime.log_level)
    if args.command == "validate-config":
        print("Configuration is valid.")
    elif args.command == "init-db":

        async def initialize() -> None:
            database = Database(config.runtime.database_url)
            await asyncio.to_thread(upgrade_database, config.runtime.database_url, config.root)
            await database.close()

        asyncio.run(initialize())
        print("Database initialized.")
    elif args.command == "discover-once":
        asyncio.run(_discover_once(config))
    elif args.command == "backfill":
        if args.queries < 1:
            raise ValueError("--queries must be at least 1")
        asyncio.run(_backfill(config, args.since, args.queries))
    elif args.command == "dashboard":
        asyncio.run(_dashboard(config))
    else:
        asyncio.run(_run(config))


if __name__ == "__main__":
    main()
