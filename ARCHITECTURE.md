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
Python · `anthropic` SDK · `python-telegram-bot` · stdlib `sqlite3` · `pydantic`.

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
- Roles: `researcher`, `summarizer`, `coder`, and the special `pm`.
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

## 6. Suggested module layout

```
starling/
  __init__.py
  __main__.py            # wiring + entrypoint
  config.py              # env: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, tick interval
  blackboard.py          # SQLite store + TaskStatus enum
  schemas.py             # Pydantic: Classification, PlannedTask, ProjectPlan
  orchestrator.py        # classify, route, handle human replies
  scheduler.py           # heartbeat + ready-task dispatch
  channels/
    base.py              # Channel interface
    telegram.py          # Telegram adapter
  agents/
    roles.py             # role -> system prompt (+ model)
    worker.py            # run_task(role, description, inputs) -> str
    pm.py                # decompose(goal) -> ProjectPlan
```

---

## 7. Out of scope for v1 (note in README, don't build)
- Discord/other channels (interface is ready for them).
- Model-per-role mixing.
- Multi-node / concurrent scheduler workers.
- Auth / multi-tenant (single-user assistant by design).
