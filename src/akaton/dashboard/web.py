# ruff: noqa: E501
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import String, func, select

from akaton.config import ConfigBundle
from akaton.dashboard.runtime import MonitorController
from akaton.persistence.database import Database
from akaton.persistence.models import (
    CandidateRow,
    EventRow,
    NotificationRow,
    SearchRunRow,
)


def create_dashboard(
    database: Database, controller: MonitorController, config: ConfigBundle
) -> FastAPI:
    app = FastAPI(title="Akaton Monitor", docs_url=None, redoc_url=None)

    async def authorize(x_akaton_token: Annotated[str | None, Header()] = None) -> None:
        expected = config.runtime.dashboard_token
        if expected and not (x_akaton_token and secrets.compare_digest(expected, x_akaton_token)):
            raise HTTPException(status_code=401, detail="Dashboard token required")

    secured = [Depends(authorize)]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return DASHBOARD_HTML

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status", dependencies=secured)
    async def status() -> dict:
        async with database.session() as session:
            candidate_count = int(await session.scalar(select(func.count(CandidateRow.id))) or 0)
            event_count = int(await session.scalar(select(func.count(EventRow.id))) or 0)
            notification_count = int(
                await session.scalar(select(func.count(NotificationRow.id))) or 0
            )
            states = dict(
                (
                    await session.execute(
                        select(CandidateRow.state, func.count(CandidateRow.id)).group_by(
                            CandidateRow.state
                        )
                    )
                ).all()
            )
            last_search = await session.scalar(
                select(SearchRunRow).order_by(SearchRunRow.started_at.desc()).limit(1)
            )
        return {
            "counts": {
                "candidates": candidate_count,
                "events": event_count,
                "notifications": notification_count,
            },
            "candidate_states": states,
            "last_search": _search_run(last_search),
            "monitor": controller.status(),
            "configuration": {
                "search_provider": config.runtime.search_provider,
                "llm_provider": config.runtime.llm_provider,
                "notifications_enabled": config.app.notifications_enabled,
                "timezone": config.app.timezone,
            },
        }

    @app.get("/api/events", dependencies=secured)
    async def events(limit: Annotated[int, Query(ge=1, le=100)] = 30) -> list[dict]:
        async with database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(EventRow).order_by(EventRow.updated_at.desc()).limit(limit)
                    )
                ).all()
            )
        return [_event(row) for row in rows]

    @app.get("/api/candidates", dependencies=secured)
    async def candidates(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        state: Annotated[str | None, Query()] = None,
        reason: Annotated[str | None, Query()] = None,
    ) -> list[dict]:
        query = select(CandidateRow).order_by(CandidateRow.updated_at.desc())
        if state:
            query = query.where(CandidateRow.state == state.upper())
        if reason:
            # rejection_reasons is a JSON array; match the quoted code inside its text.
            query = query.where(
                CandidateRow.rejection_reasons.cast(String).like(f'%"{reason.upper()}"%')
            )
        async with database.session() as session:
            rows = list((await session.scalars(query.limit(limit))).all())
        return [_candidate(row) for row in rows]

    @app.get("/api/rejections", dependencies=secured)
    async def rejections() -> dict:
        """Why candidates were dropped, so a silent pipeline can be explained."""
        async with database.session() as session:
            rows = list((await session.scalars(select(CandidateRow.rejection_reasons))).all())
        counts: dict[str, int] = {}
        for reasons in rows:
            for code in reasons or []:
                counts[code] = counts.get(code, 0) + 1
        return {
            "counts": dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)),
            "total": sum(counts.values()),
        }

    @app.get("/api/searches", dependencies=secured)
    async def searches(limit: Annotated[int, Query(ge=1, le=100)] = 25) -> list[dict]:
        """Recent search runs. A throttled backend shows up here as FAILED, not as silence."""
        async with database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SearchRunRow).order_by(SearchRunRow.started_at.desc()).limit(limit)
                    )
                ).all()
            )
        return [_search_run(row) for row in rows]

    @app.post("/api/actions/discover", status_code=202, dependencies=secured)
    async def discover() -> dict[str, object]:
        accepted = controller.trigger("discovery")
        return {"accepted": accepted, "message": _action_message(accepted, "discovery")}

    @app.post("/api/actions/refresh", status_code=202, dependencies=secured)
    async def refresh() -> dict[str, object]:
        accepted = controller.trigger("refresh")
        return {"accepted": accepted, "message": _action_message(accepted, "refresh")}

    @app.post("/api/actions/scheduler/start", dependencies=secured)
    async def scheduler_start() -> dict[str, object]:
        changed = controller.start_scheduler()
        return {"changed": changed, "state": controller.status()["scheduler"]}

    @app.post("/api/actions/scheduler/pause", dependencies=secured)
    async def scheduler_pause() -> dict[str, object]:
        changed = controller.pause_scheduler()
        return {"changed": changed, "state": controller.status()["scheduler"]}

    return app


