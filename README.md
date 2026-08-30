# Akaton

Akaton is a single-process personal competition monitor with Discord and a private web dashboard.
It discovers Philippine hackathons, business case competitions, ideathons, and related
opportunities. It combines rotated SearXNG or Brave web searches, configurable organizer searches,
and selected structured sources. Candidates are
fetched, classified, verified, deduplicated, scored, versioned, and only then considered for a
Discord notification.

The defaults favor high-recall discovery and high-precision notifications. Notifications are off
until you explicitly enable them.

## What V1 includes

- Search-provider and source-adapter interfaces, with private SearXNG, Brave Search, and Devpost
  implementations.
- An optional Kaggle API adapter.
- Search-engine discovery of public Facebook, LinkedIn, and Instagram pages; no account login or
  authenticated social scraping.
- HTTP-first fetching, HTML/PDF extraction, conditional requests, conservative domain limits, and
  Patchright Chromium only when a policy permits browser fallback.
- SSRF protection on the initial request and redirects, bounded downloads, retries, rate-limit
  deferral, and a per-domain circuit breaker.
- Optional direct/proxy operation with parsed, redacted, health-tracked proxies.
- Deterministic extraction first and local Ollama structured extraction for ambiguous pages.
- SQLite event history, source snapshots, change detection, notification outbox, and Alembic's
  initial migration.
- Discord embeds plus `/upcoming`, `/deadlines`, `/search-now`, `/status`, and `/why` commands.
- A 6-hour rotating discovery job, adaptive known-event refresh, pending-notification recovery,
  and snapshot retention.
- A Tailscale-only dashboard with live status, events, candidates, rejection reasons, and monitor
  controls.

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
SEARCH_PROVIDER=searxng
SEARXNG_BASE_URL=http://127.0.0.1:8888
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://100.102.10.69:11434
OLLAMA_MODEL=qwen3.5:27b
DASHBOARD_HOST=100.70.66.3
DASHBOARD_PORT=8765
NOTIFICATIONS_ENABLED=false
```

Discord values are only required for `akaton run`. `akaton dashboard` works without Discord.

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

Or start the dashboard and scheduler without Discord:

```powershell
akaton dashboard
```

On this machine it is available only through Tailscale at
`http://100.70.66.3:8765`. The port was selected because it was not listening when configured.

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

The default LLM fallback is the Ollama service at `100.102.10.69`, using the installed
`qwen3.5:27b` model. Akaton invokes it only when deterministic extraction is ambiguous. Set
`LLM_PROVIDER=disabled` to use deterministic extraction exclusively. OpenAI remains an optional
fallback and is used only when `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and `OPENAI_MODEL` are all
configured. Source text is treated as untrusted data, typed output is required, and claimed
evidence must occur in the fetched source.

## Getting the required credentials

### Discord

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), select
   **New Application**, and give it a name.
2. Open **Bot** and use **Reset Token** to create the bot token. Put it in
   `DISCORD_BOT_TOKEN`; never post or commit it.
3. Open **Installation**. For a Guild Install, select the `bot` and `applications.commands`
   scopes. Grant only View Channel, Send Messages, Embed Links, and Read Message History.
4. Copy the install link, open it, and add the bot to the server containing the notification
   channel.
5. In Discord, enable **User Settings > Advanced > Developer Mode**. Right-click the destination
   channel and choose **Copy Channel ID** for `DISCORD_CHANNEL_ID`. Right-click your own profile and
   choose **Copy User ID** for `DISCORD_USER_ID`.

The application uses a Discord bot token, not a user token. It does not require an interactions
public key or interactions endpoint because `discord.py` registers commands and receives events
through Discord's Gateway connection.

### Brave Search (optional)

1. Open the [Brave Search API dashboard](https://api-dashboard.search.brave.com/), create an
   account, and verify the email address.
2. Activate an available Search API plan. Brave currently requires plan activation before it lets
   you create a key; review the current pricing and included credits in the dashboard.
3. Open **API Keys**, choose **Add API Key**, and name it something like `akaton-personal`.
4. Copy it once into `BRAVE_SEARCH_API_KEY` in `.env`. Do not place it in YAML or commit it.
5. Run `akaton discover-once`. A valid key should produce search-run records instead of an HTTP
   authentication error.

## Running without paid APIs

The software path can be zero-cost: SQLite, deterministic parsing, the Ollama server at
`100.102.10.69`, a private SearXNG instance, and the dashboard are all self-hosted. This excludes
electricity, hardware, and internet service. SearXNG does not own a search index; it queries
configured upstream engines, so those engines can throttle it or return fewer results.

Patchright is not a search engine. It can render a JavaScript event page after discovery, but using
it to automate consumer search-result pages would be brittle, CAPTCHA-prone, and inappropriate as
the primary discovery strategy.

Start the free stack after Docker Desktop is running and `config/profile.yaml` exists:

```powershell
docker compose -f compose.free.yaml up --build -d
```

This starts:

- private SearXNG on `127.0.0.1:8888`;
- Akaton's dashboard and scheduler on `100.70.66.3:8765`;
- the existing Ollama model over Tailscale at `100.102.10.69:11434`.

For native Python development, start only SearXNG and run the dashboard locally:

```powershell
docker compose -f compose.free.yaml up -d searxng
akaton dashboard
```

To use Brave instead, set `SEARCH_PROVIDER=brave` and provide `BRAVE_SEARCH_API_KEY`. As of August
2026, Brave advertises monthly credits that cover the configured 950-request budget, but it still
requires account, plan, card, and API-key setup; verify current terms before relying on that credit.

## Dashboard

The dashboard polls the local database and shows:

- scheduler state and next jobs;
- candidate, event, and notification totals;
- pipeline-state counts and the last search query;
- recent accepted events, scores, deadlines, and source links;
- recent candidates, providers, rejection codes, retry state, and last pipeline transition.

Controls trigger one discovery run, refresh known events, or pause/resume automatic scheduling.
Concurrent duplicate runs are rejected. Set `DASHBOARD_TOKEN` to a long random value for an
additional header check; the page stores it only in that browser's local storage. Keep the service
bound to the Tailscale address and enforce suitable Tailscale ACLs. If the machine's Tailscale IP
changes, update `DASHBOARD_HOST` and `TAILSCALE_IP` in `.env`.

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

After creating `.env` and `config/profile.yaml`, the Discord-oriented container remains:

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
