"""Phase F verification: tool reliability (retries / timeout) + cost tracking.

Offline: a flaky read-only tool is retried to success; a flaky write is NOT retried;
a hanging tool is bounded by a timeout; token usage accumulates with a cost estimate.
"""

import asyncio
import time

from starling import usage
from starling.agents.worker import _safe_call
from starling.tools.base import Tool, ToolRegistry


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    assert condition, name


def registry(name: str, fn) -> ToolRegistry:
    reg = ToolRegistry()
    reg.add(Tool(name, "", {"type": "object"}, fn))
    return reg


async def test_retry_safe() -> None:
    print("flaky read-only tool is retried until it succeeds:")
    n = {"i": 0}

    async def flaky(args):
        n["i"] += 1
        if n["i"] < 3:
            raise RuntimeError("transient")
        return "ok"

    out = await _safe_call(registry("svc__get_data", flaky),
                           {"name": "svc__get_data", "arguments": "{}"},
                           timeout=1, retries=2, base_delay=0.01)
    check("retried to success (3 attempts)", n["i"] == 3)
    check("returned the successful result", out == "ok")


async def test_no_retry_sensitive() -> None:
    print("\nflaky state-changing tool is NOT retried:")
    n = {"i": 0}

    async def flaky_write(args):
        n["i"] += 1
        raise RuntimeError("boom")

    out = await _safe_call(registry("svc__create_thing", flaky_write),
                           {"name": "svc__create_thing", "arguments": "{}"},
                           timeout=1, retries=2, base_delay=0.01)
    check("tried exactly once (no retry on a write)", n["i"] == 1)
    check("error surfaced to the model", out.startswith("error:"))


async def test_timeout() -> None:
    print("\nhanging tool is bounded by a timeout:")

    async def hang(args):
        await asyncio.sleep(5)
        return "never"

    start = time.perf_counter()
    out = await _safe_call(registry("svc__get_slow", hang),
                           {"name": "svc__get_slow", "arguments": "{}"},
                           timeout=0.1, retries=0, base_delay=0)
    elapsed = time.perf_counter() - start
    check("returned quickly (< 1s, did not hang)", elapsed < 1.0)
    check("timeout surfaced as an error", out.startswith("error:"))


def test_usage_tracking() -> None:
    print("\ntoken/cost tracking:")
    usage.reset()

    class _U:
        prompt_tokens = 100
        completion_tokens = 50

    class _R:
        usage = _U()

    usage.record(_R())
    usage.record(_R())
    snap = usage.snapshot()
    check("two calls counted", snap["calls"] == 2)
    check("tokens summed", snap["input_tokens"] == 200 and snap["output_tokens"] == 100)
    check("cost estimated (> 0)", snap["est_cost_usd"] > 0)

    usage.record(object())  # a response with no usage attribute
    check("response without usage is ignored", usage.snapshot()["calls"] == 2)
    usage.reset()


async def main() -> None:
    await test_retry_safe()
    await test_no_retry_sensitive()
    await test_timeout()
    test_usage_tracking()
    print("\nALL PASS: tool retries/timeout + cost tracking (Phase F).")


if __name__ == "__main__":
    asyncio.run(main())
