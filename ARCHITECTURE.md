# Starling — Architecture

A channel-native **multi-agent orchestrator**. You message a Telegram bot; Starling
classifies the request as *ephemeral* (one-shot) or *project* (long-running), routes
it to the right specialized agents, runs them, and reports back — interrupting you
only when it needs a human decision.

> The name: a lone starling is unremarkable, but a *murmuration* of them moves as a
> single coordinated mind. That's the pitch — where single-assistant tools give you
> one agent, Starling coordinates a flock.

---

## 1. Design thesis

**One orchestration brain, two lifecycles.**

- **Ephemeral** — fan out to relevant workers in parallel, merge results, reply, done.
  No persistence beyond the reply.
- **Project** — a PM agent decomposes the goal into a task graph persisted in a
  durable store; the work advances across restarts and days, driven by both inbound
  events and a heartbeat scheduler.

**The wedge.** Personal-AI-agent tools (e.g. OpenClaw) are explicitly *single-user,
single-assistant*. Starling's contribution is the **coordination layer**: routing,
handoff, shared state, lifecycle, and human-in-the-loop. That's a distributed-systems
problem single-assistant tools don't address — and it's the portfolio signal.

**Stack discipline.** No agent framework (no LangChain/CrewAI/AutoGen). The
orchestration loop is hand-rolled on purpose — that's the thing being demonstrated.
Python · `openai` SDK pointed at any OpenAI-compatible provider (OpenRouter / Groq /
OpenAI) · `python-telegram-bot` · stdlib `sqlite3` · `pydantic` · `mcp` (Model Context
Protocol, for tools — see §8). MCP is a *tool* protocol, not an orchestration layer, so
the loop stays hand-rolled.

---

## 2. Component map

```
 Telegram
    │  inbound message
    ▼
┌─────────────────┐   events    ┌──────────────────┐
│ Channel Adapter │────────────▶│   Orchestrator   │
│ (Telegram first)│◀────────────│  (classify+route)│
└─────────────────┘  outbound   └──────────────────┘
    ▲  replies / pings              │            │
    │                     ephemeral │            │ project
    │                       fan-out │            │ PM decomposes goal
    │                               ▼            ▼
    │                        ┌──────────────────────────┐
    │                        │     Blackboard (SQLite)   │
    │      milestone /        │  projects, tasks, status, │
    │      decision pings     │  deps, inputs, outputs    │
    │                        └──────────────────────────┘
    │                               ▲            ▲
    │                               │ read ready │ write results
    │                        ┌──────────────────────────┐
    └────────────────────────│        Scheduler          │
       (via orchestrator)    │  heartbeat + event-poked  │
                             └──────────────────────────┘
                                        │ dispatch
                                        ▼
                             ┌──────────────────────────┐
                             │       Worker pool         │
                             │ researcher · summarizer · │
                             │ coder · pm  (role prompts)│
                             └──────────────────────────┘
```

### 2.1 Channel Adapter
- Single responsibility: inbound messages → internal events; internal updates →
  channel messages (threaded replies).
- **No orchestration logic.** Pluggable backend so Discord can be added later by
  implementing the same interface.
- Telegram first (simplest bot API, fastest demo).

### 2.2 Orchestrator
- The brain. On each inbound message:
  1. Check the blackboard: is there a task `awaiting_human` for this chat? If so, the
     message is a **reply to that task**, not a new request — route it there.
  2. Otherwise, one **structured LLM call** → `Classification {mode, goal, workers}`.
  3. Ephemeral → dispatch workers now, merge, reply.
  4. Project → create a project row, ask the PM agent to decompose, write tasks to
     the blackboard, let the scheduler take over.
- All LLM control-flow output is validated through Pydantic before being acted on.

### 2.3 Blackboard (SQLite)
- Durable shared state and the reason project-mode survives a crash.
- The scheduler reads **desired state** from here rather than holding work in memory.
- Tables: `projects`, `tasks`. Tasks carry status, role, description, `depends_on`,
  inputs, output, and an optional `question` (for human-in-the-loop).
- SQLite chosen deliberately: single file, survives restarts, zero ops, defensible
  in an interview as "durable single-node state."

### 2.4 Scheduler
- **Hybrid drive:** a heartbeat loop wakes every N seconds AND inbound events poke it
  directly. Events give responsiveness; the heartbeat is the safety net so nothing
  stalls silently.
- Each tick: find **ready** tasks (all deps `done`, not yet running), dispatch them,
  write results back, promote newly-unblocked tasks.
- Because it reads from the blackboard, a restart resumes in-flight projects.

### 2.5 Worker pool
- Same model, different system prompts per role. (Model-per-role is a one-line config
  swap — documented but not built in v1.)
- Roles: `researcher`, `summarizer`, `coder`, `operator` (acts on your email/calendar/
  Slack, §9.1), and the special `pm`.
- Workers are **stateless**: prompt built from role + task inputs, model called,
  output returned. All durable state lives in the blackboard.

---

## 3. Task lifecycle

