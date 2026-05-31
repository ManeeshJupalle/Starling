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

---

## Table of contents

- [What it does](#what-it-does)
- [See it in action](#see-it-in-action)
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
                              │  pm (one model, role system prompts) │
                              └──────────────────────────────────────┘
```

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
different **system prompt per role**. Workers are **stateless**: prompt in, text out,
all durable state lives in the blackboard. (Model-per-role is a one-line swap,
documented in [roles.py](starling/agents/roles.py) but not enabled in v1.)

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
  __main__.py        # entrypoint: wires channel + client + blackboard + scheduler + orchestrator
  config.py          # env config: LLM_API_KEY/LLM_BASE_URL/LLM_MODEL, TELEGRAM_BOT_TOKEN, TICK_INTERVAL
  blackboard.py      # SQLite store + TaskStatus enum; projects/tasks tables and all state methods
  schemas.py         # Pydantic models: Mode, Classification, PlannedTask, ProjectPlan
  orchestrator.py    # classify, route, ephemeral fan-out/merge, start project, route human replies
  scheduler.py       # heartbeat + poke; ready-task dispatch, inputs, resume, terminal reporting, pause
  channels/
    base.py          # Channel interface (on_message, send, run + on_start hook) — zero orchestration
    telegram.py      # Telegram adapter (python-telegram-bot, v20+ async)
  agents/
    roles.py         # role -> system prompt, DEFAULT_MODEL, WORKER_ROLES / PLAN_ROLES
    worker.py        # run_task(role, description, inputs) -> str  (stateless model call)
    pm.py            # decompose(goal) -> ProjectPlan  (constrained, validated, acyclic)

scratch_phase1.py ... scratch_phase6.py   # offline verification scripts (no API key needed)
ARCHITECTURE.md                            # the design source of truth
```

The interesting code to read first: [orchestrator.py](starling/orchestrator.py)
(routing + the human-reply check), [scheduler.py](starling/scheduler.py) (the
dependency-driven execution loop), and [agents/pm.py](starling/agents/pm.py) (graph
decomposition, validation, and topological insertion).

---

## Data shapes

Every LLM control-flow decision is parsed through a Pydantic model
([schemas.py](starling/schemas.py)) **before** the orchestrator acts on it — the
guardrail between free-form model output and the execution loop.

**Classification** — the orchestrator's first call on a new request:

```python
{ "mode": "ephemeral" | "project", "goal": str, "workers": [str] }
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
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `TICK_INTERVAL`      | Scheduler heartbeat in seconds (default 5) |

Get a bot token by messaging **@BotFather** on Telegram (`/newbot`).

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

See [CLAUDE_CODE_PROMPTS.md](CLAUDE_CODE_PROMPTS.md) for the per-phase build spec and
[ARCHITECTURE.md](ARCHITECTURE.md) for the design source of truth.

---

## Out of scope / future work

The architecture is ready for these; they're deliberately not built in v1, to keep
the focus on the coordination layer:

- **Discord / other channels** — implement the same `Channel` interface.
- **Model-per-role mixing** — a one-line swap (see the commented hook in
  [roles.py](starling/agents/roles.py)).
- **Multi-node / concurrent scheduler workers** — v1 is single-node by design.
- **Auth / multi-tenant** — single-user assistant by design.
- **Failure recovery beyond requeue** — retries / backoff for failed tasks.

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
- **[python-dotenv](https://pypi.org/project/python-dotenv/)** — loads secrets from `.env`

No agent framework — by design.
