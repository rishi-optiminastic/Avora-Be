"""Playbook request and response shapes.

A step carries a TEMPLATE, never a rendered title: the rendering happens per
run, against the subject on the day. Sending back a rendered title from an
authoring endpoint would invite somebody to edit the rendered copy and expect
the template to change.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.core.playbook import MAX_OFFSET_DAYS, MIN_OFFSET_DAYS, unknown_placeholders
from app.models.playbook import (
    AssigneeRule,
    PlaybookRunStatus,
    PlaybookStatus,
    PlaybookTrigger,
)
from app.models.task import TaskPriority
from app.schemas.common import ORMModel

MAX_STEPS = 60


class StepWrite(BaseModel):
    title_template: str = Field(min_length=1, max_length=256)
    description_template: str | None = Field(default=None, max_length=2000)
    assignee_rule: AssigneeRule = AssigneeRule.SUBJECT
    fixed_assignee_id: uuid.UUID | None = None
    offset_days: int = Field(default=0, ge=MIN_OFFSET_DAYS, le=MAX_OFFSET_DAYS)
    priority: TaskPriority = TaskPriority.MEDIUM
    expected_output: str | None = Field(default=None, max_length=2000)

    @field_validator("title_template", "description_template", "expected_output")
    @classmethod
    def _only_fillable_placeholders(cls, value: str | None) -> str | None:
        """Reject `{nmae}` at the edge, not when a joiner reads it in a task.

        Validated here as well as in the service: the service check is what
        guards publishing a playbook assembled over several requests, and this
        one gives the author the error on the keystroke that caused it.
        """
        if value is None:
            return None
        bad = unknown_placeholders(value)
        if bad:
            joined = ", ".join("{" + b + "}" for b in bad)
            raise ValueError(f"nothing can fill {joined}")
        return value


class StepRead(ORMModel):
    id: uuid.UUID
    position: int
    title_template: str
    description_template: str | None
    assignee_rule: AssigneeRule
    fixed_assignee_id: uuid.UUID | None
    offset_days: int
    priority: TaskPriority
    expected_output: str | None


class PlaybookWrite(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    trigger: PlaybookTrigger = PlaybookTrigger.MANUAL
    trigger_days: int | None = Field(default=None, ge=0, le=MAX_OFFSET_DAYS)
    #: Whose checklist it is. Required for a monthly trigger, which is about
    #: a desk rather than a person, and ignored otherwise - the lifecycle
    #: event supplies the subject. Enforced at publish, not here, so a draft
    #: can be saved half-finished.
    owner_id: uuid.UUID | None = None
    steps: list[StepWrite] = Field(default_factory=list, max_length=MAX_STEPS)


class PlaybookRead(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: PlaybookStatus
    trigger: PlaybookTrigger
    trigger_days: int | None
    #: The backfill guard, exposed so the UI can say WHY an automatic playbook
    #: is not firing for people who joined before it existed.
    auto_assign_since: date | None
    owner_id: uuid.UUID | None = None
    published_at: date | None
    step_count: int = 0


class PlaybookDetail(PlaybookRead):
    steps: list[StepRead] = Field(default_factory=list)


class RunWrite(BaseModel):
    subject_id: uuid.UUID
    #: Defaults to the subject's hire date, then today. Offsets count from it.
    anchor: date | None = None


class RunRead(ORMModel):
    id: uuid.UUID
    playbook_id: uuid.UUID
    subject_id: uuid.UUID
    trigger: PlaybookTrigger
    status: PlaybookRunStatus
    anchor_date: date
    #: "" for a once-ever run, "YYYY-MM" for a monthly one.
    period_key: str = ""
    tasks_created: int
    ran_by: uuid.UUID | None


class RunResult(BaseModel):
    run_id: uuid.UUID | None
    created: int
    #: Steps that produced nothing, with the reason. Never empty-and-silent:
    #: a joiner with no manager on file has to be visible to whoever ran it.
    skipped: list[str] = Field(default_factory=list)


class CancelResult(BaseModel):
    tasks_removed: int
    #: What cancelling did NOT remove, because somebody had started it.
    tasks_kept: int
