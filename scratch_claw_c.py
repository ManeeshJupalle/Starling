"""Phase C verification: memory — a preference stated once is honored later.

Offline, with a fake LLM client: message 1 states a preference (the classifier extracts
it -> stored); a later, unrelated message 2 should have that preference injected into
the worker's context.
"""

import asyncio

from scratch_fakes import FakeChannel, chat, text_response, tool_name, tool_response
from starling.blackboard import Blackboard
from starling.orchestrator import Orchestrator


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


class MemoryClient:
    """Returns scripted classifications; records the messages each worker call receives."""

    def __init__(self, classifications) -> None:
        self.chat = chat(self._create)
        self._classifications = list(classifications)
        self.worker_messages: list[list] = []

    async def _create(self, **kw):
        if tool_name(kw) == "classify_request":
            return tool_response("classify_request", self._classifications.pop(0))
        self.worker_messages.append(kw["messages"])
        return text_response("draft")


async def main() -> None:
    bb = Blackboard(":memory:")
    channel = FakeChannel()
    client = MemoryClient([
        {"mode": "ephemeral", "goal": "acknowledge the preference", "workers": ["summarizer"],
         "memory": "User prefers concise, bulleted answers"},
        {"mode": "ephemeral", "goal": "explain how DNS works", "workers": ["summarizer"],
         "memory": None},
    ])
    orch = Orchestrator(channel, client, bb)
    chat_id = 7

    print("1. user states a preference -> it is captured:")
    await orch.handle_message(chat_id, "btw I prefer concise, bulleted answers")
    memories = bb.recall_memories(chat_id)
    check("preference captured as a memory", any("concise" in m["text"] for m in memories))
    check("only one memory stored", len(memories) == 1)

    print("\n2. a later, unrelated request -> the preference is injected into context:")
    await orch.handle_message(chat_id, "explain how DNS works")
    check("no new memory captured (message 2 has none)", len(bb.recall_memories(chat_id)) == 1)
    later_worker_msgs = client.worker_messages[-1]
    injected = any("concise" in (m.get("content") or "")
                   for m in later_worker_msgs if m["role"] == "system")
    check("preference injected into the later worker's context", injected)

    bb.close()
    print("\nALL PASS: memory captured once and honored in a later request (Phase C).")


if __name__ == "__main__":
    asyncio.run(main())
