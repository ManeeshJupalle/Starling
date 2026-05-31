"""Token + rough cost tracking across all model calls (visible on the dashboard).

A single process-wide accumulator. The per-1M-token prices are a rough estimate for the
default model so the running cost is *visible* — they're not billing-accurate.
"""

from __future__ import annotations

from dataclasses import dataclass

# Rough USD per 1M tokens (gpt-4o-mini-class). For a visible estimate, not billing.
_INPUT_PER_M = 0.15
_OUTPUT_PER_M = 0.60


@dataclass
class _Totals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


_totals = _Totals()


def record(resp) -> None:
    """Accumulate token usage from a chat-completion response (no-op if absent)."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    _totals.calls += 1
    _totals.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
    _totals.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)


def snapshot() -> dict:
    cost = (_totals.input_tokens / 1e6) * _INPUT_PER_M + (_totals.output_tokens / 1e6) * _OUTPUT_PER_M
    return {
        "calls": _totals.calls,
        "input_tokens": _totals.input_tokens,
        "output_tokens": _totals.output_tokens,
        "est_cost_usd": round(cost, 4),
    }


def reset() -> None:
    global _totals
    _totals = _Totals()
