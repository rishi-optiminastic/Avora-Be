"""Employee request/response schemas.

Separate Create / Update / Read shapes. An ORM model is NEVER returned directly
(Golden rule #5). Note the *Read* schema deliberately omits nothing sensitive
here, but privilege-changing fields are absent from HR-sourced inputs.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.employee import EmployeeStatus, Role, TrackingMode
from app.schemas.common import ORMModel


class EmployeeRead(ORMModel):
    id: uuid.UUID
    hr_external_id: str
    work_email: str
    full_name: str
    department: str | None
    manager_id: uuid.UUID | None
    role: Role
    status: EmployeeStatus
    is_active: bool
    tracking_mode: TrackingMode
    biometric_id: str | None
    created_at: datetime
    updated_at: datetime


class EmployeeRoleUpdate(BaseModel):
    """Admin-only privilege change — the ONLY way a role is ever set."""

    role: Role


class TrackingModeUpdate(BaseModel):
    """An employee toggling their own work/personal capture mode."""

    mode: TrackingMode


class HREmployeeUpsert(BaseModel):
    """Payload the HR webhook may send.

    Crucially, there is NO `role`, `admin`, or privilege field here — the schema
    makes it structurally impossible for HR to escalate privilege (rule 5.5).
    """

    hr_external_id: str = Field(min_length=1, max_length=128)
    work_email: EmailStr
    full_name: str = Field(min_length=1, max_length=256)
    department: str | None = Field(default=None, max_length=128)
    manager_external_id: str | None = Field(default=None, max_length=128)
    status: EmployeeStatus
    start_date: datetime | None = None
    # Optional biometric-device enrollment id, so HR can map a person to their
    # attendance-device id in the same sync. Never a privilege field (rule 5.5).
    biometric_id: str | None = Field(default=None, max_length=64)
