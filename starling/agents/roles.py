"""Role -> system prompt, plus the default model.

Same model, different system prompts per role (ARCHITECTURE.md §2.5). Workers are
stateless; their behaviour is entirely defined by the system prompt selected here.
"""

from __future__ import annotations

import os

# Default model id (provider-agnostic). Override per-run with the LLM_MODEL env var.
DEFAULT_MODEL = "openai/gpt-4o-mini"


def active_model() -> str:
    """The model id to use, resolved at call time so .env overrides take effect."""
    return os.environ.get("LLM_MODEL") or DEFAULT_MODEL


ROLE_PROMPTS: dict[str, str] = {
    "researcher": (
        "You are a research worker in a multi-agent assistant. Given a task, gather "
        "and lay out the relevant facts, options, and trade-offs accurately and "
        "concisely. Prefer concrete specifics over generalities; do not pad."
    ),
    "summarizer": (
        "You are a summarization worker in a multi-agent assistant. Produce a clear, "
        "well-structured, balanced summary of the requested topic. Be concise; use "
        "short sections or bullets when they aid clarity."
    ),
    "coder": (
        "You are a coding worker in a multi-agent assistant. Produce correct, minimal, "
        "idiomatic code for the task with a one-line explanation. No unnecessary "
        "boilerplate."
    ),
    "pm": (
        "You are the project manager in a multi-agent assistant. Decompose a goal into "
        "a small, acyclic set of concrete tasks for the available worker roles. Used "
        "in project mode."
    ),
}

# Roles dispatchable as ephemeral fan-out workers. 'pm' plans projects; it is not an
# ephemeral worker, so it is excluded here.
WORKER_ROLES: tuple[str, ...] = ("researcher", "summarizer", "coder")

# Roles a PM may assign to a planned task: the workers plus 'pm', whose description is
# a question routed to the user at a decision point (human-in-the-loop, ARCHITECTURE.md §3).
PLAN_ROLES: tuple[str, ...] = WORKER_ROLES + ("pm",)

# Model-per-role would plug in here: map a role to its own model and have
# worker.run_task / active_model look it up, falling back to DEFAULT_MODEL.
#   ROLE_MODELS = {"coder": "openai/gpt-4o", "summarizer": "groq/llama-3.1-8b-instant"}

# Which MCP servers each worker role may use. Read-only until Phase B adds approval.
ROLE_TOOLS: dict[str, list[str]] = {
    "researcher": ["filesystem", "github"],
    "summarizer": ["filesystem"],
    "coder": ["filesystem"],
}


def tools_for_role(manager, role: str, allow_sensitive: bool = False):
    """The ToolRegistry a role may use, or None if it has no tools/manager.

    Read-only by default; ``allow_sensitive=True`` (scheduler tasks, which can pause for
    approval) also exposes write tools. ``manager`` is an MCPManager (duck-typed to keep
    this module free of tool imports).
    """
    if manager is None:
        return None
    servers = ROLE_TOOLS.get(role, [])
    if not servers:
        return None
    return manager.registry_for(servers, include_sensitive=allow_sensitive)
