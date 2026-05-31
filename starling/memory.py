"""Memory recall — inject what's known about the user into worker prompts.

Recall is by recency (most recent N) so durable preferences stay in context for later
requests regardless of keyword overlap with the current one. Embedding-based relevance
is a future upgrade. See ARCHITECTURE.md §8 / Starling-claw-prompts Phase C.
"""

from __future__ import annotations


def recall_context(blackboard, chat_id: int, limit: int = 10) -> str:
    """A short, prompt-ready summary of what's known about the user, or ''."""
    memories = blackboard.recall_memories(chat_id, limit=limit)
    if not memories:
        return ""
    return "\n".join(f"- {m['text']}" for m in memories)
