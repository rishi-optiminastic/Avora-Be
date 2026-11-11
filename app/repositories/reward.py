"""Reward data access — the catalogue, and what people have spent.

Balances are never stored. `points_spent_by` sums the redemptions that still
hold points, and the service subtracts it from what recognition earned, so
there is no counter that can drift away from the rows explaining it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.reward import (
    SPENDING_STATUSES,
    RedemptionStatus,
    RewardItem,
    RewardRedemption,
)

MAX_PAGE = 100


class RewardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- catalogue --------------------------------------------------------- #
    async def add_item(self, **values: object) -> RewardItem:
        item = RewardItem(**values)
        self._session.add(item)
        await self._session.flush()
        return item

    async def get_item(self, item_id: uuid.UUID) -> RewardItem | None:
        return await self._session.get(RewardItem, item_id)

    async def items(self, *, include_retired: bool = False) -> Sequence[RewardItem]:
        """The catalogue, cheapest first so the reachable things come first."""
        query = select(RewardItem).order_by(RewardItem.points_cost, RewardItem.title)
        if not include_retired:
            query = query.where(RewardItem.is_active.is_(True))
        rows = await self._session.scalars(query)
        return list(rows)

    # --- locks -------------------------------------------------------------- #
    async def lock_spender(self, employee_id: uuid.UUID) -> None:
        """Serialise one person's spending.

        `redeem` reads a balance and then writes against it. Without this lock
        two requests arriving together both read the same available points,
        both pass, and the person spends the same 50 twice - the balance goes
        negative and nothing in the data says which request was the mistake.

        The employee row is the lock because the balance is derived from rows
        in two tables; there is no single row to lock that represents it.
        """
        await self._session.execute(
            select(Employee.id).where(Employee.id == employee_id).with_for_update()
        )

    async def lock_item(self, item_id: uuid.UUID) -> None:
        """Serialise approvals of one catalogue item, for the same reason:
        two approvals read `stock - approved` together and both see one left."""
        await self._session.execute(
            select(RewardItem.id).where(RewardItem.id == item_id).with_for_update()
        )

    # --- spending ---------------------------------------------------------- #
    async def add_redemption(self, **values: object) -> RewardRedemption:
        row = RewardRedemption(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_redemption(self, redemption_id: uuid.UUID) -> RewardRedemption | None:
        return await self._session.get(RewardRedemption, redemption_id)

    async def points_spent_by(self, employee_id: uuid.UUID) -> int:
        """Points this person has committed.

        A DECLINED or CANCELLED redemption gave the points back, so it must not
        count - otherwise refusing a request silently fines the person who
        made it.
        """
        total = await self._session.scalar(
            select(func.coalesce(func.sum(RewardRedemption.points_spent), 0)).where(
                RewardRedemption.employee_id == employee_id,
                RewardRedemption.status.in_(SPENDING_STATUSES),
            )
        )
        return int(total or 0)

    async def approved_count_for_item(self, item_id: uuid.UUID) -> int:
        """How many of this item are already committed, for the stock check."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(RewardRedemption)
            .where(
                RewardRedemption.item_id == item_id,
                RewardRedemption.status.in_(
                    (RedemptionStatus.APPROVED, RedemptionStatus.FULFILLED)
                ),
            )
        )
        return int(total or 0)

    async def redemptions(
        self,
        *,
        employee_id: uuid.UUID | None = None,
        status: RedemptionStatus | None = None,
        limit: int = 50,
    ) -> Sequence[RewardRedemption]:
        query = select(RewardRedemption).order_by(RewardRedemption.created_at.desc())
        if employee_id is not None:
            query = query.where(RewardRedemption.employee_id == employee_id)
        if status is not None:
            query = query.where(RewardRedemption.status == status)
        rows = await self._session.scalars(query.limit(min(limit, MAX_PAGE)))
        return list(rows)
