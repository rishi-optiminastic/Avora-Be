"""Employee — identity and the org tree.

HR is the source of truth for identity and reporting lines (who reports to whom)
but NEVER for privilege. `role` is set only inside PMS by an admin; the HR
webhook is forbidden from touching it (Security rule 5.5).
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import Boolean, Date, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Role(StrEnum):
    EMPLOYEE = "employee"
    EXECUTIVE = "executive"
    MANAGER = "manager"
    SENIOR_MANAGER = "senior_manager"
    HR = "hr"
    ADMIN = "admin"
    IT_ADMIN = "it_admin"
    VIEWER = "viewer"


class EmployeeStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"  # soft-deleted on offboard; never hard-deleted


class Gender(StrEnum):
    """Self-reported gender. Gates gender-specific leave (maternity / paternity)
    and, with `date_of_birth`, birthday leave."""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class TrackingMode(StrEnum):
    """Whether the laptop agent should be capturing this person right now.

    WORK → activity + screenshots are collected. PERSONAL → nothing is collected
    or stored (the employee is on a break / off the clock). This is the server's
    source of truth and the ingest gate, independent of clock-in/out.
    """

    WORK = "work"
    PERSONAL = "personal"


class Employee(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("work_email", name="uq_employees_work_email"),)

    # External id from the HR system (stable join key for webhook upserts).
    hr_external_id: Mapped[str] = mapped_column(String(128), index=True, unique=True)

    work_email: Mapped[str] = mapped_column(String(320), index=True)
    full_name: Mapped[str] = mapped_column(String(256))
    department: Mapped[str | None] = mapped_column(String(128), default=None)

    # Self-service profile fields (display/preference only — never privilege).
    job_title: Mapped[str | None] = mapped_column(String(128), default=None)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    # Self-reported, required of every employee (enforced in the app, not the DB,
    # so existing rows stay valid). DOB drives birthday leave; gender gates
    # maternity/paternity leave.
    date_of_birth: Mapped[date | None] = mapped_column(Date, default=None)
    gender: Mapped[Gender | None] = mapped_column(default=None)

    # Enrollment id on the office biometric device. The on-prem connector tags
    # each punch with this so we can match it to an employee; set by HR sync or an
    # admin. Nullable (not everyone is enrolled); indexed for the lookup.
    biometric_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)

    # Joining date, sourced from HR (`start_date` on the sync payload). Drives the
    # per-employee leave year (quotas reset on the joining anniversary). Nullable
    # for older records / invite-provisioned people; the leave-balance logic falls
    # back to `created_at` when this is unset.
    hire_date: Mapped[date | None] = mapped_column(Date, default=None)

    # EPF Universal Account Number — the 12-digit PF identifier. HR-operational
    # data, set by HR/admin (never by the agent or self-service). Nullable: not
    # every employee has one on file, and non-India orgs won't use it at all.
    uan_number: Mapped[str | None] = mapped_column(String(12), default=None)

    # Org tree: self-referential reporting line, set from HR.
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )

    # Privilege — PMS-owned, NEVER set by HR.
    role: Mapped[Role] = mapped_column(default=Role.EMPLOYEE)

    status: Mapped[EmployeeStatus] = mapped_column(default=EmployeeStatus.ACTIVE, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Employee-controlled: pauses all capture when set to PERSONAL (rule: track
    # only in work mode). The ingest path drops samples/screenshots when PERSONAL.
    tracking_mode: Mapped[TrackingMode] = mapped_column(default=TrackingMode.WORK)

    # Profile photo — the employee sets their own. Bytes live in S3 under
    # `avatar_object_key`; `avatar_content` is the in-DB fallback (used only when
    # S3 is unconfigured), mirroring Screenshot/WorkspaceFile.
    avatar_object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    avatar_content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    avatar_content_type: Mapped[str | None] = mapped_column(String(64), default=None)

    manager: Mapped[Employee | None] = relationship(
        remote_side="Employee.id",
        backref="reports",
    )

    @property
    def has_avatar(self) -> bool:
        return bool(self.avatar_object_key or self.avatar_content)