```
pending ──▶ ready ──▶ running ──▶ done
   │                     │
   │                     ├──▶ failed
   │                     │
   │                     └──▶ awaiting_human ──(human reply)──▶ ready/running
   │
   └─ promoted to ready once all depends_on tasks are done
```

- **`awaiting_human`** is the differentiator. A task that needs a decision pauses and
  stores its `question`; the orchestrator routes the user's *next* reply in that chat
  back to this task instead of treating it as a new request.

---

## 4. The two genuinely hard parts (scope with eyes open)

1. **Task decomposition & dependency tracking.** Getting the PM agent to emit a sane,
   acyclic task graph is the real intellectual work. Constrain hard: fixed worker
   roles, max task count (~12), explicit dependency declarations as indices.

2. **Human-in-the-loop interrupts.** Pausing a task, asking in the channel, and
   resuming on reply — with the orchestrator routing that reply to the *right* paused
   task — is where hobby versions fall apart. Nailing it is the strong differentiator.

---

## 5. Data shapes (reference)

**Classification** (orchestrator's first call):
`{ mode: "ephemeral"|"project", goal: str, workers: [str] }`

**ProjectPlan** (PM decomposition):
`{ tasks: [ { role: str, description: str, depends_on: [int] } ] }`
where `depends_on` indexes into the same list (resolved to DB ids on insert).

**Task row:** `id, project_id?, role, description, status, depends_on(json),
inputs(json), output(json?), question?, created_at, updated_at`

---

## 6. Module layout (as built)

```
starling/
  __init__.py
  __main__.py            # wiring + entrypoint
  config.py              # env: LLM_* (OpenAI-compatible), TELEGRAM_BOT_TOKEN, TICK_INTERVAL, DASHBOARD_PORT
  llm.py                 # OpenAI-compatible client factory + response helpers (§1 stack)
  blackboard.py          # SQLite store + TaskStatus enum; projects, tasks, memories, triggers (§9.2)
  schemas.py             # Pydantic: Classification (mode/schedule/watch), ProjectPlan, Verdict
  orchestrator.py        # classify, route, human replies + approvals, triggers, morning brief (§9)
  scheduler.py           # heartbeat + ready-task dispatch; pause for approval; fire triggers; critic (§9)
  memory.py              # recall user context for prompt injection (§8)
  usage.py               # token + rough cost tracking (§8)
  channels/
    base.py              # Channel interface
    telegram.py          # Telegram adapter
    web.py               # live web dashboard (aiohttp + SSE) (§8)
  agents/
    roles.py             # role -> system prompt; per-role MCP tool grants; pm_model() (§9.4)
    worker.py            # run_task / resume_task — agentic tool loop, pauses for approval (§8)
    pm.py                # decompose(goal) -> ProjectPlan; morning_brief_plan() (§9.3)
    critic.py            # critique(goal, draft) -> Verdict — verify step (§9.4)
  tools/                 # §8 — the tool layer
    base.py              # Tool, ToolRegistry, read-only safety check
    mcp.py               # MCPManager: connect MCP servers, wrap their tools
    builtin.py           # a built-in tool
mcp_servers.json         # which MCP servers to launch (gitignored if it holds tokens)
```

---

## 7. Out of scope for v1 (note in README, don't build)
- Discord/other channels (interface is ready for them).
- Model-per-role mixing.
- Multi-node / concurrent scheduler workers.
- Auth / multi-tenant (single-user assistant by design).

---

## 8. Evolution: tool-using agents (Starling-Claw)

v1 (§1–§7) coordinates agents that only emit **text**. *Starling-Claw* — **built, in
phases A–F** — keeps the entire coordination engine and upgrades the **workers** from
text generators into **tool-using agents that act**: read your files, repos, and the
web (and write to them, behind approval) via the **Model Context Protocol (MCP)**, with
per-user memory, a live web dashboard, and cost tracking.

The phased build lives in **`Starling-claw-prompts.md`**; this section is the design.
The engine in §1–§6 was evolved in place — nothing was replaced; the worker grew a loop
and a tool layer was added. The sub-sections below are all implemented.

**Thesis shift:** from *coordinated text* to *coordinated action*. The coordination
layer (orchestrator, blackboard, scheduler, PM decomposition, human-in-the-loop) is
unchanged and reused.

### 8.1 Tool layer
A `Tool` is `{name, description, JSON-schema, async call(args) -> str}`. A
`ToolRegistry` exposes the tools a given role may use as OpenAI function definitions
and routes calls by name.

### 8.2 MCP integration
Rather than hand-code each integration, an `MCPManager` connects to configured MCP
servers (filesystem, GitHub, Google, Slack, …) over stdio, discovers their tools, and
wraps each as a `Tool` (namespaced `server__tool`; MCP `inputSchema` → tool schema).
One client, many pre-built integrations. Servers are declared in `mcp_servers.json`
(Claude-Desktop format). The manager owns the long-lived sessions for the app's
lifetime (via `contextlib.AsyncExitStack`).

### 8.3 Worker = agentic loop
`run_task` becomes a loop: offer the role's tools, let the model call them, execute,
feed results back, repeat until a final answer (iteration-capped). Read-only tools
auto-run — that's the first milestone.

### 8.4 Per-role tools
`roles.py` maps each role to the servers/tools it may use, so a researcher gets read
tools, an "operator" gets calendar/file actions, etc.

### 8.5 Human-in-the-loop reused as the approval layer
When an agent wants a **sensitive** action (write/send/delete), the task checkpoints
its in-flight loop state to the blackboard, goes `awaiting_human`, and asks for
approval — the exact pause/ask/route primitive from §3, now guarding real-world side
effects. Workers stay stateless *between* tasks; a paused task carries its loop state.

### 8.6 Module additions
```
starling/tools/base.py   # Tool, ToolRegistry
starling/tools/mcp.py    # MCPManager: connect servers, discover + route tool calls
mcp_servers.json         # which MCP servers to launch (Claude-Desktop format; gitignore if it holds tokens)
```
Plus: `worker.py` grows the tool loop; `roles.py` gains per-role tool sets; the
scheduler passes each task its role's tools; requirements add `mcp` (and Node.js is
needed for the common `npx`-launched reference servers).

