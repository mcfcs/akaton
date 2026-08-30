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
- No account login or authenticated social scraping. Facebook, LinkedIn, and Instagram are
  blocked in `config/domains.yaml`; results on those domains are rejected as
  `SEARCH_SNIPPET_ONLY` without a request, because they serve a JavaScript shell to
  anonymous clients and nothing usable can be extracted from it.
- HTTP-first fetching, HTML/PDF extraction, conditional requests, conservative domain limits, and
  Patchright Chromium only when a policy permits browser fallback.
- SSRF protection on the initial request and redirects, bounded downloads, retries, rate-limit
  deferral, and a per-domain circuit breaker.
- Optional direct/proxy operation with parsed, redacted, health-tracked proxies.
- Deterministic extraction first and local Ollama structured extraction for ambiguous pages.
- SQLite event history, source snapshots, change detection, notification outbox, and Alembic's
  initial migration.
- Discord embeds plus `/upcoming`, `/deadlines`, `/search-now`, `/backfill`, `/status`, and `/why`
  commands.
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
OLLAMA_MODEL=qwen2.5vl:7b
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

For an explicitly historical Discord test, temporarily enable notifications and run:

```text
/backfill since:2026-08-01 queries:4
```

This appends an `after:2026-08-01` constraint to the selected search queries and bypasses only the
past-event and closed-registration gates for that invocation. All normal scheduled discovery stays
strict. Start with two to four queries to avoid flooding the destination channel, then restore
`NOTIFICATIONS_ENABLED=false` if you only wanted a delivery test.

`/search-now` and `/backfill` acknowledge immediately and run in the background, then post their
summary to the notification channel. A full cycle can outlast Discord's 15-minute interaction
window, so the result cannot be delivered as a reply to the command. Each command refuses to
start a second run while its previous one is still going.

## Configuration

- `config/queries.yaml` contains weighted, rotating global, metro, category, platform, and
  Filipino-language queries.
- `config/sources.yaml` contains organizers, aliases, domains, authority, generated query
  templates, structured-source cadence, and a `platforms` map of domain to authority.
- `config/domains.yaml` controls requests per minute, concurrency, retries, timeouts, browser
  permission, proxy policy, and disabled sources. An entry matches its subdomains too, so
  `facebook.com` also covers `www.facebook.com`; add `match: exact` to restrict it.
- `config/scoring.yaml` contains deterministic weights and thresholds.
- `config/settings.yaml` contains scheduling, budget, retention, and notification defaults,
  including `discovery_concurrency`, the number of candidates processed in parallel within one
  search page. Per-domain rate limits still apply, so raising it only overlaps work across
  different domains.
- `config/profile.yaml` contains the private participant profile.

To add an organizer, add an entry to `config/sources.yaml`; the configured templates automatically
produce targeted searches. To add or disable a query, edit YAML rather than application code.

### Where candidates come from

| Source | What it covers | Limitation |
| --- | --- | --- |
| Rotating web queries | Organiser sites, news, aggregators | Depends on SearXNG's upstream engines |
| Organizer `site:` queries | One per listed organizer domain | Only finds organizers you have listed |
| Devpost API | Open hackathons naming the Philippines or Manila, plus open online ones | Few PH-located events are ever open there |
| Reddit, via uyam | Philippine subreddits | Needs uyam's collector to have run |

Devpost is queried through `https://devpost.com/api/hackathons` with `status[]=open` plus a
Philippines, Manila, or online filter. It previously scraped `/hackathons?status=open`, which is
every open hackathon on the site regardless of country, so most of what it produced was
unenterable. Note that Devpost currently lists no *open* Philippine hackathon at all: all 18 that
match "philippines" have ended, so the online query is what actually contributes.

Facebook is where a great many Philippine competitions are announced, and Akaton cannot read it.
Facebook, Instagram and LinkedIn serve a JavaScript shell to a logged-out client, and Akaton does
not log in, so those results are rejected as `SEARCH_SNIPPET_ONLY`. Search snippets still surface
such posts, and the organizer behind one can be added to `config/sources.yaml` so its own site is
queried directly from then on. That is the practical route from a Facebook post to a monitored
source.

### Reddit

Reddit is readable by neither Akaton's fetcher nor a plain HTTP client: a permalink returns a
JavaScript shell, `old.reddit.com` redirects to a login, and the unauthenticated `.json`
endpoints answer 403. `src/akaton/discovery/shreddit.py` and `shreddit_parse.py` are ported from
the sibling uyam project's shreddit collector and drive a real Chrome window instead.

Requires the browser extra, Google Chrome, and a desktop session:

```powershell
python -m pip install -e ".[browser]"
```

How a run goes, and why:

1. Headed Chrome through Patchright, which patches Playwright's automation fingerprints. A
   headless window is redirected to a `js_challenge` and answered with "you've been blocked by
   network security"; a headed one is not. This is why the job needs a desktop session.
2. A proxy per session from `proxies.txt`, with the browser relaunched on another IP when Reddit
   blocks the current one.
3. A persistent profile under `data/.browser-profile`, so a solved challenge survives relaunches.
4. Per subreddit and term, the search page is opened and its result permalinks collected. Search
   results are *not* `<shreddit-post>` elements, so their attributes cannot be read directly.