def _event(row: EventRow) -> dict:
    facts = row.current_facts or {}
    return {
        "id": row.id,
        "title": row.title,
        "category": row.category,
        "organizer": row.organizer,
        "phase": row.event_phase,
        "registration": row.registration_state,
        "score": row.relevance_score,
        "confidence": row.confidence_score,
        "canonical_url": row.canonical_url,
        "registration_url": row.registration_url,
        "location": facts.get("location", {}),
        "deadline": facts.get("registration_deadline", {}).get("value"),
        "event_start": facts.get("event_start", {}).get("value"),
        "updated_at": row.updated_at.isoformat(),
    }


def _candidate(row: CandidateRow) -> dict:
    trace = row.trace or []
    return {
        "id": row.id,
        "title": row.title,
        "url": row.discovered_url,
        "provider": row.provider,
        "channel": row.discovery_channel,
        "state": row.state,
        "rejection_reasons": row.rejection_reasons or [],
        "event_id": row.event_id,
        "retry_at": row.retry_at.isoformat() if row.retry_at else None,
        "last_trace": trace[-1] if trace else None,
        "updated_at": row.updated_at.isoformat(),
    }


def _search_run(row: SearchRunRow | None) -> dict | None:
    if row is None:
        return None
    return {
        "provider": row.provider,
        "query_group": row.query_group,
        "query": row.query,
        "status": row.status,
        "result_count": row.result_count,
        "error": row.error,
        "started_at": row.started_at.isoformat(),
    }


def _action_message(accepted: bool, name: str) -> str:
    return f"{name.title()} started" if accepted else f"{name.title()} is already running"


