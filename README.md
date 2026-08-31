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
- Optional headed Chrome sources for Reddit and Facebook groups, using Patchright the
  same way uyam does. LinkedIn and Instagram stay blocked in `config/domains.yaml`;
  Facebook search snippets are still rejected as `SEARCH_SNIPPET_ONLY` because the
  HTTP fetcher cannot read them. The facebook adapter is a separate headed session
  with a persistent login, not that HTTP path.
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
| Organizer name queries | Two per listed organizer | Only finds organizers you have listed |
| Devpost API | Open hackathons naming the Philippines, Manila or Filipino | Few PH-located events are ever open there |
| Reddit, via headed Chrome | r/PinoyProgrammer, r/ITPhilippines, r/ProgrammerPH | Needs Patchright, Chrome, and a desktop session |
| Facebook groups, via headed Chrome | philhacks posts and replies | Needs a one-time Facebook login in the persistent profile |

Organizer queries are name-based (`"DICT" hackathon registration Philippines`), not `site:`-based.
Measured against the live instance, `site:dict.gov.ph hackathon registration` returns **zero**
results and `site:gcash.com "case competition"` returns one irrelevant promo page, while the name
queries return twenty each and surface the actual announcements — including GCash's ImaGnation,
which was posted to Facebook and no `site:gcash.com` query could ever have reached. A `site:`
operator also narrows the answering engine pool from about six engines to three, so those queries
were both the least productive and the first to fail under throttling. The organizer's authority
score still lets its own domain clear the verifier's gate when the announcement is there.

Devpost is queried through `https://devpost.com/api/hackathons` with `status[]=open` plus a
Philippines, Manila or Filipino filter. It previously scraped `/hackathons?status=open`, which is
every open hackathon on the site regardless of country, so most of what it produced was
unenterable; `challenge_type[]=online` was the same mistake in a narrower form — every open online
hackathon in the world, and the highest-volume lowest-precision producer in a run. An online
hackathon a Filipino can join still arrives when it names the country.

Instagram and LinkedIn still serve a JavaScript shell to a logged-out client, so those search
hits are rejected as `SEARCH_SNIPPET_ONLY`. Facebook search snippets are treated the same way.
The dedicated `facebook` adapter is the path that actually reads a group: it drives headed
Chrome, not the HTTP fetcher.

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

### Facebook groups through Patchright