### 8.7 Safety (non-negotiable once agents act)
Read tools expose personal data to the model provider — expected for a personal agent,
but know it happens. Write/destructive tools are approval-gated (§8.5); code/shell
tools must be sandboxed. Start read-only; add writes only behind the approval flow.

The read/write classifier (`is_read_only`) is a name heuristic: a tool is auto-run only
if its operation starts with a read verb **and** no name token is a write verb (so
`get_or_create_label` is correctly gated). Genuine reads whose names dodge the rule
(e.g. `slack_list_channels`) are opted in per server via `read_only_tools` in
`mcp_servers.json`. Default-deny throughout: when in doubt, it asks.

---

## 9. Proactive layer & self-checking (Starling-Claw, phases G–I)

§1–§8 react to your messages. This layer lets the flock **act on your real accounts**,
**act on its own**, and **check its own work** — without changing the coordination core.

### 9.1 Real integrations + the `operator` role
A new `operator` worker role is granted the Gmail / Google Calendar / Slack MCP servers
and is the agent that acts on your accounts. The classifier routes account-touching
requests to it. Reads auto-run; sends/creates/deletes flow through the §8.5 approval
gate unchanged — the safety story was already built, so integrations are mostly config
(`mcp_servers.json` + `SETUP_INTEGRATIONS.md`). Servers ship disabled until credentials
are set, and a missing one is skipped, never fatal.

### 9.2 Triggers — scheduled and event-driven
A `triggers` table makes the scheduler **start** projects, not just drive them. Each
heartbeat, `_fire_due_triggers` runs any trigger whose `next_run` has arrived:

- **schedule** (`recurrence` `once`/`daily`) — fires its goal at a clock time; daily
  re-arms (skipping missed days), one-shots retire. The classifier extracts a
  `ScheduleSpec {recurrence, at}` from "every morning brief me"-style messages.
- **watch** — polls a read tool (Gmail search) on an interval and fires only when the
  result **changes** vs a stored `cursor` (the first poll just baselines, so an existing
  inbox isn't treated as new); the changed content is passed to the goal as context.

A fired trigger calls `orchestrator.run_goal`, which classifies and runs the goal,
delivering the result to the chat **unprompted**. Trigger firing is advanced/disabled
*before* the goal runs, so a slow run can't double-fire.

### 9.3 Morning Brief — the multi-agent showcase
`morning_brief_plan()` is a hand-built project (not PM-decomposed, for determinism):
`operator`(calendar) ‖ `operator`(email) ‖ `researcher`(news) run **in parallel**, a
`summarizer` merges them into one digest. It's the thing a single assistant does slowly
and serially that the flock does at once — and a daily schedule (§9.2) makes it arrive
on its own each morning.

### 9.4 Planning quality + the critic (phase I)
Two reliability levers around the "task decomposition is hard" problem (§4):

- **`PM_MODEL`** — the PM's single planning call can use a stronger model than the
  workers (one call per project, negligible cost), plus a prompt rule that a task using
  another's output must depend on it. Makes plans usually-correct.
- **Critic** — before a project's result is delivered, `critique(goal, draft)` checks it
  against the goal and either approves, returns a corrected version (using only facts in
  the draft — never fabricating), or flags an unfixable concern. Best-effort: a critic
  error never blocks delivery. This catches the residual bad plan/output the stronger
  planner misses.

### 9.5 Module additions
```
starling/agents/critic.py   # critique(goal, draft) -> Verdict
blackboard.py: triggers table + add_trigger/add_watch/due_triggers/...
scheduler.py:  _fire_due_triggers / _fire_watch / _review (critic)
orchestrator.py: schedule/watch creation, run_goal, start_morning_brief
schemas.py: ScheduleSpec, WatchSpec, Verdict; Classification.{schedule,watch}
SETUP_INTEGRATIONS.md       # Gmail/Calendar/Slack OAuth + token setup
```
