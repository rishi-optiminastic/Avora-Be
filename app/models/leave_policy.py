"""Leave policy — the single org-wide leave entitlement rule set.

One row (a singleton, upserted by the service), mirroring `AttendancePolicy`.
It holds the annual quota per *paid* leave type; the per-employee leave year runs
from the joining anniversary (`Employee.hire_date`), so a quota resets each year
on the date the person joined. Unpaid leave is uncapped and excluded from quotas.
Admin/HR edit it; everyone may read it (so people see the entitlement they have).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LeavePolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_policy"

    # Annual entitlement (working days) per paid leave type, per leave year.
    annual_planned_days: Mapped[int] = mapped_column(default=12)
    annual_sick_days: Mapped[int] = mapped_column(default=8)
    # Additional paid leave types (see LeaveType). Each is an annual working-day
    # quota; birthday/maternity/paternity are further gated by profile fields
    # (date_of_birth / gender) in the service.
    annual_days: Mapped[int] = mapped_column(default=15)
    bereavement_days: Mapped[int] = mapped_column(default=5)
    birthday_days: Mapped[int] = mapped_column(default=1)
    maternity_days: Mapped[int] = mapped_column(default=90)
    paternity_days: Mapped[int] = mapped_column(default=15)
    marriage_days: Mapped[int] = mapped_column(default=5)

    # Minimum days' notice required to apply for PLANNED / ANNUAL leave (0 ⇒ any
    # time). Sick / unpaid / half-day / event-based leave are applicable any time.
    planned_min_notice_days: Mapped[int] = mapped_column(default=2)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
