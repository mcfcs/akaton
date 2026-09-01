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
OLLAMA_BASE_URL=http://ollama.internal:11434
OLLAMA_MODEL=dolphin3:8b
DASHBOARD_HOST=100.100.100.100
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

It is available only through Tailscale, at `http://<your-tailscale-ip>:8765`. The port was selected
because it was not listening when configured.

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

The LLM fallback is whichever Ollama host `OLLAMA_BASE_URL` names — there is no default in code,
because in practice it is a private Tailscale address. Akaton invokes it only when deterministic
extraction is ambiguous. Set
`LLM_PROVIDER=disabled` to use deterministic extraction exclusively.

### Not re-reading what has already been judged

Search returns the same URLs every run. Measured on a real database, 97 of 364 candidates
had been fetched more than once — 491 fetches for 364 pages — and the most repeated was a
Facebook group URL fetched **seven times**, rejected identically each time because
`config/domains.yaml` disables fetching that host. No amount of asking changes that answer.

A candidate is now re-fetched only when the answer could plausibly differ, and how long
that takes depends on why it was dropped:

| situation | what happens |
| --- | --- |
| already an event | left to `RefreshJob`, which re-reads it on its own cadence |
| rejected on the *host or kind of page* — blocked domain, unlisted domain, foreign event, results post | `candidate_settled_recheck_days`, default 30 |
| rejected on *what the page had not said yet* — thin confidence, unconfirmed registration | `candidate_recheck_days`, default 7 |
| fetch failed | unchanged; `retry_at` already governs those with its own backoff |

On the same database that is **307 of 364 skipped, 84% fewer fetches**, while every page is
still re-examined eventually. Three things deliberately ignore the cooldown, because each
exists *in order to* re-read a page: `RefreshJob`, the dashboard's **Retry** button, and any
backdate. The sighting is still recorded either way — `last_seen_at` moves, so you can see
a URL keeps coming back without paying to fetch it again.

The cooldown reads the last verdict out of the candidate's trace rather than `updated_at`,
because recording a sighting touches the row: `updated_at` would be pushed forward by
exactly the URLs that keep reappearing, and their cooldown would never expire.

### Telling a news article from an event page

A university or agency news article about a competition is mostly *about the competition*: it names
it, describes it, quotes the organisers. On the body text alone it is indistinguishable from the
competition's own page. This was not hypothetical — six of the first eight events the live database
stored were wrong, and all eight had alerted:

| stored title | what it was |
| --- | --- |
| "CIT students **secure top spots** in HackForGov 5" | winner announcement |
| "WPU IDEA Pitch 2026 **Champions** Youth Innovation" | recap |
| "QCU **Hosts**… **Showcases Excellence**" | recap |
| "Polytechnic University of the Philippines" | a `/news/?go=…` listing page |
| two municipal tourism poster contests | real, but not a hackathon or case competition |

Three independent causes, each fixed:

**The tense lives in the headline.** `classify_document` now takes the title and URL, and applies a
much broader result and recap vocabulary to the *headline* than it dares apply to the body — because
a body-level rule cannot distinguish "cash prizes await the winning teams" in a live announcement
from a report of who won. The headline decides before the body is consulted, so a news article that
quotes the original call for entries no longer talks the classifier out of what its own headline
says. `tests/fixtures/news_vs_events.json` is the real page text of all eight, plus six documents
that were already being rejected correctly, as a guard against over-correcting.

**Some headlines say nothing.** "Polytechnic University of the Philippines" is unclassifiable, but
`pup.edu.ph/news/?go=…` is not. `is_news_url` treats a `/news/`, `/press-release/` or `/article/`
segment, or a `/YYYY/MM/DD/` date path, as a newsroom post. An explicit registration call in the
headline overrides it, so an organiser announcing on their own newsroom still passes.

**A backdate used to lower the bar.** `historical_test` relaxes the past-event and deadline gates —
that is its job — but it also skipped the notification threshold, so a backdate alerted for every new
event at any score. The three below-threshold events above (59, 64, 64 against 65) would never have
alerted from a scheduled run. The event is still created; only the alert is suppressed.

A search result's own headline is checked *before* the fetch too, so a recap costs no fetch,
no extraction and no model call.

