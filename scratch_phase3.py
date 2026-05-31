"""Scratch verification for Phase 3 (ephemeral mode: classify -> fan-out -> merge).

Runs the orchestrator end-to-end with a fake Channel and a fake OpenAI-compatible
client, so no API key or Telegram token is needed. Covers: schema validation,
multi-worker parallel fan-out + merge, single-worker short-circuit, unknown-worker
filtering, and the Pydantic guard rejecting a bad classification.
"""

import asyncio
import time

from pydantic import ValidationError

from scratch_fakes import (
    FakeChannel,
    chat,
    system_of,
    text_response,
    tool_name,
    tool_response,
    user_of,
)
from starling.blackboard import Blackboard
from starling.orchestrator import Orchestrator
from starling.schemas import Classification, Mode, PlannedTask, ProjectPlan


class FakeClient:
    def __init__(self, classification: dict, worker_delay: float = 0.0) -> None:
        self.chat = chat(self._create)
        self._classification = classification
        self._worker_delay = worker_delay
        self.calls: list[str] = []

    async def _create(self, **kw):
        if tool_name(kw) == "classify_request":
            self.calls.append("classify")
            return tool_response("classify_request", self._classification)
        if system_of(kw).startswith("You merge"):
            self.calls.append("merge")
            return text_response("MERGED::" + user_of(kw))
        self.calls.append("worker")
        if self._worker_delay:
            await asyncio.sleep(self._worker_delay)
        return text_response("draft for: " + user_of(kw))


def make_orch(channel: FakeChannel, client: FakeClient) -> Orchestrator:
    # Ephemeral-mode tests don't touch the blackboard; an in-memory one satisfies the
    # constructor. Project-mode decomposition is verified in scratch_phase4.py.
    return Orchestrator(channel, client, Blackboard(":memory:"))


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


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
