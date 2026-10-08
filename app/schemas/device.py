"""Device request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class DeviceCreate(BaseModel):
    """Admin/IT enrolls a device for an employee."""

    employee_id: uuid.UUID
    label: str = Field(min_length=1, max_length=256)


class DeviceSelfEnroll(BaseModel):
    """The agent self-enrolls the calling user's own machine on first launch.

    No employee_id — the device is always bound to the authenticated caller
    (Golden rule #2). hostname/os are optional hints used to build the label.
    """

    label: str | None = Field(default=None, max_length=256)
    hostname: str | None = Field(default=None, max_length=256)
    os: str | None = Field(default=None, max_length=128)


class DeviceCreated(BaseModel):
    """Returned on enrollment — carries the raw token, shown exactly once."""

    id: uuid.UUID
    employee_id: uuid.UUID
    label: str
    token: str
    created_at: datetime


class DeviceRead(ORMModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    label: str
    last_sequence: int
    is_revoked: bool
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DeviceReassign(BaseModel):
    """Admin/IT re-points a device at the employee who actually uses it."""

    employee_id: uuid.UUID
    # When the machine changed hands. Omitted ⇒ leave history alone and only fix
    # what the device captures from now on. Supplied ⇒ also re-file the rows
    # captured from that moment, which is what makes BOTH people's records true.
    history_from: datetime | None = Field(default=None)


class DeviceReassigned(ORMModel):
    """The updated device plus what the re-filing actually touched, so the UI
    can tell the admin exactly what changed rather than claiming success."""

    device: DeviceRead
    previous_employee_id: uuid.UUID
    screenshots_moved: int
    activity_samples_moved: int


class DeviceNudge(BaseModel):
    """Admin/manager nudges an employee to keep/reinstall the agent."""

    employee_id: uuid.UUID
    message: str | None = Field(default=None, max_length=280)


class DeviceNudgeResult(BaseModel):
    """Which channel the nudge went out on (agent on-screen vs notification+email)."""

    channel: Literal["agent", "notification_email"]


class FleetUpdateResult(BaseModel):
    """How many agents were signalled to self-update on demand."""

    updated: int