A great many Philippine competitions are announced in
[philhacks](https://www.facebook.com/groups/philhacks/) rather than on a crawlable site, and
the event is sometimes only in a reply. A post such as
[this one](https://www.facebook.com/groups/philhacks/permalink/4116540755148003/) is just
"any upcoming hackathon near Manila?"; the listing is in a comment. The adapter therefore
scrolls the group feed, opens threads that look incomplete, and classifies the post and
every reply independently.

Enable it under `structured_sources.facebook` in `config/sources.yaml` after the same browser
extra as Reddit. Log in once, including captcha and two-step verification, in the headed
Chrome window (`python tools/facebook_login.py`). The session is stored in
`data/.facebook-profile` and the proxy Facebook accepted is remembered, so later scrapes
reuse that login instead of prompting 2FA every cycle. A new IP from a different proxy
will often force another checkpoint; that is why the Facebook adapter stays on one sticky
proxy rather than rotating. Optional `FACEBOOK_EMAIL` / `FACEBOOK_PASSWORD` in `.env` only
fill the first login form.

A reply or post that links to Devpost, Unstop, Luma, Eventbrite, `.gov.ph`, or `.edu.ph` is
followed as the authoritative page. A Google Form or an unlisted site stays on the Facebook
document (social authority 75, which clears the verifier) with the outbound URL kept as a
registration link. Question posts, teammate-only posts, recaps, and jobs are dropped even
when they mention the word "hackathon".

The verifier accepts a lone source only at authority 60 or above, and an unlisted site scores 50,
so it is rejected with `LOW_AUTHORITY`. `platforms` in `config/sources.yaml` admits trusted
listing sites by domain, and covers subdomains. It seeds the restricted `gov.ph` and `edu.ph`
suffixes, which only Philippine government agencies and accredited schools can hold. When a real
event is rejected for `LOW_AUTHORITY`, adding its domain there is the intended fix.

The default LLM fallback is the Ollama service at `100.102.10.69`, using the installed
`qwen2.5vl:7b` model. Akaton invokes it only when deterministic extraction is ambiguous. Set
`LLM_PROVIDER=disabled` to use deterministic extraction exclusively.

### Mentions: when a post names a competition without linking to it

Most competition talk in a group is not an announcement. Someone asks whether one is coming up,
looks for teammates for one they already joined, or complains about one that has ended. Replaying
the recorded philhacks scrape, 15 of the mentions are that shape and only 8 posts are actual
announcements.

That evidence used to be thrown away on Facebook, and on Reddit it was worse: the question became
a candidate, spending a fetch and a fourteen-second model call before the verifier rejected it for
being a question. Neither found the competition it was talking about.

A **lead** is the third option. The name is lifted out of the text, one search finds the official
page, and *that page* goes through the normal pipeline — so it alerts only if it would have alerted
had it been found directly. The thread never becomes the candidate.

```
"pwede po ba manuod if hindi naka register sa egov hackaton?"
  -> lead "egov hackathon"          (question, philhacks)
  -> search "egov hackathon Philippines"
  -> https://dict.gov.ph/...        (authority 90, ranked above the news coverage)
  -> normal pipeline, alert labelled "Found via a Facebook mention"
```

**Naming is deterministic** — `processing/mentions.py`, no model. It anchors on a head term and
walks left over qualifying tokens, stopping at punctuation or a stopword; the stopword list carries
Tagalog function words (`sa ng na po ba may yung mga`) beside the English determiners, because the
posts are Taglish. A bare head is refused: "any upcoming hackathon events near manila" yields
nothing, because a lead there would spend a search request on the word "hackathon". At fourteen
seconds a call with `llm_concurrency: 1`, classifying a sixty-post run with the model would be
fourteen minutes of serialised time to decide what a regex settles.

**Repeat mentions cost nothing.** The lead is keyed on the folded name, so the three separate
philhacks posts about eGovPH — one of them spelling it "hackaton" — are one lead with three
sightings and one search. The canonical spelling is what gets searched, not whichever misspelling
arrived first.

**A new edition is still a new search.** Any year or month written within 40 characters of the name
goes into the key, so "the eGov hackathon" and "eGov hackathon September" are *different* leads and
the September one is searched at once rather than waiting out the first one's 30-day cooldown. A
September mention carrying no date at all does collapse onto the earlier lead — nothing in the text
distinguishes them, and the scheduled queries still cover the event.

**Ranking is by authority, not by position**, and that was decided by running the real queries:

| query | first result | what the resolver picks |
| --- | --- | --- |
| `ImaGnation Philippines` | `gcash.com/imagnation` | the same — already right |
| `Hack4Gov Philippines` | news coverage, authority 50 | `pia.gov.ph`, authority 85 |
| `eGov hackathon Philippines` | led by yugatech, mb.com.ph, inquirer | `dict.gov.ph`, authority 90 |

Taking the first result would have resolved two of those three to a page the verifier then rejects
as `LOW_AUTHORITY`, having already spent the fetch. Results on the social platforms are dropped
outright — one live hit was literally *"Questions about Hack4gov competition : r/PinoyProgrammer"*,
which is resolving a mention to another mention. Authority alone is not enough either: the same
search returned `elibrary.judiciary.gov.ph` at authority 85 purely for being under `gov.ph`, so the
name has to actually appear in the result.

**Budget.** Leads come out of the run's own allocation, never in addition to it, capped at
`mention_leads_per_run` (default 3) and at a third of the run. They are round-robined through the
same paced loop as the scheduled queries, so every request still gets its `search_interval_seconds`
gap — a second sub-loop would reintroduce the burst that gets SearXNG's engines suspended. A lead
found in one run is searched in the next, because this run's allocation was fixed before the
collectors produced it.

Leads and their outcomes are listed on the dashboard, and each search is also recorded as a normal
search run, so budget accounting and the Search-health panel need no special case.

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
recover after a few idle minutes. The scheduled default of `discovery_queries_per_run: 12` every
six hours, spaced by `search_interval_seconds`, is well inside this limit; repeated back-to-back
manual runs are not.

Akaton records such a run as a `FAILED` search naming the engines rather than as a successful run
that found nothing, so the dashboard and `/status` distinguish a throttled backend from a quiet
week. If searches start failing this way, leave the instance idle for a while or move to
`SEARCH_PROVIDER=brave` with an API key, which queries an owned index instead.

That distinction has to be drawn carefully, and at first it was not. Marking a search FAILED
whenever *any* engine was unresponsive reported 28 of 33 searches as broken while the instance was
returning 48 results a query: six engines are suspended on a routine day, and a `site:` query with
no matches is an empty answer, not an unreachable backend. A run is now FAILED only when **no**
engine capable of answering did (`PRIMARY_ENGINES` in `discovery/searxng.py`). A failed search also
no longer claims its cadence slot — `search_history` counts only successful runs, so a query
throttled out of one run is eligible again in the next rather than parked for another 6 to 72
hours.

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
2026, Brave advertises monthly credits in the same order as the configured 2000-request budget, so
check the current allowance against `monthly_search_budget` before switching — and note that it
requires account, plan, card, and API-key setup regardless.

## Dashboard

The dashboard polls the local database and shows:

- scheduler state and next jobs;
- candidate, event, and notification totals;
- pipeline-state counts and the last search query;
- recent accepted events, scores, deadlines, and source links;
- mentions being chased: competitions named on Facebook or Reddit without a link, their sighting
  counts, and whether each resolved to a page;
- recent candidates, providers, rejection codes, retry state, and last pipeline transition.

Controls trigger one discovery run, refresh known events, start or stop the Discord bot, send an
alert for one event by hand, or pause/resume automatic scheduling. Concurrent duplicate runs are
rejected.

The **Backdate** panel re-reads a chosen date range from chosen collectors, so a backfill no longer
needs the command line. It has a date (defaulting to a month back), a query budget, and a collector
picker whose entries come from the server — so it offers exactly the adapters this deployment
enabled rather than a list that drifts from `config/sources.yaml`.

Naming a collector waives its cadence: asking to read the Facebook group back to June means now,
not at the next six-hour boundary. Two backdates cannot overlap, and because a run over Facebook
and Reddit takes minutes, the button stays disabled and the panel shows `RUNNING` until the poll
reports it finished — then the run's own counts (queries, candidates, processed, errors, leads).
Past-event and registration-deadline gates are bypassed exactly as in the CLI.

With no collectors selected it runs search alone, matching `akaton backfill --since 2026-06-01`;
the equivalent of the panel with Facebook and Reddit ticked is:

```powershell
akaton backfill --since 2026-06-01 --sources facebook,reddit
```

`--sources` was needed because `since` never used to reach the adapters at all: `DiscoveryJob`
called `adapter.discover()` with no arguments, so only the search path honoured a backdate even
though both social collectors already accepted one.

Set `DASHBOARD_TOKEN` to a long random value for an
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
The Reddit and Facebook structured sources are a different path: they launch headed Chrome with
a persistent profile so a person can solve a challenge or log in once.

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
and stable edition keys. A unique notification key prevents repeated new-event alerts.

### Telling one run of a competition from the next

An edition key is `2026`, or `2026-09` when the start date is trustworthy — the same test
`verifier.deadline_past` applies before it will call a deadline expired. Everything downstream asks
whether two keys *positively disagree*, never whether they differ: `2026` is the same edition as
`2026-03` seen more precisely, and a raw `!=` would have split every stored row from its own next
update the moment that update parsed a month. That prefix tolerance is the whole compatibility
story, so the migration backfills nothing and old rows behave as they do today until refreshed.

This matters because three of the four shapes were failing, silently and in both directions:

| | before | now |
| --- | --- | --- |
| September reuses the March landing page | merged at 100 on URL identity — the second run never alerted | separate |
| 92-day gap, same organiser, new URL | `POSSIBLE_DUPLICATE`: no event, no alert, scan abandoned | separate |
| September page with no parsed date | same silent death | merged, conservatively |
| 184-day gap, different URL | separate | separate |

Reusing a landing page for the next run is what government and university sites do as a matter of
course, so the first row was the common case, not an edge case. Where a key cannot settle it, two
trustworthy starts more than 45 days apart do — *both* sides must be trustworthy, because a
Facebook announcement usually carries a deadline and no start at all, and reading that absence as
disagreement would break the collapse of three reposts into one alert.

`POSSIBLE_DUPLICATE` also no longer abandons the scan on the first weak match, so a genuine merge
later in the pool is still reached, and the reasons `compare_events` had already computed are
written into the candidate's trace instead of discarded.

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
