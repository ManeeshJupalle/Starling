"""Phase I2 verification: the critic / verify step before delivery (offline).

A critic checks a project's deliverable against the goal before it's sent: approve as-is,
ship a corrected version, or attach a note when it can't fix it. The critic is best-effort
— if it errors or is absent, the draft ships unchanged. A fake client returns the verdicts.
"""

import asyncio

from scratch_fakes import FakeChannel, chat, tool_response
from starling.agents.critic import critique
from starling.blackboard import Blackboard
from starling.scheduler import Scheduler


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


class _Client:
    """Minimal AsyncOpenAI stand-in: exposes .chat.completions.create."""
    def __init__(self, create) -> None:
        self.chat = chat(create)


def verdict_client(verdict: dict) -> _Client:
    async def create(**kw):
        return tool_response("submit_verdict", verdict)
    return _Client(create)


def raising_client() -> _Client:
    async def create(**kw):
        raise RuntimeError("model down")
    return _Client(create)


def _scheduler(client):
    return Scheduler(Blackboard(":memory:"), FakeChannel(), client, 5)


async def test_critique_parses() -> None:
    print("critique() returns a validated Verdict:")
    client = verdict_client({"ok": False, "reason": "missing the news section", "revised": "fixed draft"})
    v = await critique("a morning brief", "draft", client=client)
    check("ok parsed", v.ok is False)
    check("reason parsed", v.reason == "missing the news section")
    check("revised parsed", v.revised == "fixed draft")


async def test_review_variants() -> None:
    print("\n_review applies the verdict:")
    approved = _scheduler(verdict_client({"ok": True, "reason": "", "revised": None}))
    check("approved -> draft unchanged", await approved._review("g", "the draft") == "the draft")

    revised = _scheduler(verdict_client({"ok": False, "reason": "incomplete", "revised": "the better draft"}))
    check("revised -> corrected text delivered", await revised._review("g", "the draft") == "the better draft")

    flagged = _scheduler(verdict_client({"ok": False, "reason": "wrong, can't fix", "revised": None}))
    out = await flagged._review("g", "the draft")
    check("unfixable -> draft kept with a note", out.startswith("the draft") and "wrong, can't fix" in out)


async def test_best_effort() -> None:
    print("\nthe critic never blocks delivery:")
    none_client = _scheduler(None)
    check("no client -> draft as-is", await none_client._review("g", "the draft") == "the draft")
    check("empty draft skipped", await none_client._review("g", "(no output)") == "(no output)")

    boom = _scheduler(raising_client())
    check("critic error -> draft as-is", await boom._review("g", "the draft") == "the draft")


async def main() -> None:
    await test_critique_parses()
    await test_review_variants()
    await test_best_effort()
    print("\nALL PASS: critic / verify step before delivery (Phase I2).")


if __name__ == "__main__":
    asyncio.run(main())
