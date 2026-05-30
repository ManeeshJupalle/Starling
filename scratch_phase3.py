"""Scratch verification for Phase 3 (ephemeral mode: classify -> fan-out -> merge).

Runs the orchestrator end-to-end with a fake Channel and a fake Anthropic client, so
no API key or Telegram token is needed. Covers: schema validation, multi-worker
parallel fan-out + merge, single-worker short-circuit, unknown-worker filtering,
project-mode placeholder, and the Pydantic guard rejecting a bad classification.

The live check ("summarize SQLite vs Postgres" via Telegram) is run separately with
real ANTHROPIC_API_KEY + TELEGRAM_BOT_TOKEN via ``python -m starling``.
"""

import asyncio
import time

from pydantic import ValidationError

from starling.blackboard import Blackboard
from starling.channels.base import Channel, InboundHandler
from starling.orchestrator import Orchestrator
from starling.schemas import Classification, Mode, PlannedTask, ProjectPlan


def make_orch(channel: "FakeChannel", client: "FakeClient") -> Orchestrator:
    # Ephemeral-mode tests don't touch the blackboard; an in-memory one satisfies the
    # constructor. Project-mode decomposition is verified in scratch_phase4.py.
    return Orchestrator(channel, client, Blackboard(":memory:"))


# --- fakes ----------------------------------------------------------------

class FakeChannel(Channel):
    def __init__(self) -> None:
        self._handler: InboundHandler | None = None
        self.sent: list[tuple[int, str]] = []

    def on_message(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def send(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def run(self) -> None:
        raise NotImplementedError


class _Block:
    def __init__(self, type: str, text: str | None = None, input: dict | None = None) -> None:
        self.type = type
        self.text = text
        self.input = input


class _Resp:
    def __init__(self, content: list[_Block]) -> None:
        self.content = content


class _Messages:
    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    async def create(self, **kw):
        return await self._client._create(**kw)


class FakeClient:
    """Mimics AsyncAnthropic: classify -> tool_use; merge/worker -> text."""

    def __init__(self, classification: dict, worker_delay: float = 0.0) -> None:
        self.messages = _Messages(self)
        self._classification = classification
        self._worker_delay = worker_delay
        self.calls: list[str] = []

    async def _create(self, **kw):
        if kw.get("tools"):
            self.calls.append("classify")
            return _Resp([_Block("tool_use", input=self._classification)])
        system = kw.get("system", "")
        if system.startswith("You merge"):
            self.calls.append("merge")
            user = kw["messages"][0]["content"]
            return _Resp([_Block("text", text="MERGED::" + user)])
        # worker call — echo the role implied by the system prompt back as the draft
        self.calls.append("worker")
        if self._worker_delay:
            await asyncio.sleep(self._worker_delay)
        return _Resp([_Block("text", text="draft for: " + kw["messages"][0]["content"])])


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


# --- checks ---------------------------------------------------------------

def test_schemas() -> None:
    print("schemas:")
    c = Classification.model_validate(
        {"mode": "ephemeral", "goal": "x", "workers": ["researcher", "summarizer"]}
    )
    check("Classification parses + coerces mode to enum", c.mode is Mode.EPHEMERAL)
    check("workers preserved", c.workers == ["researcher", "summarizer"])

    plan = ProjectPlan.model_validate(
        {"tasks": [
            {"role": "researcher", "description": "a", "depends_on": []},
            {"role": "summarizer", "description": "b", "depends_on": [0]},
        ]}
    )
    check("ProjectPlan parses tasks", len(plan.tasks) == 2)
    check("PlannedTask depends_on indices", isinstance(plan.tasks[1], PlannedTask)
          and plan.tasks[1].depends_on == [0])

    raised = False
    try:
        Classification.model_validate({"mode": "banana", "goal": "x", "workers": []})
    except ValidationError:
        raised = True
    check("invalid mode rejected by Pydantic", raised)


async def test_multi_worker_parallel() -> None:
    print("\nephemeral, multiple workers (parallel fan-out + merge):")
    fake = FakeClient(
        {"mode": "ephemeral", "goal": "compare SQLite vs Postgres",
         "workers": ["researcher", "summarizer"]},
        worker_delay=0.2,
    )
    channel = FakeChannel()
    orch = make_orch(channel, fake)

    start = time.perf_counter()
    await orch.handle_message(99, "sqlite vs postgres?")
    elapsed = time.perf_counter() - start

    check("exactly one reply sent", len(channel.sent) == 1)
    check("reply is the merged synthesis", channel.sent[0][1].startswith("MERGED::"))
    check("classified first", fake.calls[0] == "classify")
    check("two workers dispatched", fake.calls.count("worker") == 2)
    check("merge ran last", fake.calls[-1] == "merge")
    # 2 workers x 0.2s sequential = 0.4s; parallel should land near 0.2s.
    print(f"    elapsed={elapsed:.3f}s (sequential would be ~0.40s)")
    check("workers ran concurrently (elapsed < 0.35s)", elapsed < 0.35)


async def test_single_worker_short_circuit() -> None:
    print("\nephemeral, single worker (no merge call):")
    fake = FakeClient({"mode": "ephemeral", "goal": "g", "workers": ["summarizer"]})
    channel = FakeChannel()
    await make_orch(channel, fake).handle_message(1, "hi")
    check("one reply sent", len(channel.sent) == 1)
    check("no merge call for a single worker", "merge" not in fake.calls)
    check("reply is the worker draft", channel.sent[0][1].startswith("draft for:"))


async def test_unknown_worker_filtered() -> None:
    print("\nephemeral, unknown worker filtered out:")
    fake = FakeClient({"mode": "ephemeral", "goal": "g", "workers": ["researcher", "banana"]})
    channel = FakeChannel()
    await make_orch(channel, fake).handle_message(1, "hi")
    check("only the known worker dispatched", fake.calls.count("worker") == 1)


async def test_bad_classification_is_caught() -> None:
    print("\nbad classification is gated by Pydantic (graceful error):")
    fake = FakeClient({"mode": "banana", "goal": "g", "workers": []})
    channel = FakeChannel()
    await make_orch(channel, fake).handle_message(1, "hi")
    check("no workers ran on invalid classification", "worker" not in fake.calls)
    check("user got a graceful error reply", channel.sent[0][1].startswith("Sorry"))


async def main() -> None:
    test_schemas()
    await test_multi_worker_parallel()
    await test_single_worker_short_circuit()
    await test_unknown_worker_filtered()
    await test_bad_classification_is_caught()
    print("\nALL PASS: ephemeral classify -> fan-out -> merge verified offline.")


if __name__ == "__main__":
    asyncio.run(main())