DASHBOARD_HTML = """<!doctype html>
<html lang="en" class="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Akaton Monitor</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = {darkMode:'class', theme:{extend:{colors:{
  ink:'#08110f', panel:'#0f1e19', edge:'#224036', mint:'#51d88a', amber:'#f4bc62', rose:'#ff7a7a', sky:'#69b7ff'}}}}
</script>
</head>
<body class="bg-ink text-emerald-50 antialiased" style="background:radial-gradient(circle at 80% -10%, #17392d 0, #08110f 42%);min-height:100vh">
<main class="mx-auto max-w-[1500px] p-6 lg:p-8">

  <header class="flex flex-wrap items-start justify-between gap-5">
    <div>
      <p class="text-[11px] font-bold uppercase tracking-[0.18em] text-mint">Private tailnet console</p>
      <h1 class="mt-1 text-3xl font-bold lg:text-4xl">Akaton Monitor</h1>
      <p id="subtitle" class="mt-1 text-sm text-emerald-200/60">Loading monitor state…</p>
    </div>
    <div class="flex flex-wrap items-center justify-end gap-2">
      <input id="token" type="password" placeholder="Dashboard token (optional)"
        class="w-56 rounded-lg border border-edge bg-panel px-3 py-2 text-sm placeholder:text-emerald-200/30 focus:border-mint focus:outline-none">
      <button data-act="discover" class="rounded-lg border border-edge bg-panel px-3 py-2 text-sm font-semibold hover:border-mint">Run discovery</button>
      <button data-act="refresh" class="rounded-lg border border-edge bg-panel px-3 py-2 text-sm font-semibold hover:border-mint">Refresh events</button>
      <button data-sched="start" class="rounded-lg border border-edge bg-panel px-3 py-2 text-sm font-semibold hover:border-mint">Start monitor</button>
      <button data-sched="pause" class="rounded-lg border border-edge bg-panel px-3 py-2 text-sm font-semibold hover:border-mint">Pause</button>
    </div>
  </header>

  <section class="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-5">
    <div class="rounded-xl border border-edge bg-panel/90 p-4 shadow-lg shadow-black/20">
      <p class="text-xs text-emerald-200/60">Candidates seen</p><p id="k-candidates" class="mt-1 text-3xl font-extrabold">—</p></div>
    <div class="rounded-xl border border-edge bg-panel/90 p-4 shadow-lg shadow-black/20">
      <p class="text-xs text-emerald-200/60">Verified events</p><p id="k-events" class="mt-1 text-3xl font-extrabold text-mint">—</p></div>
    <div class="rounded-xl border border-edge bg-panel/90 p-4 shadow-lg shadow-black/20">
      <p class="text-xs text-emerald-200/60">Notifications</p><p id="k-notifications" class="mt-1 text-3xl font-extrabold">—</p></div>
    <div class="rounded-xl border border-edge bg-panel/90 p-4 shadow-lg shadow-black/20">
      <p class="text-xs text-emerald-200/60">Rejected</p><p id="k-rejected" class="mt-1 text-3xl font-extrabold text-rose">—</p></div>
    <div class="rounded-xl border border-edge bg-panel/90 p-4 shadow-lg shadow-black/20">
      <p class="text-xs text-emerald-200/60">Scheduler</p><p id="k-scheduler" class="mt-1 text-2xl font-extrabold">—</p>
      <p id="k-next" class="mt-1 text-[11px] text-emerald-200/50">—</p></div>
  </section>

  <section class="mt-4 grid gap-4 lg:grid-cols-2">
    <div class="rounded-xl border border-edge bg-panel/90 p-5">
      <h2 class="mb-3 text-base font-semibold">Search health</h2>
      <p class="mb-3 text-xs text-emerald-200/50">SearXNG scrapes upstream engines. A throttled backend appears here as FAILED rather than as an empty run.</p>
      <div id="searches" class="max-h-72 space-y-2 overflow-y-auto pr-1"></div>
    </div>
    <div class="rounded-xl border border-edge bg-panel/90 p-5">
      <h2 class="mb-3 text-base font-semibold">Pipeline states</h2>
      <div id="states" class="flex flex-wrap gap-2"></div>
      <h2 class="mb-2 mt-5 text-base font-semibold">Last run</h2>
      <div id="lastruns" class="space-y-1 text-xs text-emerald-200/70"></div>
    </div>
  </section>

  <section class="mt-4 rounded-xl border border-edge bg-panel/90 p-5">
    <h2 class="mb-3 text-base font-semibold">Accepted events</h2>
    <div class="overflow-x-auto"><table class="w-full min-w-[820px] text-sm">
      <thead><tr class="border-b border-edge text-left text-xs uppercase tracking-wide text-emerald-200/50">
        <th class="pb-2 pr-3">Competition</th><th class="pb-2 pr-3">Category</th><th class="pb-2 pr-3">Location</th>
        <th class="pb-2 pr-3">Deadline</th><th class="pb-2 pr-3">Reg.</th><th class="pb-2 text-right">Score</th></tr></thead>
      <tbody id="events"></tbody></table></div>
  </section>

  <section class="mt-4 rounded-xl border border-edge bg-panel/90 p-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold">Rejected scrapes</h2>
        <p class="mt-1 text-xs text-emerald-200/50">Everything the pipeline dropped, and why. Click a reason to filter.</p>
      </div>
      <button id="clear-filter" class="hidden rounded-lg border border-edge bg-panel px-3 py-1.5 text-xs font-semibold hover:border-mint">Clear filter</button>
    </div>
    <div id="reasons" class="mt-3 flex flex-wrap gap-2"></div>
    <div class="mt-4 overflow-x-auto"><table class="w-full min-w-[820px] text-sm">
      <thead><tr class="border-b border-edge text-left text-xs uppercase tracking-wide text-emerald-200/50">
        <th class="pb-2 pr-3">Page</th><th class="pb-2 pr-3">State</th><th class="pb-2 pr-3">Reasons</th><th class="pb-2">Last step</th></tr></thead>
      <tbody id="candidates"></tbody></table></div>
  </section>
</main>

<div id="toast" class="pointer-events-none fixed bottom-6 right-6 hidden rounded-lg border border-mint bg-emerald-900/90 px-4 py-2.5 text-sm shadow-xl"></div>

<script>
const $ = (id) => document.getElementById(id);
const token = $('token');
token.value = localStorage.getItem('akaton-token') || '';
token.onchange = () => { localStorage.setItem('akaton-token', token.value); load(); };
let filter = null;

const esc = (v) => (v === null || v === undefined) ? '' : String(v);
function headers() { return token.value ? {'X-Akaton-Token': token.value} : {}; }
async function api(path, options = {}) {
  options.headers = {...headers(), ...(options.headers || {})};
  const r = await fetch(path, options);
  if (!r.ok) throw new Error(r.status === 401 ? 'Dashboard token required' : 'HTTP ' + r.status);
  return r.json();
}
function toast(message) {
  const t = $('toast'); t.textContent = message; t.classList.remove('hidden');
  setTimeout(() => t.classList.add('hidden'), 3200);
}
function cell(text, cls) { const td = document.createElement('td'); td.className = 'py-2 pr-3 align-top ' + (cls || ''); td.textContent = esc(text); return td; }
function link(text, href) {
  const td = document.createElement('td'); td.className = 'py-2 pr-3 align-top';
  const a = document.createElement('a'); a.textContent = esc(text) || '(untitled)';
  a.href = href || '#'; a.target = '_blank'; a.rel = 'noreferrer';
  a.className = 'font-semibold text-sky hover:underline'; td.append(a); return td;
}
function chip(text, cls) { const s = document.createElement('span'); s.textContent = text; s.className = 'inline-block rounded-full px-2 py-0.5 text-[11px] ' + cls; return s; }

document.querySelectorAll('[data-act]').forEach((b) => b.onclick = async () => {
  try { const d = await api('/api/actions/' + b.dataset.act, {method: 'POST'}); toast(d.message); setTimeout(load, 600); }
  catch (e) { toast(e.message); }
});
document.querySelectorAll('[data-sched]').forEach((b) => b.onclick = async () => {
  try { const d = await api('/api/actions/scheduler/' + b.dataset.sched, {method: 'POST'}); toast('Scheduler ' + d.state); load(); }
  catch (e) { toast(e.message); }
});
$('clear-filter').onclick = () => { filter = null; load(); };

function renderSearches(rows) {
  const box = $('searches'); box.replaceChildren();
  if (!rows.length) { box.append(chip('No searches recorded yet', 'text-emerald-200/50')); return; }
  for (const s of rows) {
    const failed = s.status === 'FAILED';
    const row = document.createElement('div');
    row.className = 'rounded-lg border px-3 py-2 ' + (failed ? 'border-rose/40 bg-rose/5' : 'border-edge');
    const head = document.createElement('div');
    head.className = 'flex items-center justify-between gap-2';
    const q = document.createElement('span'); q.className = 'truncate text-xs'; q.textContent = s.query;
    head.append(q, chip(failed ? 'FAILED' : s.result_count + ' results',
      failed ? 'bg-rose/20 text-rose' : 'bg-emerald-500/15 text-mint'));
    row.append(head);
    if (s.error) { const e = document.createElement('p'); e.className = 'mt-1 text-[11px] leading-snug text-rose/80'; e.textContent = s.error; row.append(e); }
    box.append(row);
  }
}

function renderEvents(rows) {
  const body = $('events'); body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement('tr'); const td = cell('No accepted events yet.', 'text-emerald-200/50');
    td.colSpan = 6; tr.append(td); body.append(tr); return;
  }
  for (const e of rows) {
    const tr = document.createElement('tr'); tr.className = 'border-b border-edge/50';
    tr.append(link(e.title, e.canonical_url));
    tr.append(cell((e.category || '').replaceAll('_', ' '), 'text-emerald-200/70'));
    const loc = [e.location && e.location.city, e.location && e.location.region].filter(Boolean).join(' — ');
    tr.append(cell(loc || (e.location && e.location.location_type) || '—', 'text-emerald-200/70'));
    tr.append(cell(e.deadline ? new Date(e.deadline).toLocaleDateString() : '—', 'text-emerald-200/70'));
    const rt = document.createElement('td'); rt.className = 'py-2 pr-3 align-top';
    rt.append(chip(e.registration || 'UNKNOWN', e.registration === 'OPEN' ? 'bg-emerald-500/15 text-mint' : 'bg-white/5 text-emerald-200/60'));
    tr.append(rt);
    tr.append(cell(e.score, 'text-right font-bold'));
    body.append(tr);
  }
}

function renderReasons(counts) {
  const box = $('reasons'); box.replaceChildren();
  const entries = Object.entries(counts || {});
  if (!entries.length) { box.append(chip('Nothing rejected yet', 'bg-white/5 text-emerald-200/50')); return; }
  for (const [code, n] of entries) {
    const b = document.createElement('button');
    b.className = 'rounded-full border px-3 py-1 text-xs font-semibold transition ' +
      (filter === code ? 'border-mint bg-mint/15 text-mint' : 'border-edge bg-panel hover:border-mint');
    b.textContent = code.replaceAll('_', ' ') + ' · ' + n;
    b.onclick = () => { filter = (filter === code ? null : code); load(); };
    box.append(b);
  }
}

function renderCandidates(rows) {
  const body = $('candidates'); body.replaceChildren();
  $('clear-filter').classList.toggle('hidden', !filter);
  if (!rows.length) {
    const tr = document.createElement('tr'); const td = cell('Nothing recorded yet.', 'text-emerald-200/50');
    td.colSpan = 4; tr.append(td); body.append(tr); return;
  }
  for (const c of rows) {
    const tr = document.createElement('tr'); tr.className = 'border-b border-edge/50';
    tr.append(link(c.title || c.url, c.url));
    const st = document.createElement('td'); st.className = 'py-2 pr-3 align-top';
    st.append(chip(c.state, c.state === 'REJECTED' ? 'bg-rose/15 text-rose' : 'bg-white/5 text-emerald-200/70'));
    tr.append(st);
    tr.append(cell((c.rejection_reasons || []).join(', ').replaceAll('_', ' ') || '—', 'text-rose/80 text-xs'));
    const step = c.last_trace ? (c.last_trace.state + (c.last_trace.failure ? ' · ' + c.last_trace.failure : '')) : '—';
    tr.append(cell(step, 'text-xs text-emerald-200/50'));
    body.append(tr);
  }
}

async function load() {
  try {
    const candidateUrl = '/api/candidates?limit=60' + (filter ? '&reason=' + encodeURIComponent(filter) : '');
    const [s, ev, cand, rej, searches] = await Promise.all([
      api('/api/status'), api('/api/events'), api(candidateUrl), api('/api/rejections'), api('/api/searches')
    ]);
    $('k-candidates').textContent = s.counts.candidates;
    $('k-events').textContent = s.counts.events;
    $('k-notifications').textContent = s.counts.notifications;
    $('k-rejected').textContent = rej.total;
    $('k-scheduler').textContent = s.monitor.scheduler;
    const next = (s.monitor.jobs || []).map((j) => j.next_run_at).filter(Boolean).sort()[0];
    $('k-next').textContent = next ? 'next ' + new Date(next).toLocaleTimeString() : 'no job scheduled';
    $('subtitle').textContent = s.configuration.search_provider + ' search · ' + s.configuration.llm_provider +
      ' extraction · notifications ' + (s.configuration.notifications_enabled ? 'on' : 'off') + ' · ' + s.configuration.timezone;

    const st = $('states'); st.replaceChildren();
    for (const [k, v] of Object.entries(s.candidate_states)) st.append(chip(k.replaceAll('_', ' ') + ' · ' + v, 'border border-edge bg-panel text-emerald-200/80'));

    const lr = $('lastruns'); lr.replaceChildren();
    const runs = Object.entries(s.monitor.last_runs || {});
    if (!runs.length) lr.append(chip('No manual run yet', 'text-emerald-200/50'));
    for (const [name, r] of runs) {
      const p = document.createElement('p');
      p.textContent = name + ': ' + r.status + (r.result ? ' — ' + JSON.stringify(r.result) : '') + (r.error ? ' — ' + r.error : '');
      lr.append(p);
    }
    renderSearches(searches); renderEvents(ev); renderReasons(rej.counts); renderCandidates(cand);
  } catch (e) { toast(e.message); }
}
load(); setInterval(load, 8000);
</script>
</body></html>
"""
