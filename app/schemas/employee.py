"""Employee request/response schemas.

Separate Create / Update / Read shapes. An ORM model is NEVER returned directly
(Golden rule #5). Note the *Read* schema deliberately omits nothing sensitive
here, but privilege-changing fields are absent from HR-sourced inputs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.employee import EmployeeStatus, Gender, Role, TrackingMode
from app.schemas.common import ORMModel


class EmployeeRead(ORMModel):
    id: uuid.UUID
    hr_external_id: str
    work_email: str
    full_name: str
    department: str | None
    location: str | None
    job_title: str | None
    timezone: str | None
    manager_id: uuid.UUID | None
    role: Role
    payroll_manager: bool = False
    status: EmployeeStatus
    is_active: bool
    tracking_mode: TrackingMode
    biometric_id: str | None
    hire_date: date | None
    uan_number: str | None
    date_of_birth: date | None
    gender: Gender | None
    has_avatar: bool
    created_at: datetime
    updated_at: datetime


class EmployeeRoleUpdate(BaseModel):
    """Admin-only privilege change — the ONLY way a role is ever set."""

    role: Role


class PayrollManagerUpdate(BaseModel):
    """Admin-only capability grant: turn payroll-cluster management on/off for one
    person (e.g. a finance executive doing salary processing)."""

    payroll_manager: bool


class AssignmentGrantsUpdate(BaseModel):
    """Admin/HR replacing who an employee may assign work to beyond their own
    reports. The full desired list — anything omitted is revoked."""

    assignee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class TrackingModeUpdate(BaseModel):
    """An employee toggling their own work/personal capture mode."""

    mode: TrackingMode


class SelfProfileUpdate(BaseModel):
    """Fields a person may edit on their OWN profile — display/preference only.

    Deliberately excludes email, role, department, and manager: email is the
    identity key, role is admin-only (rule 5.5), and department/manager are org
    structure that drive scope, so HR/admin own them — not self-service.

    `date_of_birth` and `gender` are REQUIRED — every employee must record them
    (they gate birthday / maternity / paternity leave), and the dashboard blocks
    on a completion gate until they're set, so the profile save enforces them too.
    """

    full_name: str = Field(min_length=1, max_length=256)
    job_title: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    date_of_birth: date
    gender: Gender

    @field_validator("date_of_birth")
    @classmethod
    def _dob_in_past(cls, value: date) -> date:
        if value >= datetime.now(UTC).date():
            raise ValueError("Date of birth must be in the past.")
        return value


class AdminProfileUpdate(BaseModel):
    """Admin/HR editing another employee's profile incl. org + HR-operational fields.

    Email + role + hr_external_id are still excluded: email and hr_external_id are
    identity / HR-sync keys, and role is the admin-only privilege path (rule 5.5).
    NOTE: department/manager/hire_date may be overwritten by the next HR sync, which
    is the system of record for org structure. `hire_date` moves the leave-year
    anniversary; `biometric_id` is the office-device punch-matching key.
    """

    full_name: str = Field(min_length=1, max_length=256)
    department: str | None = Field(default=None, max_length=128)
    location: str | None = Field(default=None, max_length=128)
    job_title: str | None = Field(default=None, max_length=128)
    manager_id: uuid.UUID | None = None
    timezone: str | None = Field(default=None, max_length=64)
    hire_date: date | None = None
    uan_number: str | None = None
    date_of_birth: date | None = None
    gender: Gender | None = None
    biometric_id: str | None = Field(default=None, max_length=64)

    @field_validator("uan_number")
    @classmethod
    def _valid_uan(cls, value: str | None) -> str | None:
        """UAN is exactly 12 digits. Blank clears it; anything else must match."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not (cleaned.isdigit() and len(cleaned) == 12):
            raise ValueError("UAN must be exactly 12 digits.")
        return cleaned


class EmployeeStatusUpdate(BaseModel):
    """Admin/HR activating or deactivating (soft-delete / offboard) an employee."""

    is_active: bool


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