One query was also removed as structurally unsound: `"calling all students" competition Philippines`
searched for a phrase that is itself an entry in `ACTION_TERMS`, which is what the classifier uses as
evidence that a page is a live call for entries. Searching for the classifier's own signal
guaranteed every result would look actionable whatever its subject; it returned both tourism
contests.

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

Measured with `tools/llm_bench.py` against the 26 fixtures in `tests/fixtures/events.json`, of
which 17 are thin enough to reach a model at all. Each model is warmed before it is timed:

| model | s/doc | usable | category | document kind |
| --- | --- | --- | --- | --- |
| deterministic, no LLM | 0.00 | 15/26 | 15/26 | 15/26 |
| `Gemma-SEA-LION-v3-9B-IT` (default) | 9.47 | 15/26 | 15/26 | **15/26** |
| `qwen3:8b` | 9.4 | 15/26 | 15/26 | 14/26 |
| `dolphin3:8b` | 11.1 | 15/26 | 15/26 | 13/26 |

SEA-LION is the default because it is the only candidate that matches deterministic extraction on
every column rather than regressing one, and because it is trained for Southeast Asian languages
including Filipino — which these English fixtures cannot show, but the Taglish group posts need.

Benchmarking also found a real defect. `merge_extraction` filled `category`, `location` and
`eligibility` from the model *without* the evidence check every other contributed field gets, so
`dolphin3:8b` promoted both the webinar and the job advertisement from `UNKNOWN` to
`OTHER_COMPETITION` on nothing but its own say-so. Category feeds the verifier's `competition` gate
and the scorer's +15 for a preferred category, so that is a false alert in the making. With those
three fields held to the same evidence requirement as the rest, dolphin3's category score went from
13/26 back to 15/26 — the regression was entirely the missing guard.

Read that table with its bias in mind: these fixtures were written for the deterministic
extractor, so they favour it, and they contain none of the awkward real-world pages the LLM
exists to rescue. What they do show is that no model here reads `document_kind` reliably, and that
a 27B model is not buying accuracy for its cost.

Rebuild the table for your own hosts with `tools/llm_bench.py`, which warms each model before
timing it and prints the deterministic baseline alongside — a model that cannot beat that baseline
on a column is not earning its place there.

```powershell
$env:PYTHONPATH='src'
python tools/llm_bench.py --host http://your-host:11434
```

### Two hosts, and when the second is asked

`OLLAMA_BASE_URL` is asked for every extraction that needs one. `OLLAMA_ESCALATION_URL`, if set, is
asked **only** when the first host left the merged confidence below `llm_escalation_confidence`
(0.70, one notch under the 0.75 that summons a model at all) or left a title, date or category
unresolved — and at most `llm_escalations_per_run` times. A clean page never reaches it.

That shape exists because of what the two machines actually are. A dedicated 8GB box keeps one
model resident and answers with 0.3s of load; a shared 24GB box holds whatever its other users
last asked for, and reloading a model on it was measured at 5.8s, 16.1s and **39.9s**. So the small
host is the everyday one and the big one is worth its latency only when the small one came up
short. 8GB caps the model at about 9B: `qwen2.5:14b` loads but spills to CPU, keeping 5.8GB of
9.3GB in VRAM.

Escalation cannot make an extraction worse. The second pass goes through the same
`merge_extraction` as the first, so it may only fill fields still empty and may only *downgrade* a
document kind.

Failover is the same mechanism. A refused connection moves straight to the next host, which is what
happens when the everyday host is a laptop and the laptop is asleep. The connect timeout is 5
seconds and separate from the 180-second read timeout — a cold model load legitimately takes tens of
seconds, but an absent host should not take three minutes to notice — and a refused connection is
not retried, because retrying doubles the wait for an answer that is not coming. If no host answers
at all, extraction simply stays deterministic.

The dashboard shows both hosts, whether each is reachable, which model each currently has loaded,
and a **Make primary** button that reorders the ladder at runtime — so the machines can be swapped
without editing `.env` and restarting.

