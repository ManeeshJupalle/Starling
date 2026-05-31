"""Live web dashboard — watch the flock work.

A small aiohttp server that streams the blackboard's projects and tasks to a browser
in real time (Server-Sent Events), so you can see a project execute task-by-task. It's
read-only: you still chat via Telegram; this just visualises what the engine is doing.
See ARCHITECTURE.md §8 / Starling-claw-prompts Phase E.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from aiohttp import web

from ..blackboard import Blackboard


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    output = task.get("output")
    if isinstance(output, str) and len(output) > 280:
        output = output[:280] + "…"
    return {
        "id": task["id"],
        "role": task["role"],
        "description": task["description"],
        "status": task["status"],
        "depends_on": task["depends_on"],
        "question": task.get("question"),
        "output": output,
    }


class WebDashboard:
    """Serves a live, read-only view of the blackboard over HTTP + SSE."""

    def __init__(self, blackboard: Blackboard, host: str = "127.0.0.1", port: int = 8000) -> None:
        self._bb = blackboard
        self._host = host
        self._port = port
        self._runner: Optional[web.AppRunner] = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "projects": [
                {
                    "id": project["id"],
                    "goal": project["goal"],
                    "chat_id": project["chat_id"],
                    "tasks": [_task_view(t) for t in self._bb.project_tasks(project["id"])],
                }
                for project in self._bb.all_projects()
            ]
        }

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/state", self._state)
        app.router.add_get("/events", self._events)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        print(f"[web] dashboard at http://{self._host}:{self._port}")

    async def aclose(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self.snapshot())

    async def _events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await resp.prepare(request)
        try:
            while True:
                await resp.write(f"data: {json.dumps(self.snapshot())}\n\n".encode())
                await asyncio.sleep(1)
        except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
            pass
        return resp


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Starling — live</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background: #0b0e14; color: #e6e9ef; }
  header { display: flex; align-items: center; gap: 12px; padding: 18px 24px; border-bottom: 1px solid #1c2230; }
  header h1 { font-size: 18px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  header .sub { color: #8b93a7; font-size: 13px; }
  #conn { margin-left: auto; font-size: 12px; color: #8b93a7; display: flex; align-items: center; gap: 6px; }
  #dot { width: 8px; height: 8px; border-radius: 50%; background: #f59e0b; }
  #dot.on { background: #22c55e; }
  main { padding: 24px; display: grid; gap: 18px; max-width: 980px; margin: 0 auto; }
  .empty { color: #8b93a7; text-align: center; padding: 60px 0; }
  .project { background: #11151f; border: 1px solid #1c2230; border-radius: 12px; overflow: hidden; }
  .project > .head { padding: 14px 18px; border-bottom: 1px solid #1c2230; display: flex; gap: 10px; align-items: baseline; }
  .project .pid { color: #7c84f8; font-weight: 650; }
  .project .goal { font-weight: 550; }
  .tasks { display: grid; }
  .task { display: grid; grid-template-columns: 92px 1fr; gap: 12px; padding: 12px 18px; border-top: 1px solid #161b27; align-items: start; }
  .task:first-child { border-top: none; }
  .badge { font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: .4px;
           padding: 3px 8px; border-radius: 999px; text-align: center; align-self: start; }
  .badge.pending { background: #232a38; color: #8b93a7; }
  .badge.ready { background: #16314e; color: #5fa8ff; }
  .badge.running { background: #4a3410; color: #f7b955; animation: pulse 1.1s ease-in-out infinite; }
  .badge.awaiting_human { background: #3a2150; color: #c79bff; }
  .badge.done { background: #143524; color: #5cd68a; }
  .badge.failed { background: #41181b; color: #ff8a8a; }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .55 } }
  .task .role { color: #8b93a7; font-size: 12px; }
  .task .desc { margin-top: 2px; }
  .task .q { margin-top: 6px; color: #c79bff; font-size: 13px; }
  .task .out { margin-top: 6px; color: #9aa3b8; font-size: 13px; white-space: pre-wrap; }
</style>
</head>
<body>
<header>
  <h1>Starling</h1>
  <span class="sub">live task graph</span>
  <span id="conn"><span id="dot"></span><span id="connlabel">connecting…</span></span>
</header>
<main id="root"><div class="empty">No projects yet. Start one from Telegram and watch it run here.</div></main>
<script>
  const root = document.getElementById('root');
  const dot = document.getElementById('dot');
  const connlabel = document.getElementById('connlabel');
  const esc = (s) => (s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

  function render(state) {
    const projects = state.projects || [];
    if (!projects.length) {
      root.innerHTML = '<div class="empty">No projects yet. Start one from Telegram and watch it run here.</div>';
      return;
    }
    root.innerHTML = projects.map(p => `
      <div class="project">
        <div class="head"><span class="pid">#${p.id}</span><span class="goal">${esc(p.goal)}</span></div>
        <div class="tasks">
          ${p.tasks.map(t => `
            <div class="task">
              <span class="badge ${t.status}">${t.status.replace('_',' ')}</span>
              <div>
                <div class="role">${esc(t.role)} · #${t.id}${t.depends_on.length ? ' · needs ' + t.depends_on.map(d=>'#'+d).join(', ') : ''}</div>
                <div class="desc">${esc(t.description)}</div>
                ${t.question ? `<div class="q">❓ ${esc(t.question)}</div>` : ''}
                ${t.output ? `<div class="out">${esc(t.output)}</div>` : ''}
              </div>
            </div>`).join('')}
        </div>
      </div>`).join('');
  }

  const es = new EventSource('/events');
  es.onmessage = (e) => { dot.classList.add('on'); connlabel.textContent = 'live'; render(JSON.parse(e.data)); };
  es.onerror = () => { dot.classList.remove('on'); connlabel.textContent = 'reconnecting…'; };
</script>
</body>
</html>
"""
