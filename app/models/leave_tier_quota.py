"""Leave entitlement per tenure band — what each tier grants, per leave type.

Sits between the org-wide `LeavePolicy` (the flat default) and the per-employee
`LeaveAllocation` (the individual override), so the resolution order for any
quota is:

    LeaveAllocation override  →  LeaveTierQuota for the employee's band  →  LeavePolicy

A row is SPARSE by design: only the (tier, leave_type) pairs that actually differ
from the org default need one. An absent row means "this band uses the org
policy", which is what keeps the tenured band working before its own policy is
decided, and keeps statutory leave (maternity/paternity) uniform across bands
unless somebody deliberately says otherwise.

Two mutually exclusive ways to express an entitlement:
  `annual_days`          — a flat total for the leave year (e.g. 4 sick days)
  `monthly_accrual_days` — earned per month, unused days rolling forward within
                           the leave year (e.g. 1 planned leave a month)
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.tenure import TenureStatus
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.leave import LeaveType


class LeaveTierQuota(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leave_tier_quotas"
    __table_args__ = (
        UniqueConstraint("tier", "leave_type", name="uq_leave_tier_quotas_tier_type"),
    )

    tier: Mapped[TenureStatus] = mapped_column(index=True)
    leave_type: Mapped[LeaveType] = mapped_column(index=True)

    # Flat entitlement for the leave year. 0 is meaningful ("this band gets none")
    # and is NOT the same as NULL ("fall through to the org policy").
    annual_days: Mapped[int | None] = mapped_column(Integer, default=None)

    # Accrued entitlement, in days earned per month of eligibility. Set this OR
    # `annual_days`, never both — `accrual` wins if somebody sets both anyway.
    monthly_accrual_days: Mapped[float | None] = mapped_column(Numeric(4, 2), default=None)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
