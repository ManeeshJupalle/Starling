# Starling

A channel-native **multi-agent orchestrator**. You message a Telegram bot;
Starling classifies the request as *ephemeral* (one-shot) or *project*
(long-running), routes it to the right specialized agents, runs them — in parallel
where it can — and reports back, interrupting you only when it genuinely needs a
human decision.

> The name: a lone starling is unremarkable, but a *murmuration* of them moves as
> a single coordinated mind. Where single-assistant tools give you one agent,
> Starling coordinates a flock.

It's built **without an agent framework** (no LangChain / CrewAI / AutoGen). The
orchestration loop — routing, task decomposition, dependency scheduling, shared
state, crash recovery, human-in-the-loop — is hand-rolled on purpose. That
coordination layer *is* the project.

And the agents now **act**: they use tools over the [Model Context Protocol
(MCP)](https://modelcontextprotocol.io) to read your files, repos, the web, and your
**Gmail / Calendar / Slack** — and to *write*, but only after you approve. It also runs
**proactively** — scheduled jobs and an inbox watcher start projects on their own and
message you unprompted (the flagship being a parallel-fan-out **Morning Brief**) — and a
**critic agent** checks each deliverable before it reaches you. Starling remembers your
preferences, tracks token cost, and ships a live web dashboard so you can watch the flock
work. See [Tool-using agents](#tool-using-agents-starling-claw).

---

## Table of contents

- [What it does](#what-it-does)
- [See it in action](#see-it-in-action)
- [Tool-using agents (Starling-Claw)](#tool-using-agents-starling-claw)
- [Architecture](#architecture)
- [How a message is handled](#how-a-message-is-handled)
- [Task lifecycle](#task-lifecycle)
- [Project structure](#project-structure)
- [Data shapes](#data-shapes)
- [Setup](#setup)
- [Running it](#running-it)
- [Verifying it works (no API key needed)](#verifying-it-works-no-api-key-needed)
- [Design decisions](#design-decisions)
- [How it was built (phases)](#how-it-was-built-phases)
- [Out of scope / future work](#out-of-scope--future-work)
- [Tech stack](#tech-stack)

---

## What it does

**One orchestration brain, two lifecycles.**

- **Ephemeral** — a one-shot question. Starling fans out to the relevant worker
  agents *in parallel*, merges their drafts into one coherent answer, replies, and
  forgets it. No persistence beyond the reply.

- **Project** — a multi-step goal. A **PM agent** decomposes it into a task graph
  (with dependencies), which is persisted to a durable SQLite **blackboard**. A
  **scheduler** then drives the graph to completion across ticks — and across
  restarts — running tasks as their dependencies finish and posting the final
  result back to your chat.

**Human-in-the-loop.** The PM can insert a *decision point*: a task whose job is to
ask **you** a question. The scheduler pauses there, asks in the channel, and the
orchestrator routes your *next* reply back to that waiting task — not as a new
request — then the project resumes. This is the part hobby versions usually get
wrong, and it's the strongest differentiator.

---

## See it in action

**Ephemeral — fan out, merge, reply:**

```
You:      summarize the pros and cons of SQLite vs Postgres for a single-user app
Starling: SQLite is the better default for a single-user app: zero-ops, single-file,
          and no server to run... Postgres earns its keep once you need concurrent
          writers, rich types, or network access... [one merged answer]
```

Behind the scenes: the request is classified as `ephemeral`, dispatched to the
`researcher` and `summarizer` workers concurrently, and their drafts are synthesized
into a single reply.

**Project — decompose, schedule, complete:**

```
You:      research the top 3 Python task-queue libraries and write a short comparison
Starling: Project #1 started - 4 tasks queued.
          ... (3 researcher tasks run in parallel, then a summarizer that depends on them)
Starling: Project #1 complete:

          Celery vs RQ vs Dramatiq — a short comparison
          ... [the synthesized comparison]
```

**Human-in-the-loop — pause for a decision, then resume:**

```
You:      plan a weekend itinerary, but ask me which city first
Starling: Project #1 started - 3 tasks queued.
Starling: Which city do you want to visit - Paris or Rome?
You:      Paris
Starling: Got it - continuing.
Starling: Project #1 complete:

          Your Paris weekend itinerary
          ... [itinerary that used your answer]
```

Your `"Paris"` reply is routed to the paused task, stored as its output, and fed to
the downstream tasks that depend on it.

---

## Tool-using agents (Starling-Claw)

Beyond coordinating *text*, Starling's workers coordinate *action*. A worker runs an
**agentic tool loop** — the model calls tools, they execute, results feed back, until
it answers — and tools come from **MCP servers**, so dozens of pre-built integrations
plug in through config alone.

| Capability | How |
|---|---|
| **Read your stuff** | MCP servers (filesystem, GitHub, web search, **Gmail, Calendar, Slack**) exposed per worker role; a dedicated `operator` agent acts on your accounts |
| **Take actions — safely** | writes/sends (send email, create event, post Slack) are *sensitive*: the worker **pauses and asks you to approve**, reusing the same human-in-the-loop primitive; reads auto-run |
| **Work proactively** | **scheduled triggers** ("every morning brief me") and an **inbox watcher** start projects on their own and message you unprompted — including the **Morning Brief**, a parallel fan-out (calendar ‖ email ‖ news → one digest) |
| **Check its own work** | a **critic agent** verifies each project's deliverable against the goal before delivery — approving, correcting (without fabricating), or flagging |
| **Remember you** | durable preferences captured from your messages and recalled into context on later, unrelated requests |
| **Watch it work** | a live web dashboard streaming the task graph as it executes (SSE) |
| **Stay reliable & cheap** | per-tool timeouts, retries for reads (never blind-retrying a write), a stronger model for planning (`PM_MODEL`), and visible token/cost tracking |

**Safety model.** Tools are read-only by default (a name allowlist; a whole server can
be trusted with `"read_only": true`). Anything that changes state is gated behind your
approval — and the approval pause **checkpoints the worker's state to the blackboard**,
so it survives a restart like everything else.

**Adding an integration is config, not code** — drop an MCP server into
`mcp_servers.json` and grant it to a role. The same approval gate covers it
automatically. Gmail / Calendar / Slack setup (OAuth + tokens) is documented in
[SETUP_INTEGRATIONS.md](SETUP_INTEGRATIONS.md). See [ARCHITECTURE.md §8–§9](ARCHITECTURE.md)
for the tool-using and proactive designs.

---

## Architecture

```
 Telegram
    │  inbound message
    ▼
┌──────────────────┐   (chat_id, text)   ┌────────────────────────┐
│  Channel adapter │────────────────────▶│      Orchestrator      │
│  (Telegram; the  │◀────────────────────│  classify + route +    │
│  Channel iface   │   replies / pings   │  route human replies   │
│  is pluggable)   │                     └───────────┬────────────┘
└──────────────────┘                  ephemeral │    │ project
    ▲                                   fan-out  │    │ PM decomposes the goal
    │                                            ▼    ▼
    │                         ┌──────────────────────────────────────┐
    │      milestone /        │          Blackboard (SQLite)         │
    │      decision pings     │  projects · tasks · status · deps ·  │
    │                         │  inputs · outputs · questions        │
    │                         └──────────────┬───────────────────────┘
    │                              reads ready│   ▲ writes results
    │                              tasks      ▼   │ promotes unblocked
    │                         ┌──────────────────────────────────────┐
    └─────────────────────────│              Scheduler               │
       (posts results /       │  heartbeat (every TICK_INTERVAL) +   │
        asks questions)       │  poke(); resumes after a crash       │
                              └──────────────┬───────────────────────┘
                                             │ dispatch (stateless)
                                             ▼
                              ┌──────────────────────────────────────┐
                              │             Worker pool              │
                              │  researcher · summarizer · coder ·   │
                              │  operator · pm (role system prompts) │
                              └──────────────────────────────────────┘
```

A **proactive layer** sits beside this: the scheduler also fires **triggers**
(time-scheduled jobs and an inbox watcher) that start projects on their own and deliver
to your chat unprompted, and a **critic** verifies each deliverable before it's sent.
See [ARCHITECTURE.md §9](ARCHITECTURE.md).

**Channel adapter** ([starling/channels/](starling/channels/)) — turns inbound
platform messages into `(chat_id, text)` calls and sends outbound text. It holds
**zero** orchestration logic, so a new backend (Discord, Slack, …) is just another
implementation of the `Channel` interface. Telegram is the first backend.

**Orchestrator** ([starling/orchestrator.py](starling/orchestrator.py)) — the brain.
For each message it (1) checks the blackboard for a task awaiting a human reply in
this chat and routes the message there if one exists; otherwise (2) makes **one
structured, Pydantic-validated LLM call** to classify the request, then either runs
it ephemerally (fan-out + merge) or starts a project (decompose + persist).

**Blackboard** ([starling/blackboard.py](starling/blackboard.py)) — durable shared
state in a single SQLite file. It's the reason project mode survives a crash: the
scheduler reads *desired state* from here rather than holding work in memory. Two
tables, `projects` and `tasks`; tasks carry status, role, description, dependencies,
inputs, output, and an optional `question`.

**Scheduler** ([starling/scheduler.py](starling/scheduler.py)) — hybrid-driven: a
heartbeat wakes it every `TICK_INTERVAL` seconds **and** the orchestrator can
`poke()` it the instant new work appears (responsiveness + a safety net). Each tick
it runs the ready tasks concurrently, feeds each task its upstream outputs, stores
results, lets newly-unblocked tasks promote, and posts terminal results to the chat.

**Worker pool** ([starling/agents/](starling/agents/)) — the same model with a
different **system prompt per role** (`researcher`, `summarizer`, `coder`, `operator`,
`pm`). The `operator` acts on your accounts (email/calendar/Slack). Workers are
**stateless**: prompt in, text out, all durable state lives in the blackboard.
Project planning can use a stronger model via `PM_MODEL` (it's the hardest single
judgement); model-per-role is otherwise a one-line swap in
[roles.py](starling/agents/roles.py).

---

## How a message is handled

The orchestrator's decision tree for every inbound message:

```
inbound (chat_id, text)
        │
        ▼
 is a task in this chat awaiting_human?
        │
   yes ─┤── store text as that task's answer → mark it done → poke scheduler → ack
        │
   no  ─┤── classify (one structured LLM call, validated by Pydantic)
        │
        ├── mode = ephemeral → run chosen workers in parallel → merge → reply
        │
        └── mode = project   → PM decomposes goal → persist task graph → poke scheduler
                                                                          → "Project #N started"
```

The two genuinely hard parts — and where this implementation focuses:

1. **Task decomposition & dependency tracking.** The PM emits an acyclic task graph.
   Constraints are enforced hard: a fixed role set, a max task count (12), and
   `depends_on` given as indices into the task list. The plan is validated and
   *repaired* (drop out-of-range / self dependencies) or *rejected* (unknown role,
   too many tasks, or a cycle). Tasks are inserted in topological order so index
   dependencies resolve cleanly to blackboard ids.

2. **Human-in-the-loop interrupts.** A `pm`-role task is a question. The scheduler
   parks it as `awaiting_human`, stores the question, and asks it in the channel. The
   orchestrator routes the user's next reply in that chat to the *right* waiting task,
   marks it done with the answer, and the project resumes — the answer flows to
   downstream tasks as an input.

---

## Task lifecycle

```
pending ──▶ ready ──▶ running ──▶ done
   │                     │
   │                     ├──▶ failed
   │                     │
   │  (a 'pm' question task)
   └──▶ ready ──▶ awaiting_human ──(human reply)──▶ done ──▶ unblocks dependents
```

- A task with no dependencies starts **ready**; one with dependencies starts
  **pending** and is **promoted** to ready once all its dependencies are `done`.
- The scheduler marks a task **running**, executes it, then **done** (storing output)
  or **failed**.
- A `pm` question task goes **awaiting_human** until a reply arrives.
- **Crash resume:** on startup the scheduler requeues any task left `running`
  (interrupted mid-flight) back to `ready`. `done` tasks are never redone, so a
  restart picks up exactly where it left off. `awaiting_human` tasks survive restarts
  too, so a pending question is still answerable after a reboot.

---

## Project structure

```
starling/
  __main__.py        # entrypoint: wires channel + client + blackboard + scheduler + orchestrator + tools + dashboard
  config.py          # env config: LLM_*, TELEGRAM_BOT_TOKEN, TICK_INTERVAL, DASHBOARD_PORT
  llm.py             # OpenAI-compatible client factory + response helpers
  blackboard.py      # SQLite store: projects, tasks (status/deps/checkpoint), memories, triggers
  schemas.py         # Pydantic: Classification (mode/schedule/watch), ProjectPlan, Verdict
  orchestrator.py    # classify, route, fan-out/merge, projects, human replies + approvals, triggers, morning brief
  scheduler.py       # heartbeat + poke; ready-task dispatch, resume, reporting; fires triggers; runs the critic
  memory.py          # recall user context for prompt injection
  usage.py           # token + rough cost tracking
  channels/
    base.py          # Channel interface (on_message, send, run + on_start hook)
    telegram.py      # Telegram adapter (python-telegram-bot, v20+ async)
    web.py           # live web dashboard (aiohttp + SSE)
  agents/
    roles.py         # role -> system prompt + per-role MCP tool grants (incl. operator); PM_MODEL
    worker.py        # run_task / resume_task — agentic tool loop, pauses for approval
    pm.py            # decompose(goal) -> ProjectPlan; morning_brief_plan() template
    critic.py        # critique(goal, draft) -> Verdict — verify step before delivery
  tools/
    base.py          # Tool + ToolRegistry + read-only safety (write-verb guard, per-tool override)
    mcp.py           # MCPManager: connect MCP servers, wrap their tools
    builtin.py       # a tiny built-in tool

scratch_phase1.py … scratch_phase6.py     # offline verification of the orchestrator
scratch_claw_a1.py … scratch_claw_i2.py   # offline verification of tools, integrations, proactive layer, critic
mcp_servers.example.json                   # MCP server config template (filesystem/github/web/gmail/calendar/slack)
SETUP_INTEGRATIONS.md                      # Gmail / Calendar / Slack OAuth + token setup
ARCHITECTURE.md                            # the design source of truth (§8 tools, §9 proactive + critic)
```

The interesting code to read first: [orchestrator.py](starling/orchestrator.py)
(routing + the reply/approval check), [scheduler.py](starling/scheduler.py) (the
dependency-driven execution loop), [agents/worker.py](starling/agents/worker.py) (the
pause/resume tool loop), and [tools/mcp.py](starling/tools/mcp.py) (the MCP client).

---

## Data shapes

Every LLM control-flow decision is parsed through a Pydantic model
([schemas.py](starling/schemas.py)) **before** the orchestrator acts on it — the
guardrail between free-form model output and the execution loop.

**Classification** — the orchestrator's first call on a new request (the optional
fields capture a durable preference, a schedule, or an inbox watch when present):

```python
{ "mode": "ephemeral" | "project", "goal": str, "workers": [str],
  "memory": str?, "schedule": {recurrence, at}?, "watch": {query, every_minutes}? }
```

**ProjectPlan** — the PM's decomposition (`depends_on` indexes into the same list,
resolved to blackboard ids on insert):

```python
{ "tasks": [ { "role": str, "description": str, "depends_on": [int] } ] }
```

**Task row** (in the blackboard):

```
id · project_id? · role · description · status · depends_on(json) ·
inputs(json) · output(json?) · question? · created_at · updated_at
```

---

## Setup

Requires **Python 3.11+**.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Unix:     source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in your keys
```

Environment variables (see [.env.example](.env.example)):

| Variable             | Purpose                                    |
| -------------------- | ------------------------------------------ |
| `LLM_API_KEY`        | API key for your provider (OpenRouter / Groq / OpenAI) |
| `LLM_BASE_URL`       | Provider base URL (defaults to OpenRouter; Groq/OpenAI presets in `.env.example`) |
| `LLM_MODEL`          | Model id, e.g. `openai/gpt-4o-mini`        |
| `PM_MODEL`           | *Optional* — stronger model for project planning only (falls back to `LLM_MODEL`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `TICK_INTERVAL`      | Scheduler heartbeat in seconds (default 5) |

Get a bot token by messaging **@BotFather** on Telegram (`/newbot`). To connect Gmail /
Calendar / Slack, follow [SETUP_INTEGRATIONS.md](SETUP_INTEGRATIONS.md) — those servers
ship disabled and are skipped until you set them up.

---

## Running it

```bash
python -m starling
```

Then message your bot on Telegram. Try one of each mode:

- *"summarize the pros and cons of SQLite vs Postgres for a single-user app"* → an
  ephemeral fan-out + merged reply.
- *"research the top 3 Python task-queue libraries and write a short comparison"* → a
  project that runs to completion and posts the result.
- *"plan a weekend itinerary, but ask me which city first"* → a project that pauses,
  asks, and resumes on your reply.
- *"every morning at 8am give me a brief"* → schedules a daily **Morning Brief** that
  fans out (calendar ‖ email ‖ news) and messages you the digest unprompted.
- *"watch my inbox for new unread emails and summarize them"* → an inbox watcher that
  reacts when something new arrives. (Both need Gmail/Calendar connected — see
  [SETUP_INTEGRATIONS.md](SETUP_INTEGRATIONS.md).)

Open **http://localhost:8000** to watch the live dashboard — tasks flip through
ready → running → done in real time, a task turns purple when it pauses for your
approval, and the header shows running token cost.

To give agents tools, copy `mcp_servers.example.json` to `mcp_servers.json` (gitignored)
and fill in keys: the **filesystem** server (scoped to `workspace/`) needs none; **GitHub**
needs a token; **web search** needs a free Brave key. Then ask the researcher to *"read
the README in my workspace"* or, with a write tool configured, watch it pause for approval.

State persists in `starling.db` (a local SQLite file, gitignored). Kill the process
mid-project and relaunch — it resumes from the blackboard without redoing completed
tasks. Run `python -m starling --reset` to wipe the blackboard for a clean run (stop
any running instance first).

---

## Verifying it works (no API key needed)

Each phase ships a self-contained `scratch_phaseN.py` script that exercises the real
code against **fakes** (a fake channel and a fake OpenAI-compatible client) plus a real
SQLite blackboard — so the full logic is verifiable offline, with no API key or
Telegram token:

```bash
python scratch_phase1.py   # channel adapter contract (register -> deliver -> send)
python scratch_phase2.py   # blackboard: dependency promotion + cross-process persistence
python scratch_phase3.py   # ephemeral: classify -> parallel fan-out -> merge (incl. a real timing check)
python scratch_phase4.py   # project: PM decomposition, graph repair/reject, persistence
python scratch_phase5.py   # scheduler: run-to-completion, inputs, crash recovery, resume
python scratch_phase6.py   # human-in-the-loop: pause, ask, route reply, resume
```

These double as living documentation of each subsystem's contract.

---

## Design decisions

**No agent framework.** The orchestration loop is hand-rolled. Routing, decomposition,
dependency scheduling, and human-in-the-loop are explicit code you can read — not
hidden behind a framework abstraction. Demonstrating that coordination layer is the
whole point.

**Validate every LLM control-flow output.** Classification and decomposition both go
through Pydantic models before anything acts on them. A malformed model response
can't drive execution — it's rejected at the boundary, and the bot replies
gracefully instead of crashing.

**SQLite blackboard as the single source of truth.** Durable, single-file, zero-ops,
and the reason projects survive restarts. The scheduler reads desired state from the
blackboard rather than holding work in memory, which makes crash recovery almost
free.

**Stateless workers.** A worker is prompt-in / text-out with no durable state, so the
same code scales from one model to model-per-role with a one-line config change, and
nothing is lost on restart.

**Parallel fan-out.** Independent work runs concurrently via `asyncio.gather` — both
the ephemeral workers and a project's ready tasks within a tick.

**Hybrid scheduler drive.** A heartbeat guarantees progress even if an event is
missed; a `poke()` from the orchestrator gives instant responsiveness when new work
arrives. Belt and suspenders.

**Pluggable channel.** The `Channel` interface is the only thing that knows about
Telegram. Everything above it is transport-agnostic.

---

## How it was built (phases)

Built incrementally; each phase is self-contained, verified, and committed on its
own. All six are complete.

| Phase | Deliverable                                                  | Status |
| ----- | ------------------------------------------------------------ | :----: |
| 0     | Project setup — skeleton, config, tooling                    |   ✅   |
| 1     | Channel adapter + echo loop (Telegram)                       |   ✅   |
| 2     | Blackboard (SQLite durable state)                            |   ✅   |
| 3     | Ephemeral mode — classify → parallel fan-out → merge         |   ✅   |
| 4     | Project mode — PM goal decomposition into a task graph       |   ✅   |
| 5     | Scheduler — heartbeat, dependency resolution, crash resume   |   ✅   |
| 6     | Human-in-the-loop — pause, ask in channel, resume on reply   |   ✅   |

Then **Starling-Claw** evolved the workers from *text* into *action*:

| Phase  | Deliverable                                                  | Status |
| ------ | ------------------------------------------------------------ | :----: |
| A1–A4  | Agentic tool loop + MCP servers (filesystem, GitHub, web), read-only |   ✅   |
| B      | Sensitive actions gated by human approval (checkpoint + resume) |   ✅   |
| C      | Per-user memory — capture preferences, recall into context   |   ✅   |
| D      | Web tools + per-server `read_only` override                  |   ✅   |
| E      | Live web dashboard (SSE task-graph view)                     |   ✅   |
| F      | Reliability — tool retries/timeouts + token-cost tracking    |   ✅   |

Then **G·H·I** turned it into a proactive, integrated personal agent:

| Phase | Deliverable                                                         | Status |
| ----- | ------------------------------------------------------------------- | :----: |
| G1–G2 | Real integrations — Gmail / Calendar / Slack + `operator` agent; writes behind approval; safety-heuristic tuning |   ✅   |
| H1    | Proactive scheduled triggers + unprompted Telegram delivery         |   ✅   |
| H2    | Inbox watcher — an event trigger that reacts to new mail            |   ✅   |
| H3    | **Morning Brief** — parallel fan-out (calendar ‖ email ‖ news) → digest |   ✅   |
| I1    | Stronger planner (`PM_MODEL`) + data-dependency planning fix        |   ✅   |
| I2    | Critic / verify step before every project deliverable               |   ✅   |

See [ARCHITECTURE.md](ARCHITECTURE.md) (§8 tool-using layer, §9 proactive + critic) for
the design, and [SETUP_INTEGRATIONS.md](SETUP_INTEGRATIONS.md) to connect your accounts.

---

## Out of scope / future work

The architecture is ready for these; they're deliberately not built, to keep the
focus sharp:

- **Web *chat* channel** — the dashboard is read-only today; a chat-from-web channel
  is a small follow-up (it needs per-message reply routing).
- **Discord / other channels** — implement the same `Channel` interface.
- **Model-per-role mixing** — a one-line swap (see the commented hook in
  [roles.py](starling/agents/roles.py)).
- **Embedding-based memory recall** — memory recall is recency-based today.
- **Multi-node / concurrent scheduler workers** — single-node by design.
- **Auth / multi-tenant** — single-user assistant by design.

---

## Tech stack

- **Python 3.11+**
- **[openai](https://pypi.org/project/openai/)** (async) — the model client, pointed at
  any OpenAI-compatible provider (OpenRouter / Groq / OpenAI) via `base_url`; used with
  function-calling for structured, validated control-flow output
- **[python-telegram-bot](https://python-telegram-bot.org/)** (v20+, async) — the
  Telegram channel
- **stdlib `sqlite3`** — the durable blackboard
- **[pydantic](https://docs.pydantic.dev/)** — validation of every LLM control-flow output
- **[mcp](https://pypi.org/project/mcp/)** — Model Context Protocol client, for tool servers
- **[aiohttp](https://pypi.org/project/aiohttp/)** — the live web dashboard (SSE)
- **[python-dotenv](https://pypi.org/project/python-dotenv/)** — loads secrets from `.env`

No agent framework — by design.
