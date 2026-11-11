"""Reward endpoints - the points wall, the catalogue, and spending.

Reads are org-wide on purpose: the recognition feed that produced these points
already is, and a leaderboard nobody can check is not a leaderboard. Writing
is scoped in the service - `RewardService._may_manage` holds the rule for the
catalogue and for deciding redemptions.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUserDep, RewardServiceDep
from app.schemas.reward import (
    PointsBalance,
    RedemptionCreate,
    RedemptionDecision,
    RedemptionRead,
    RewardItemCreate,
    RewardItemRead,
    RewardItemUpdate,
    StandingRead,
    Whiteboard,
)

router = APIRouter(prefix="/rewards", tags=["rewards"])


@router.get("/me/balance", response_model=PointsBalance)
async def my_balance(caller: CurrentUserDep, service: RewardServiceDep) -> PointsBalance:
    """What I have earned, committed, and can still spend - plus how many peer
    recognitions I have left to give this month."""
    return await service.balance(caller, caller.employee_id)


@router.get("/balance/{employee_id}", response_model=PointsBalance)
async def balance_for(
    employee_id: uuid.UUID, caller: CurrentUserDep, service: RewardServiceDep
) -> PointsBalance:
    """Anyone's balance. The feed behind it is org-wide already."""
    return await service.balance(caller, employee_id)


@router.get("/standings", response_model=list[StandingRead])
async def standings(
    caller: CurrentUserDep,
    service: RewardServiceDep,
    this_month: Annotated[bool, Query(description="Only awards since the 1st.")] = False,
) -> list[StandingRead]:
    """The wall: who has been recognised most, all time or this month."""
    return await service.standings(caller, this_month=this_month)


@router.get("/whiteboard", response_model=Whiteboard)
async def whiteboard(
    caller: CurrentUserDep,
    service: RewardServiceDep,
    period: Annotated[
        Literal["week", "month", "all"], Query(description="The window to rank over.")
    ] = "month",
) -> Whiteboard:
    """Recognition by department, and which one is holding the period.

    Ranked by award COUNT, not points: the question is who is recognising
    each other most, and one big badge should not win a department the week.
    The leader is decided server-side so every screen crowns the same one; a
    tie leaves nobody leading.
    """
    return await service.whiteboard(caller, period=period)


@router.get("/catalogue", response_model=list[RewardItemRead])
async def catalogue(caller: CurrentUserDep, service: RewardServiceDep) -> list[RewardItemRead]:
    """What points buy, cheapest first, each marked affordable or not for the
    caller. HR and admins also see retired items."""
    return await service.catalogue(caller)


@router.post("/catalogue", response_model=RewardItemRead, status_code=status.HTTP_201_CREATED)
async def add_item(
    payload: RewardItemCreate, caller: CurrentUserDep, service: RewardServiceDep
) -> RewardItemRead:
    """Publish a reward. HR and admins only."""
    return await service.add_item(caller, payload)


@router.patch("/catalogue/{item_id}", response_model=RewardItemRead)
async def update_item(
    item_id: uuid.UUID,
    payload: RewardItemUpdate,
    caller: CurrentUserDep,
    service: RewardServiceDep,
) -> RewardItemRead:
    """Reprice, restock or retire a reward. HR and admins only.

    Never rewrites what anyone already redeemed: those rows carry their own
    title and cost.
    """
    return await service.update_item(caller, item_id, payload)


@router.post("/redemptions", response_model=RedemptionRead, status_code=status.HTTP_201_CREATED)
async def redeem(
    payload: RedemptionCreate, caller: CurrentUserDep, service: RewardServiceDep
) -> RedemptionRead:
    """Spend points on a reward.

    409 when the points are short or the reward is out of stock. The points
    are held from this moment so the same balance cannot be spent twice while
    HR works through the queue.
    """
    return await service.redeem(caller, payload.item_id)


@router.get("/redemptions/me", response_model=list[RedemptionRead])
async def my_redemptions(caller: CurrentUserDep, service: RewardServiceDep) -> list[RedemptionRead]:
    return await service.my_redemptions(caller)


@router.get("/redemptions/queue", response_model=list[RedemptionRead])
async def queue(caller: CurrentUserDep, service: RewardServiceDep) -> list[RedemptionRead]:
    """Everything waiting on a decision. HR and admins only."""
    return await service.queue(caller)


@router.post("/redemptions/{redemption_id}/decision", response_model=RedemptionRead)
async def decide(
    redemption_id: uuid.UUID,
    payload: RedemptionDecision,
    caller: CurrentUserDep,
    service: RewardServiceDep,
) -> RedemptionRead:
    """Approve, decline or mark delivered. HR and admins only.

    The balance is re-checked here, not just when the request was made: an
    unaward in between can take the points away.
    """
    return await service.decide(caller, redemption_id, payload)


@router.post("/redemptions/{redemption_id}/cancel", response_model=RedemptionRead)
async def cancel(
    redemption_id: uuid.UUID, caller: CurrentUserDep, service: RewardServiceDep
) -> RedemptionRead:
    """Withdraw your own request, while nobody has acted on it. The points
    come straight back."""
    return await service.cancel(caller, redemption_id)
