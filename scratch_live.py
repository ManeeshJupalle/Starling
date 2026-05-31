"""Live smoke test for the LLM integration (uses real OpenRouter credits).

Makes two small real calls — a plain worker completion and a tool-calling classify —
to confirm LLM_API_KEY / LLM_BASE_URL / LLM_MODEL are wired correctly, before any
Telegram setup. Reads .env directly so it does NOT require TELEGRAM_BOT_TOKEN.

    python scratch_live.py
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from scratch_fakes import FakeChannel
from starling.agents.roles import active_model
from starling.agents.worker import run_task
from starling.blackboard import Blackboard
from starling.llm import make_client
from starling.orchestrator import Orchestrator


async def main() -> None:
    print(f"base_url : {os.environ.get('LLM_BASE_URL')}")
    print(f"model    : {active_model()}")
    if not os.environ.get("LLM_API_KEY"):
        print("\nLLM_API_KEY is empty in .env — fill it in first.")
        return

    client = make_client(os.environ["LLM_API_KEY"], os.environ.get("LLM_BASE_URL"))

    print("\n[1] worker completion ...")
    try:
        result = await run_task("summarizer", "Reply with exactly: hello from starling", client=client)
        print(f"    OK -> {result.output!r}")
    except Exception as exc:
        print(f"    FAILED -> {type(exc).__name__}: {exc}")
        return

    print("\n[2] classify (tool/function calling) ...")
    cases = [
        ("summarize the pros and cons of SQLite vs Postgres for a single-user app", "ephemeral"),
        ("research the top 3 Python task-queue libraries and write a short comparison", "project"),
        ("plan a weekend itinerary, but ask me which city first", "project"),
    ]
    try:
        orch = Orchestrator(FakeChannel(), client, Blackboard(":memory:"))
        for text, expected in cases:
            c = await orch._classify(text)
            mark = "ok" if c.mode.value == expected else "!!"
            print(f"    [{mark}] {c.mode.value:9} (want {expected:9}) <- {text[:50]}")
    except Exception as exc:
        print(f"    FAILED -> {type(exc).__name__}: {exc}")
        return

    print("\n[3] decompose a decision-point goal ...")
    try:
        from starling.agents.pm import decompose
        plan = await decompose(
            "plan a weekend itinerary, but ask me which city first", client=client
        )
        for i, t in enumerate(plan.tasks):
            print(f"    {i}: [{t.role}] {t.description}  (depends_on={t.depends_on})")
    except Exception as exc:
        print(f"    FAILED -> {type(exc).__name__}: {exc}")
        return

    print("\nLLM integration works. Ready for the Telegram step.")


if __name__ == "__main__":
    asyncio.run(main())
