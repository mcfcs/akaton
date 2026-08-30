# Akaton

Akaton is a single-process personal Discord bot that discovers Philippine hackathons,
business case competitions, ideathons, and related opportunities. It combines rotated Brave
web searches, configurable organizer searches, and selected structured sources. Candidates are
fetched, classified, verified, deduplicated, scored, versioned, and only then considered for a
Discord notification.

The defaults favor high-recall discovery and high-precision notifications. Notifications are off
until you explicitly enable them.

## What V1 includes

- Search-provider and source-adapter interfaces, with Brave Search and Devpost implementations.
- An optional Kaggle API adapter.
- Search-engine discovery of public Facebook, LinkedIn, and Instagram pages; no account login or
  authenticated social scraping.
- HTTP-first fetching, HTML/PDF extraction, conditional requests, conservative domain limits, and
  Patchright Chromium only when a policy permits browser fallback.
- SSRF protection on the initial request and redirects, bounded downloads, retries, rate-limit
  deferral, and a per-domain circuit breaker.
- Optional direct/proxy operation with parsed, redacted, health-tracked proxies.
- Deterministic extraction first and optional OpenAI structured extraction for ambiguous pages.
- SQLite event history, source snapshots, change detection, notification outbox, and Alembic's
  initial migration.
- Discord embeds plus `/upcoming`, `/deadlines`, `/search-now`, `/status`, and `/why` commands.
- A 6-hour rotating discovery job, adaptive known-event refresh, pending-notification recovery,
  and snapshot retention.

## Setup

Python 3.12 or newer is required. From PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
Copy-Item config/profile.example.yaml config/profile.yaml
```

Edit `config/profile.yaml` for the intended participant. It is ignored by Git because it can
contain personal information. Then edit `.env`:

```dotenv
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_USER_ID=123456789012345678
BRAVE_SEARCH_API_KEY=your_key
OPENAI_API_KEY=
OPENAI_MODEL=
NOTIFICATIONS_ENABLED=false
```

Create a bot in the Discord Developer Portal, invite it to the destination server with the `bot`
and `applications.commands` scopes, and grant it View Channel, Send Messages, Embed Links, and Read
Message History. `DISCORD_USER_ID` restricts commands and mentions that one account; leaving it
blank permits any member with channel access to invoke the commands.

Validate configuration and initialize storage:

```powershell
akaton validate-config
akaton init-db
python -m pytest -q
```

Run one discovery cycle without Discord:

```powershell
akaton discover-once
```

Start the bot and scheduler:

```powershell
akaton run
```

Keep `NOTIFICATIONS_ENABLED=false` for the first few cycles. Inspect `/status`, `/upcoming`, and
`/why event_id:<id>` before enabling delivery. Events and decisions are still stored in shadow
mode.

## Configuration

- `config/queries.yaml` contains weighted, rotating global and indexed-social queries.
- `config/sources.yaml` contains organizers, aliases, domains, authority, generated query
  templates, and structured-source cadence.
- `config/domains.yaml` controls requests per minute, concurrency, retries, timeouts, browser
  permission, proxy policy, and disabled sources.
- `config/scoring.yaml` contains deterministic weights and thresholds.
- `config/settings.yaml` contains scheduling, budget, retention, and notification defaults.
- `config/profile.yaml` contains the private participant profile.

To add an organizer, add an entry to `config/sources.yaml`; the configured templates automatically
produce targeted searches. To add or disable a query, edit YAML rather than application code.

OpenAI extraction is optional. When `OPENAI_API_KEY` and `OPENAI_MODEL` are absent, Akaton uses the
deterministic pipeline only. Source text is treated as untrusted data, typed output is required,
and claimed evidence must occur in the fetched source.

## Browser fallback

Install the optional browser dependency only if a permitted JavaScript page needs it:

```powershell
python -m pip install -e ".[browser]"
patchright install chromium
```

Browser fallback is never used for configured blocked social domains or HTTP 401/403/404/429
responses. It has isolated contexts, resource cleanup, navigation bounds, and no login workflow.

## Proxies

Proxies are optional. Copy `proxies.example.txt` to the ignored `proxies.txt` and use one raw proxy
per line. Supported forms are `IP:PORT`, `USER:PASS@IP:PORT`, and `http://`, `https://`, or
`socks5://` URIs. Markdown link syntax is deliberately rejected.

`PROXY_MODE` supports:

- `direct`: always use the local connection.
- `auto`: use direct HTTP first and a proxy only for transient connection failures when the domain
  policy permits it.
- `proxy` or `proxy_only`: require a healthy proxy.
- `disabled`: disable proxy use (an alias of `direct`).

Failures attributable to a proxy cause exponential temporary cooldowns. Origin HTTP restrictions
do not condemn a proxy, credentials are redacted, and Akaton does not rotate proxies to bypass
access controls.

## Storage and delivery guarantees

The default database is `data/akaton.db`. A candidate retains its pipeline trace and rejection
codes. Accepted events have immutable versions, linked source snapshots, material change records,
and stable annual-edition keys. A unique notification key prevents repeated new-event alerts.

Notifications use an outbox row before Discord delivery. On restart, pending rows are reconciled by
searching recent bot messages for an embed footer token before resending. Meaningful updates within
30 minutes are debounced. The daily maintenance task removes old unlinked snapshots and compacts
old linked source text while keeping every candidate's latest snapshot and event-version evidence.

## Docker

After creating `.env` and `config/profile.yaml`:

```powershell
docker compose up --build -d
```

The container runs as a non-root user and persists SQLite in `./data`. Browser fallback is excluded
from the default low-cost image. To include it, build with
`docker build --build-arg INSTALL_BROWSER=true -t akaton .`.

## Development

```powershell
python -m ruff format --check src migrations tests
python -m ruff check src migrations tests
python -m pytest -q
```

All decision tests use local fixtures and fakes; they require no Discord, search, browser, proxy,
website, or LLM access. Real credentials belong only in `.env`, and real proxy credentials only in
`proxies.txt`; both are ignored by Git.
