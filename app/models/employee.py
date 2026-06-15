"""Employee — identity and the org tree.

HR is the source of truth for identity and reporting lines (who reports to whom)
but NEVER for privilege. `role` is set only inside PMS by an admin; the HR
webhook is forbidden from touching it (Security rule 5.5).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
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


class Employee(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("work_email", name="uq_employees_work_email"),)

    # External id from the HR system (stable join key for webhook upserts).
    hr_external_id: Mapped[str] = mapped_column(String(128), index=True, unique=True)

    work_email: Mapped[str] = mapped_column(String(320), index=True)
    full_name: Mapped[str] = mapped_column(String(256))
    department: Mapped[str | None] = mapped_column(String(128), default=None)

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

    manager: Mapped[Employee | None] = relationship(
        remote_side="Employee.id",
        backref="reports",
    )
