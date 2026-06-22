"""Task request/response schemas.

Separate Create / Update / Read shapes; the ORM model is never returned directly
(Golden rule #5). `assigned_by_id` is set server-side from the caller, never the
client, so it is absent from Create.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

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
    created_at: datetime
    updated_at: datetime
