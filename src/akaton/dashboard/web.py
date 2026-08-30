# ruff: noqa: E501
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

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
    async def candidates(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
        async with database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(CandidateRow).order_by(CandidateRow.updated_at.desc()).limit(limit)
                    )
                ).all()
            )
        return [_candidate(row) for row in rows]

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
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Akaton Monitor</title><style>
:root{color-scheme:dark;--bg:#08110f;--panel:#10201b;--line:#254238;--muted:#91aa9f;
--text:#eef8f2;--green:#51d88a;--amber:#f4bc62;--red:#ff7a7a;--blue:#69b7ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#17392d 0,
var(--bg) 38%);font:14px/1.5 Inter,ui-sans-serif,system-ui;color:var(--text)}
main{max-width:1400px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;
align-items:flex-start;gap:20px}.eyebrow{color:var(--green);text-transform:uppercase;letter-spacing:.16em;
font-weight:700;font-size:11px}h1{font-size:34px;margin:4px 0}h2{font-size:16px;margin:0 0 14px}
.muted{color:var(--muted)}.controls{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
button,input{border:1px solid var(--line);background:#132820;color:var(--text);border-radius:9px;
padding:9px 12px}button{cursor:pointer;font-weight:650}button:hover{border-color:var(--green)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.card,.panel{
background:color-mix(in srgb,var(--panel) 93%,transparent);border:1px solid var(--line);border-radius:14px;
box-shadow:0 14px 40px #0003}.card{padding:16px}.metric{font-size:29px;font-weight:760;margin-top:5px}
.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:14px}.panel{padding:17px;min-width:0}
.wide{grid-column:1/-1}.row{display:grid;grid-template-columns:minmax(220px,2fr) 1fr 1fr 1fr;
gap:12px;align-items:center;padding:11px 5px;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.title{font-weight:680;overflow:hidden;text-overflow:ellipsis}
.pill{display:inline-block;padding:3px 8px;border-radius:999px;background:#1d352d;color:#c7e9d7;
font-size:11px}.bad{color:var(--red)}.good{color:var(--green)}a{color:var(--blue);text-decoration:none}
.states{display:flex;gap:7px;flex-wrap:wrap}.state{border:1px solid var(--line);padding:6px 8px;
border-radius:8px}.toast{position:fixed;right:22px;bottom:22px;background:#183429;border:1px solid
var(--green);padding:11px 15px;border-radius:10px;display:none}@media(max-width:850px){.cards{grid-template-columns:
repeat(2,1fr)}.grid{grid-template-columns:1fr}.row{grid-template-columns:1fr 1fr}.top{display:block}
.controls{justify-content:flex-start;margin-top:15px}} </style></head><body><main>
<div class="top"><div><div class="eyebrow">Private tailnet console</div><h1>Akaton Monitor</h1>
<div class="muted" id="subtitle">Loading monitor state…</div></div><div class="controls">
<input id="token" type="password" placeholder="Dashboard token (optional)">
<button onclick="action('discover')">Run discovery</button><button onclick="action('refresh')">Refresh events</button>
<button onclick="scheduler('start')">Start monitor</button><button onclick="scheduler('pause')">Pause</button></div></div>
<section class="cards"><div class="card"><div class="muted">Candidates seen</div><div class="metric" id="candidates">—</div></div>
<div class="card"><div class="muted">Verified events</div><div class="metric" id="events">—</div></div>
<div class="card"><div class="muted">Notifications</div><div class="metric" id="notifications">—</div></div>
<div class="card"><div class="muted">Scheduler</div><div class="metric" id="scheduler">—</div></div></section>
<section class="grid"><div class="panel"><h2>Pipeline states</h2><div class="states" id="states"></div></div>
<div class="panel"><h2>Last search</h2><div id="last-search" class="muted">No searches yet.</div></div>
<div class="panel wide"><h2>Recent accepted events</h2><div id="event-list"></div></div>
<div class="panel wide"><h2>What the monitor is seeing</h2><div id="candidate-list"></div></div></section>
</main><div class="toast" id="toast"></div><script>
const token=document.getElementById('token');token.value=localStorage.getItem('akaton-token')||'';
token.onchange=()=>{localStorage.setItem('akaton-token',token.value);load()};
function headers(){return token.value?{'X-Akaton-Token':token.value}:{}}
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;
if(cls)n.className=cls;return n}function show(msg){const t=document.getElementById('toast');t.textContent=msg;
t.style.display='block';setTimeout(()=>t.style.display='none',3000)}
async function api(path,options={}){options.headers={...headers(),...(options.headers||{})};const r=await fetch(path,options);
if(!r.ok)throw new Error(r.status===401?'Dashboard token required':`HTTP ${r.status}`);return r.json()}
async function action(name){try{const d=await api(`/api/actions/${name}`,{method:'POST'});show(d.message);setTimeout(load,500)}catch(e){show(e.message)}}
async function scheduler(action){try{const d=await api(`/api/actions/scheduler/${action}`,{method:'POST'});show(`Scheduler ${d.state}`);load()}catch(e){show(e.message)}}
function renderRows(target,rows,type){const box=document.getElementById(target);box.replaceChildren();if(!rows.length){box.append(el('div','Nothing recorded yet.','muted'));return}
for(const x of rows){const row=el('div',undefined,'row');const first=el('div');const link=el('a',x.title||x.url||`Candidate ${x.id}`,'title');link.href=x.canonical_url||x.url||'#';link.target='_blank';link.rel='noreferrer';first.append(link);
if(type==='candidate'&&x.rejection_reasons.length)first.append(el('div',x.rejection_reasons.join(', '),'bad'));row.append(first);
row.append(el('div',type==='event'?x.category:x.provider,'muted'));row.append(el('div',type==='event'?`${x.score} score`:x.state,'pill'));
row.append(el('div',type==='event'?(x.deadline?new Date(x.deadline).toLocaleDateString():'No deadline'):(x.last_trace?.state||'—'),'muted'));box.append(row)}}
async function load(){try{const [s,e,c]=await Promise.all([api('/api/status'),api('/api/events'),api('/api/candidates')]);
for(const k of ['candidates','events','notifications'])document.getElementById(k).textContent=s.counts[k];
document.getElementById('scheduler').textContent=s.monitor.scheduler;document.getElementById('subtitle').textContent=`${s.configuration.search_provider} search · ${s.configuration.llm_provider} extraction · ${s.configuration.timezone}`;
const states=document.getElementById('states');states.replaceChildren();for(const [k,v] of Object.entries(s.candidate_states))states.append(el('span',`${k} ${v}`,'state'));
const ls=s.last_search;document.getElementById('last-search').textContent=ls?`${ls.status} · ${ls.provider} · ${ls.result_count} results · ${ls.query}`:'No searches yet.';
renderRows('event-list',e,'event');renderRows('candidate-list',c,'candidate')}catch(e){show(e.message)}}load();setInterval(load,5000);
</script></body></html>"""