`LLM_PROVIDER=disabled` is a supported configuration and makes runs dramatically faster. Discovery
still works: the ImaGnation page, for instance, extracts at 0.83 confidence deterministically and
never calls the model. Disabling it costs recall only on pages the regexes cannot read, which are
rejected as `LOW_CONFIDENCE` instead. Start with it disabled if you want fast cycles, and turn it
on if `LOW_CONFIDENCE` dominates your rejection counts. OpenAI remains an optional
fallback and is used only when `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and `OPENAI_MODEL` are all
configured. Source text is treated as untrusted data, typed output is required, and claimed
evidence must occur in the fetched source.

### What an alert looks like

Every fact used to be a full-width field, so nine rows stacked into a single column and
most of them read "Not specified". The layout now is:

- the **organizer** as the embed's author line, with its logo — taken from an optional
  `logo:` on the organizer in `config/sources.yaml`, else that site's own `/favicon.ico`;
- the **title**, linked to the official page only when that host is trusted;
- a two-to-three sentence **summary**;
- the page's own **banner** (`og:image`), which for a competition is usually its poster;
- a compact grid of short facts, three to a row — event date, registration deadline and
  location first, because those are what the reader is deciding on;
- longer fields (eligibility, why it matched, links, source) below, full width;
- relevance and confidence in the **footer**, where they no longer cost two field slots.

Dates use Discord's own timestamp markup, so each reader sees them in their own timezone
with a live countdown — "5 October 2026 (in 34 days)" — rather than Manila time baked into
the text. **A fact that is not known is omitted**, not printed as "Not specified".

Two trust rules keep the presentation honest, and they are the same ones the links already
follow. An **image is a link the reader cannot inspect before it renders**, so a banner is
shown only when its host would be trusted for a link — a Facebook post's `fbcdn` image is
refused, which means social alerts carry no picture. A **logo** is only fetched from a host
that already clears the authority gate, so an arbitrary scraped domain cannot put its
artwork in the channel. Both verdicts are decided where the sources config is available and
carried on the payload, exactly as `official_url_clickable` is, so the reconciliation path
cannot re-render to a different conclusion.

Scraped text is still markdown-escaped; text this renderer generates is not, which is what
makes the timestamp markup work.

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

The software path can be zero-cost: SQLite, deterministic parsing, a self-hosted Ollama server, a
private SearXNG instance, and the dashboard are all self-hosted. This excludes
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
- Akaton's dashboard and scheduler on your Tailscale address, port 8765;
- an existing Ollama model over Tailscale, port 11434.

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

### Correcting records by hand

Every extraction is a guess, and some of them are wrong. Events, leads and candidates can each be
corrected from the dashboard through a modal form — a modal rather than inline fields, so a mis-click
cannot write.

**An edit is pinned.** This is the part that matters. A refresh re-reads the source page every 24
hours, so an unpinned correction is silently undone within a day and the operator would fix the same
field over and over without ever seeing why. A corrected field is recorded in `events.manual_overrides`
and re-applied over every later extraction; the form shows a **pinned** badge with an ✕ to release
that field back to the page. Only fields you actually change are pinned.

Edits go through the same versioning path as an automatic update, so the history stays complete and
the new version is marked `manual`. Material changes are recorded but never alert — you already know
what you just typed.

**Events are archived, not deleted.** Seven tables carry a foreign key to `events.id` with no
cascade, and `notifications` is the record of what was actually delivered. Archiving hides the event,
stops `RefreshJob` re-reading it, and makes it unable to alert again; `show archived` finds it and
Restore brings it back. Leads are genuinely deleted — a lead is a work item, not a record of
delivery.

Leads can also be renamed, which **re-keys** them, so the cooldown follows the corrected spelling
rather than the misspelling that was extracted; and **Search now** clears the cooldown so the next
discovery run spends a request on it. Candidates can be retried, which puts the page back through the
pipeline — useful right after a classifier rule changes.

### Stopping a running job

A backdate over Facebook and Reddit runs for minutes in a headed browser, so discovery, refresh and
backfill each get a Stop control that appears only while that job is running. Cancellation runs the
collectors' `finally` blocks, so Chrome closes with it. A cancelled job reports `CANCELLED`; before
this it reported `RUNNING` forever, because `CancelledError` derives from `BaseException` and the
`except Exception` around the job never saw it — which also meant the single-flight guard would never
let another run start.

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
