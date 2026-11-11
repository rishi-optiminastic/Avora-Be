"""Rewards business rules — what recognition points are worth, and spending them.

The rules that carry weight:

* **A balance is derived, never stored.** `earned - spent` over the rows that
  explain it. There is no counter to drift, and no migration that can leave
  somebody richer or poorer than their history says.
* **Points are snapshotted when they move.** A recognition records what it was
  worth; a redemption records what it cost. Repricing a badge or a reward
  changes what happens NEXT, never what already happened.
* **Only committed points count as spent.** A declined or cancelled redemption
  gives them back, so refusing a request never quietly fines the person who
  made it.
* **The balance is re-checked at approval.** Minutes pass between asking and
  deciding, and the person may have spent the same points elsewhere. Checking
  only at request time is how a balance goes negative.
* **HR runs the catalogue.** Everyone can see it and spend against it; only
  HR and admins publish items or decide redemptions.
* **Reading a balance and writing against it is one transaction.** Both paths
  that do it take a row lock first - the spender for a redemption, the item
  for an approval. A check-then-write with no lock is how two requests spend
  the same points, and a points system that can go negative is worse than one
  that occasionally makes somebody wait.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.recognition_badges import PEER_AWARDS_PER_MONTH, month_start, week_start
from app.models.employee import Employee, Role
from app.models.reward import RedemptionStatus, RewardItem, RewardRedemption
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.recognition import RecognitionRepository
from app.repositories.reward import RewardRepository
from app.schemas.auth import CurrentUser
from app.schemas.reward import (
    DepartmentStandingRead,
    PointsBalance,
    RedemptionDecision,
    RedemptionRead,
    RewardItemCreate,
    RewardItemRead,
    RewardItemUpdate,
    StandingRead,
    Whiteboard,
)

#: A redemption is only the requester's to withdraw while nobody has acted.
_CANCELLABLE = frozenset({RedemptionStatus.REQUESTED})
#: Statuses that hold stock. A request reserves nothing - otherwise one person
#: could empty the shelf by asking for everything and never being approved.
_HOLDS_STOCK = frozenset({RedemptionStatus.APPROVED, RedemptionStatus.FULFILLED})


def _may_manage(caller: CurrentUser) -> bool:
    """HR and admins publish rewards and decide redemptions."""
    return caller.role in (Role.ADMIN, Role.HR)


class RewardService:
    def __init__(
        self,
        rewards: RewardRepository,
        recognitions: RecognitionRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._rewards = rewards
        self._recognitions = recognitions
        self._employees = employees
        self._audit = audit

    # --- balances ---------------------------------------------------------- #
    async def balance(self, caller: CurrentUser, employee_id: uuid.UUID) -> PointsBalance:
        """What one person has earned, committed and can still spend.

        Anyone may read anyone's: the feed that produced these points is
        org-wide already, and a leaderboard nobody can check is not one.
        """
        earned = await self._recognitions.points_earned(employee_id)
        spent = await self._rewards.points_spent_by(employee_id)
        given = await self._recognitions.peer_awards_given_since(employee_id, month_start())
        return PointsBalance(
            employee_id=employee_id,
            earned=earned,
            spent=spent,
            available=earned - spent,
            peer_awards_left=max(0, PEER_AWARDS_PER_MONTH - given),
        )

    async def standings(
        self, caller: CurrentUser, *, this_month: bool = False
    ) -> list[StandingRead]:
        """The wall: who has been recognised most, all time or this month."""
        del caller  # org-wide, like the feed it is built from
        rows = await self._recognitions.standings(
            since=month_start() if this_month else None, limit=20
        )
        people = await self._employees.get_many([r.employee_id for r in rows])
        return [
            StandingRead(
                employee_id=row.employee_id,
                employee_name=person.full_name if person else None,
                employee_avatar_url=getattr(person, "avatar_url", None),
                department=person.department if person else None,
                points=row.points,
                awards=row.awards,
            )
            for row in rows
            for person in (people.get(row.employee_id),)
        ]

    async def whiteboard(self, caller: CurrentUser, *, period: str = "month") -> Whiteboard:
        """Recognition by department, and which one is holding the period.

        Ranked by AWARD COUNT rather than points, unlike the individual wall:
        the question a whiteboard answers is "who is recognising each other
        most", and one big badge should not win a department the week.

        The leader is decided HERE, not in the UI, so the dashboard, the page
        and anything added later all crown the same department. A tie leaves
        nobody leading - a shared first place is not a win, and quietly
        picking one by name would be inventing a result.
        """
        del caller  # org-wide, like the feed it is built from
        since = week_start() if period == "week" else month_start() if period == "month" else None
        rows = await self._recognitions.department_standings(since=since)
        top = rows[0].awards if rows else 0
        outright = top > 0 and sum(1 for r in rows if r.awards == top) == 1
        return Whiteboard(
            period=period,
            departments=[
                DepartmentStandingRead(
                    department=row.department,
                    points=row.points,
                    awards=row.awards,
                    people=row.people,
                    leading=outright and row.awards == top,
                )
                for row in rows
            ],
        )

    # --- the catalogue ----------------------------------------------------- #
    async def catalogue(self, caller: CurrentUser) -> list[RewardItemRead]:
        """What is on offer, and what this caller can afford right now."""
        items = await self._rewards.items(include_retired=_may_manage(caller))
        available = (await self.balance(caller, caller.employee_id)).available
        return [await self._item_read(item, available) for item in items]

    async def _available_to(self, caller: CurrentUser) -> int:
        """The caller's spendable points.

        `affordable` is answered for whoever is looking, including on the row
        returned when HR publishes one: hard-coding zero there made every
        reward HR had just created appear out of their own reach.
        """
        return (await self.balance(caller, caller.employee_id)).available

    async def _item_read(self, item: RewardItem, available: int) -> RewardItemRead:
        remaining: int | None = None
        if item.stock is not None:
            taken = await self._rewards.approved_count_for_item(item.id)
            remaining = max(0, item.stock - taken)
        return RewardItemRead.model_validate(item).model_copy(
            update={
                "remaining": remaining,
                "affordable": available >= item.points_cost
                and item.is_active
                and (remaining is None or remaining > 0),
            }
        )

    async def add_item(self, caller: CurrentUser, payload: RewardItemCreate) -> RewardItemRead:
        if not _may_manage(caller):
            raise AuthorizationError()
        item = await self._rewards.add_item(
            title=payload.title,
            description=payload.description,
            points_cost=payload.points_cost,
            stock=payload.stock,
            created_by=caller.employee_id,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reward.item_added",
            target=f"reward_item:{item.id}:{payload.points_cost}",
        )
        return await self._item_read(item, await self._available_to(caller))

    async def update_item(
        self, caller: CurrentUser, item_id: uuid.UUID, payload: RewardItemUpdate
    ) -> RewardItemRead:
        """Change what is on offer. Never touches what people already redeemed:
        those rows carry their own title and cost."""
        if not _may_manage(caller):
            raise AuthorizationError()
        item = await self._rewards.get_item(item_id)
        if item is None:
            raise NotFoundError()
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(item, field, value)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reward.item_updated",
            target=f"reward_item:{item.id}",
        )
        return await self._item_read(item, await self._available_to(caller))

    # --- spending ---------------------------------------------------------- #
    async def redeem(self, caller: CurrentUser, item_id: uuid.UUID) -> RedemptionRead:
        """Ask for a reward. Costs points the moment it is asked for.

        REQUESTED already counts against the balance: holding the points is
        what stops the same 500 being spent on four things while HR works
        through the queue. Declining or cancelling gives them straight back.
        """
        item = await self._rewards.get_item(item_id)
        if item is None or not item.is_active:
            raise NotFoundError()

        # Before the balance is read, not after. Two requests arriving together
        # would otherwise both read the same available points, both pass, and
        # the person would spend the same 50 twice.
        await self._rewards.lock_spender(caller.employee_id)

        remaining = await self._remaining(item)
        if remaining is not None and remaining <= 0:
            raise ConflictError("That reward is out of stock.")
        balance = await self.balance(caller, caller.employee_id)
        if balance.available < item.points_cost:
            raise ConflictError(
                f"That costs {item.points_cost} points and you have {balance.available}."
            )

        row = await self._rewards.add_redemption(
            employee_id=caller.employee_id,
            item_id=item.id,
            # Snapshots: retiring or repricing the item must not rewrite this.
            item_title=item.title,
            points_spent=item.points_cost,
            status=RedemptionStatus.REQUESTED,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reward.redeem",
            target=f"reward_redemption:{row.id}:{item.points_cost}",
        )
        return await self._redemption_read(caller, row)

    async def decide(
        self, caller: CurrentUser, redemption_id: uuid.UUID, payload: RedemptionDecision
    ) -> RedemptionRead:
        """HR approves, declines or marks a redemption delivered."""
        if not _may_manage(caller):
            raise AuthorizationError()
        row = await self._rewards.get_redemption(redemption_id)
        if row is None:
            raise NotFoundError()
        if row.status in (RedemptionStatus.DECLINED, RedemptionStatus.CANCELLED):
            raise ConflictError("That redemption was already closed.")
        if row.status is RedemptionStatus.FULFILLED:
            raise ConflictError("That redemption was already fulfilled.")

        if payload.status in _HOLDS_STOCK and row.status is RedemptionStatus.REQUESTED:
            # Same race, one table along: two approvals of the last item would
            # both read "one left" and both take it.
            if row.item_id is not None:
                await self._rewards.lock_item(row.item_id)
            await self._check_still_affordable(row)
            await self._check_stock(row)

        row.status = payload.status
        row.note = payload.note
        row.decided_by = caller.employee_id
        row.decided_at = datetime.now(UTC)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reward.decide",
            target=f"reward_redemption:{row.id}:{payload.status.value}",
        )
        return await self._redemption_read(caller, row)

    async def _check_still_affordable(self, row: RewardRedemption) -> None:
        """Re-check at approval, not just at request.

        Minutes pass between asking and deciding, and an unaward in between
        can take the points away. Approving on the request-time balance is how
        a balance goes negative.
        """
        earned = await self._recognitions.points_earned(row.employee_id)
        spent = await self._rewards.points_spent_by(row.employee_id)
        # This row's own points are already inside `spent`; a balance of
        # exactly zero is therefore correct and fine.
        if earned - spent < 0:
            raise ConflictError(
                "They no longer have the points for this. Decline it, or wait "
                "until they have earned them back."
            )

    async def _check_stock(self, row: RewardRedemption) -> None:
        if row.item_id is None:
            return
        item = await self._rewards.get_item(row.item_id)
        if item is None:
            return
        remaining = await self._remaining(item)
        if remaining is not None and remaining <= 0:
            raise ConflictError("That reward is out of stock.")

    async def _remaining(self, item: RewardItem) -> int | None:
        if item.stock is None:
            return None
        taken = await self._rewards.approved_count_for_item(item.id)
        return max(0, item.stock - taken)

    async def cancel(self, caller: CurrentUser, redemption_id: uuid.UUID) -> RedemptionRead:
        """Withdraw your own request while nobody has acted on it."""
        row = await self._rewards.get_redemption(redemption_id)
        if row is None or row.employee_id != caller.employee_id:
            raise NotFoundError()
        if row.status not in _CANCELLABLE:
            raise ConflictError("That request has already been decided.")
        row.status = RedemptionStatus.CANCELLED
        row.decided_at = datetime.now(UTC)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="reward.cancel",
            target=f"reward_redemption:{row.id}",
        )
        return await self._redemption_read(caller, row)

    async def my_redemptions(self, caller: CurrentUser) -> list[RedemptionRead]:
        rows = await self._rewards.redemptions(employee_id=caller.employee_id)
        return await self.reads_for(caller, rows)

    async def queue(self, caller: CurrentUser) -> list[RedemptionRead]:
        """Everything waiting on HR."""
        if not _may_manage(caller):
            raise AuthorizationError()
        rows = await self._rewards.redemptions(status=RedemptionStatus.REQUESTED)
        return await self.reads_for(caller, rows)

    async def _redemption_read(self, caller: CurrentUser, row: RewardRedemption) -> RedemptionRead:
        people = await self._employees.get_many(
            [i for i in (row.employee_id, row.decided_by) if i is not None]
        )
        return self._to_read(caller, row, people)

    @staticmethod
    def _to_read(
        caller: CurrentUser, row: RewardRedemption, people: dict[uuid.UUID, Employee]
    ) -> RedemptionRead:
        who = people.get(row.employee_id)
        decider = people.get(row.decided_by) if row.decided_by else None
        return RedemptionRead.model_validate(row).model_copy(
            update={
                "employee_name": who.full_name if who else None,
                "decided_by_name": decider.full_name if decider else None,
                "can_cancel": row.employee_id == caller.employee_id and row.status in _CANCELLABLE,
            }
        )

    async def reads_for(
        self, caller: CurrentUser, rows: Sequence[RewardRedemption]
    ) -> list[RedemptionRead]:
        """Several redemptions with their names resolved in ONE lookup."""
        ids = {i for row in rows for i in (row.employee_id, row.decided_by) if i is not None}
        people = await self._employees.get_many(list(ids))
        return [self._to_read(caller, row, people) for row in rows]
