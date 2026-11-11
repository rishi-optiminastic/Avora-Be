"""Rewards — what recognition points are actually worth.

Recognition on its own is a feed that scrolls away. A reward is the other
half: points accumulate into a balance, and a balance buys something real.

Two tables, and the split matters:

* **RewardItem** is the catalogue HR publishes - what is on offer and what it
  costs today. Editable, because prices and stock change.
* **RewardRedemption** is one person spending points on one item. It snapshots
  `points_spent` and the title at request time, so repricing or retiring an
  item never rewrites what somebody already paid. The same rule the
  recognition row follows for its own points, and for the same reason: a
  balance that moves without a transaction behind it cannot be trusted.

A balance is therefore always derivable and never stored:

    earned (sum of recognitions received) - spent (redemptions not refused)

so there is no counter to drift out of step with the rows that explain it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RedemptionStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    FULFILLED = "fulfilled"
    DECLINED = "declined"
    # Withdrawn by the person who asked, before anyone acted on it.
    CANCELLED = "cancelled"


#: Statuses that still hold the points. A declined or cancelled redemption
#: returns them, so it must NOT count against the balance.
SPENDING_STATUSES = frozenset(
    {RedemptionStatus.REQUESTED, RedemptionStatus.APPROVED, RedemptionStatus.FULFILLED}
)


class RewardItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One thing points can be spent on."""

    __tablename__ = "reward_items"
    __table_args__ = (Index("ix_reward_items_active_cost", "is_active", "points_cost"),)

    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(String(1000), default=None)
    points_cost: Mapped[int] = mapped_column(Integer)
    # Retired rather than deleted: redemptions reference it, and the catalogue
    # is a record of what was once on offer.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # None = unlimited. A number is decremented as redemptions are APPROVED,
    # not when they are requested - a request nobody approved has reserved
    # nothing, and holding stock against it would let anyone empty the shelf.
    stock: Mapped[int | None] = mapped_column(Integer, default=None)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )


class RewardRedemption(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One person spending points on one catalogue item."""

    __tablename__ = "reward_redemptions"
    __table_args__ = (
        Index("ix_reward_redemptions_employee_status", "employee_id", "status"),
        Index("ix_reward_redemptions_status_created", "status", "created_at"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    # SET NULL so retiring an item never erases what someone redeemed; the
    # snapshotted title below is what the record reads back as.
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reward_items.id", ondelete="SET NULL"), default=None, index=True
    )
    # Snapshots, for exactly the reason in the module docstring.
    item_title: Mapped[str] = mapped_column(String(160))
    points_spent: Mapped[int] = mapped_column(Integer)

    status: Mapped[RedemptionStatus] = mapped_column(default=RedemptionStatus.REQUESTED)
    # Why HR declined, or how it was fulfilled ("voucher emailed 12 Nov").
    note: Mapped[str | None] = mapped_column(String(1000), default=None)

    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
