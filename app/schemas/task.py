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


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    assignee_id: uuid.UUID
    description: str | None = Field(default=None, max_length=2000)
    project: str | None = Field(default=None, max_length=128)
    priority: TaskPriority = TaskPriority.MEDIUM
    cadence: TaskCadence = TaskCadence.DAILY
    start_date: datetime | None = None
    due_date: datetime | None = None
    remarks: str | None = Field(default=None, max_length=2000)


class TaskUpdate(BaseModel):
    """Every field optional — only provided fields are changed."""

    title: str | None = Field(default=None, min_length=1, max_length=256)
    assignee_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    project: str | None = Field(default=None, max_length=128)
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    cadence: TaskCadence | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    remarks: str | None = Field(default=None, max_length=2000)


class TaskRead(ORMModel):
    id: uuid.UUID
    title: str
    description: str | None
    assignee_id: uuid.UUID
    assigned_by_id: uuid.UUID | None
    project: str | None
    priority: TaskPriority
    status: TaskStatus
    cadence: TaskCadence
    start_date: datetime | None
    due_date: datetime | None
    remarks: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
