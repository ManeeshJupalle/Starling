"""Pydantic schemas for validated LLM control-flow output.

Every structured decision the model emits is parsed through these models before it
is acted on (ARCHITECTURE.md §5): ``Classification`` routes an inbound request, and
``ProjectPlan`` is the PM's decomposition (used from Phase 4). Validation is the
guardrail between the model's free-form output and the orchestration loop.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Mode(str, Enum):
    """How a request is handled: answered now, or planned as a project."""

    EPHEMERAL = "ephemeral"
    PROJECT = "project"


class Classification(BaseModel):
    """The orchestrator's first call: route a request (ARCHITECTURE.md §5)."""

    mode: Mode
    goal: str = Field(description="The request restated as one clear instruction.")
    workers: list[str] = Field(
        default_factory=list,
        description="Worker roles to fan out to for an ephemeral request.",
    )


class PlannedTask(BaseModel):
    """One task in a PM decomposition. ``depends_on`` indexes into the plan list."""

    role: str
    description: str
    depends_on: list[int] = Field(
        default_factory=list,
        description="Indices of tasks in the same plan that must finish first.",
    )


class ProjectPlan(BaseModel):
    """The PM's task graph for a project goal (ARCHITECTURE.md §5)."""

    tasks: list[PlannedTask]