5. Each permalink is then opened, where the post does render as `<shreddit-post>`, and the title
   and body are read from its attributes. A comments page also renders recommended posts, so the
   element matching the permalink's own id is the one taken.

Captchas are never auto-solved. With `challenge_wait_seconds: 0` a challenged run rotates and
moves on, so an unattended monitor finds nothing rather than hanging; raise it if you intend to
sit and solve one in the window.

A post that links out is followed to that page, which is authoritative. A self-post carries its
own body on the candidate instead, since there is nothing to fetch. Everything after that is the
ordinary pipeline, so a Reddit lead still has to clear the same authority and confidence gates as
any other candidate; a passing mention of a hackathon in an unrelated thread is found by the
keyword filter and then dropped by them.

The verifier accepts a lone source only at authority 60 or above, and an unlisted site scores 50,
so it is rejected with `LOW_AUTHORITY`. `platforms` in `config/sources.yaml` admits trusted
listing sites by domain, and covers subdomains. It seeds the restricted `gov.ph` and `edu.ph`
suffixes, which only Philippine government agencies and accredited schools can hold. When a real
event is rejected for `LOW_AUTHORITY`, adding its domain there is the intended fix.

The default LLM fallback is the Ollama service at `100.102.10.69`, using the installed
`qwen2.5vl:7b` model. Akaton invokes it only when deterministic extraction is ambiguous. Set
`LLM_PROVIDER=disabled` to use deterministic extraction exclusively.

### What the LLM is for, and whether you need it

Deterministic extraction handles a page with regexes and keyword lists. `should_use_llm` sends a
page to the model only when that result is thin: overall confidence below 0.75, or a missing
title, date, or category. The model is a fallback for messy pages, not the primary reader, and
most pages never reach it. It is also the throughput limit, because Ollama serialises requests per
model, so `llm_concurrency` defaults to 1 while fetching stays parallel.

Measured against the 15 classification fixtures in `tests/fixtures/events.json`:

| model | per document | usable | category | document kind |
| --- | --- | --- | --- | --- |
| deterministic, no LLM | instant | 15/15 | 15/15 | 15/15 |
| `qwen2.5vl:7b` (default) | 14.0s | 15/15 | 14/15 | 5/15 |
| `dolphin3:8b` | 12.0s | 15/15 | 12/15 | 6/15 |
| `llama3:8b` | 8.1s | 15/15 | 10/15 | 3/15 |
| `qwen3.5:27b` | 105.5s | 10/15 | 8/15 | 8/15 |

`qwen2.5vl:7b` replaced `qwen3.5:27b` as the default: it is roughly seven times faster and more
accurate on category, and none of its extractions were discarded by `validate_llm_evidence`,
against 5 of 15 for the 27B model. `llama3:8b` is faster still but returned a confidence below
0.75 on 12 of 15 documents, which the verifier rejects outright, so it is not a usable swap.

Read that table with its bias in mind: these fixtures were written for the deterministic
extractor, so they favour it, and they contain none of the awkward real-world pages the LLM
exists to rescue. What they do show is that no model here reads `document_kind` reliably, and that
a 27B model is not buying accuracy for its cost.

`LLM_PROVIDER=disabled` is a supported configuration and makes runs dramatically faster. Discovery
still works: the ImaGnation page, for instance, extracts at 0.83 confidence deterministically and
never calls the model. Disabling it costs recall only on pages the regexes cannot read, which are
rejected as `LOW_CONFIDENCE` instead. Start with it disabled if you want fast cycles, and turn it
on if `LOW_CONFIDENCE` dominates your rejection counts. OpenAI remains an optional
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
   choose **Copy User ID** for `DISCORD_USER_ID`. Right-click the server and choose **Copy Server
   ID** for `DISCORD_GUILD_ID`.

`DISCORD_USER_ID` must be your own account ID, not the application's. Every command is refused
for any other user, so pasting the bot's ID here makes the bot answer "Not authorized" to you.

Set `DISCORD_GUILD_ID` so commands register against that server and appear immediately. Without
it they are registered globally, which Discord can take up to an hour to propagate.

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

Throttling is the practical limit on how often discovery can run, and it is easy to reach. After
several 16-query runs in quick succession, Brave, Google CSE, and Startpage suspended the instance
and DuckDuckGo timed out; SearXNG then answered HTTP 200 with an empty result list. Once an
instance is in that state a fresh 16-query burst re-suspends it immediately, while the engines
recover after a few idle minutes. The scheduled default of `discovery_queries_per_run: 8` every
six hours is well inside this limit; repeated back-to-back manual runs are not.

Akaton records such a run as a `FAILED` search naming the engines rather than as a successful run
that found nothing, so the dashboard and `/status` distinguish a throttled backend from a quiet
week. If searches start failing this way, leave the instance idle for a while or move to
`SEARCH_PROVIDER=brave` with an API key, which queries an owned index instead.

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
per line. Supported forms are `IP:PORT`, `HOST:PORT:USERNAME:PASSWORD`, `USER:PASS@IP:PORT`, and
`http://`, `https://`, or `socks5://` URIs. Markdown link syntax is deliberately rejected.

`HOST:PORT:USERNAME:PASSWORD` is what most vendors hand out and is the same layout uyam expects,
so one file serves both projects. Any unparseable line aborts startup rather than silently
shrinking the pool, so a malformed `proxies.txt` will stop `akaton run` from starting at all.

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
