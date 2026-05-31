"""Pydantic schemas for validated LLM control-flow output.

Every structured decision the model emits is parsed through these models before it
is acted on (ARCHITECTURE.md §5): ``Classification`` routes an inbound request, and
``ProjectPlan`` is the PM's decomposition (used from Phase 4). Validation is the
guardrail between the model's free-form output and the orchestration loop.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Mode(str, Enum):
    """How a request is handled: answered now, or planned as a project."""

    EPHEMERAL = "ephemeral"
    PROJECT = "project"


class ScheduleSpec(BaseModel):
    """A proactive schedule the user asked to set up (Phase H1)."""

    recurrence: Literal["once", "daily"] = Field(
        description="'daily' to repeat every day, 'once' for a single future firing."
    )
    at: str = Field(description="Time of day to fire, 24-hour 'HH:MM'.")


class WatchSpec(BaseModel):
    """An inbox watch the user asked to set up (Phase H2): poll Gmail, act on change."""

    query: str = Field(
        description="Gmail search query to poll, e.g. 'is:unread' or 'from:boss@x.com'."
    )
    every_minutes: int = Field(
        default=5, description="How often to poll the inbox, in minutes."
    )


class Classification(BaseModel):
    """The orchestrator's first call: route a request (ARCHITECTURE.md §5)."""

    mode: Mode
    goal: str = Field(description="The request restated as one clear instruction.")
    workers: list[str] = Field(
        default_factory=list,
        description="Worker roles to fan out to for an ephemeral request.",
    )
    memory: Optional[str] = Field(
        default=None,
        description="A durable preference or fact about the user stated in this message "
        "(short, third person), worth remembering for future requests; null if none.",
    )
    schedule: Optional[ScheduleSpec] = Field(
        default=None,
        description="Set ONLY when the user asks to set up a recurring or future task "
        "(e.g. 'every morning brief me'); otherwise null. 'goal' holds the task itself.",
    )
    watch: Optional[WatchSpec] = Field(
        default=None,
        description="Set ONLY when the user asks to watch their inbox / react to new "
        "emails (e.g. 'when an email from my boss arrives, draft a reply'); otherwise "
        "null. 'goal' holds what to do when something new arrives.",
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


class Verdict(BaseModel):
    """A critic's check of a deliverable against the goal (Phase I2)."""

    ok: bool = Field(description="True if the draft adequately and correctly meets the goal.")
    reason: str = Field(default="", description="Brief explanation when not ok.")
    revised: Optional[str] = Field(
        default=None,
        description="A corrected deliverable (using only facts already in the draft) when "
        "the shortfall is fixable; null otherwise.",
    )
