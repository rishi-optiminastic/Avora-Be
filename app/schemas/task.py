"""Task request/response schemas.

Separate Create / Update / Read shapes; the ORM model is never returned directly
(Golden rule #5). `assigned_by_id` is set server-side from the caller, never the
client, so it is absent from Create.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, field_validator

from app.models.task import TaskCadence, TaskPriority, TaskStatus
from app.schemas.common import ORMModel


class Attachment(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=2048)


class TaskCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class TaskCommentRead(ORMModel):
    id: uuid.UUID
    task_id: uuid.UUID
    author_id: uuid.UUID | None
    body: str
    created_at: datetime


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    assignee_id: uuid.UUID
    description: str | None = Field(default=None, max_length=2000)
    project: str | None = Field(default=None, max_length=128)
    project_id: uuid.UUID | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    cadence: TaskCadence = TaskCadence.DAILY
    start_date: datetime | None = None
    due_date: datetime | None = None
    remarks: str | None = Field(default=None, max_length=2000)
    expected_output: str | None = Field(default=None, max_length=2000)
    attachments: list[Attachment] = Field(default_factory=list)
    parent_task_id: uuid.UUID | None = None
    depends_on_id: uuid.UUID | None = None

    @field_validator("due_date")
    @classmethod
    def _due_not_in_past(cls, value: datetime | None) -> datetime | None:
        """A new task can't already be overdue. One day of grace so a client's
        local 'today' is never rejected across timezones; clearly-past dates are
        refused (defense-in-depth — the picker also blocks them)."""
        if value is None:
            return value
        due = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        if due.date() < datetime.now(UTC).date() - timedelta(days=1):
            raise ValueError("Due date can't be in the past.")
        return value


class TaskUpdate(BaseModel):
    """Every field optional — only provided fields are changed."""

    title: str | None = Field(default=None, min_length=1, max_length=256)
    assignee_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    project: str | None = Field(default=None, max_length=128)
    project_id: uuid.UUID | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    cadence: TaskCadence | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    remarks: str | None = Field(default=None, max_length=2000)
    expected_output: str | None = Field(default=None, max_length=2000)
    completion_pct: int | None = Field(default=None, ge=0, le=100)
    review_notes: str | None = Field(default=None, max_length=2000)
    final_outcome: str | None = Field(default=None, max_length=2000)
    blocked_reason: str | None = Field(default=None, max_length=500)
    attachments: list[Attachment] | None = None


class TaskRead(ORMModel):
    id: uuid.UUID
    title: str
    description: str | None
    assignee_id: uuid.UUID
    assigned_by_id: uuid.UUID | None
    project: str | None
    project_id: uuid.UUID | None
    priority: TaskPriority
    status: TaskStatus
    cadence: TaskCadence
    start_date: datetime | None
    due_date: datetime | None
    remarks: str | None
    completed_at: datetime | None
    expected_output: str | None
    completion_pct: int
    review_notes: str | None
    final_outcome: str | None
    blocked_reason: str | None
    attachments: list[Attachment]
    escalated: bool
    parent_task_id: uuid.UUID | None
    depends_on_id: uuid.UUID | None
    collaborator_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# -- Bulk create ------------------------------------------------------------ #

# A paste can hold a lot of lines; cap it so one request can't create thousands
# of rows. Matches the page-size ceiling elsewhere in the API.
MAX_BULK_TASKS = 100


class TaskBulkCreate(BaseModel):
    """Create many tasks in one call (the paste-import flow). Each item is a
    normal `TaskCreate`, so every task still names an assignee the caller can see
    — 'general' tasks are assigned to the caller on the client before sending."""

    tasks: list[TaskCreate] = Field(min_length=1, max_length=MAX_BULK_TASKS)


# -- Free-text parsing (LLM) ------------------------------------------------ #

# The pasted blob. Generous but bounded — never feed an unbounded string to the
# model, and never echo it back in logs.
MAX_PARSE_CHARS = 8000


class TaskParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_PARSE_CHARS)


class ParsedTask(BaseModel):
    """One task the model pulled out of the paste. `assignee_id` is resolved to a
    real employee in the caller's scope when a name header matched; otherwise it
    is null (the client defaults it to the caller / lets the user pick)."""

    title: str = Field(min_length=1, max_length=256)
    assignee_id: uuid.UUID | None = None
    assignee_name: str | None = Field(default=None, max_length=128)


class TaskParseResult(BaseModel):
    tasks: list[ParsedTask]


# -- Collaborators ---------------------------------------------------------- #


class CollaboratorAdd(BaseModel):
    employee_id: uuid.UUID
