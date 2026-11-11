"""Reward request and response shapes."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.reward import RedemptionStatus


class RewardItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    # Free rewards are not rewards, and a negative cost would pay people to
    # redeem. The upper bound keeps a typo from making something unreachable.
    points_cost: int = Field(ge=1, le=100_000)
    stock: int | None = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Give the reward a name.")
        return trimmed


class RewardItemUpdate(BaseModel):
    """Every field optional: a PATCH changes only what it names."""

    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    points_cost: int | None = Field(default=None, ge=1, le=100_000)
    stock: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class RewardItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    points_cost: int
    is_active: bool
    stock: int | None
    #: What is left once approved redemptions are counted. None = unlimited.
    remaining: int | None = None
    #: Whether the caller has the points for it right now.
    affordable: bool = False


class RedemptionCreate(BaseModel):
    item_id: uuid.UUID


class RedemptionDecision(BaseModel):
    """HR approving, declining or marking a redemption delivered."""

    status: RedemptionStatus
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _decidable(cls, value: RedemptionStatus) -> RedemptionStatus:
        # REQUESTED is where a redemption starts, and CANCELLED belongs to the
        # person who asked - neither is a decision HR makes on their behalf.
        allowed = {
            RedemptionStatus.APPROVED,
            RedemptionStatus.DECLINED,
            RedemptionStatus.FULFILLED,
        }
        if value not in allowed:
            raise ValueError("A decision is approve, decline or fulfil.")
        return value


class RedemptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str | None = None
    item_id: uuid.UUID | None
    item_title: str
    points_spent: int
    status: RedemptionStatus
    note: str | None
    decided_by: uuid.UUID | None
    decided_by_name: str | None = None
    decided_at: datetime | None
    created_at: datetime
    can_cancel: bool = False


class PointsBalance(BaseModel):
    """What one person has earned, committed and can still spend."""

    employee_id: uuid.UUID
    earned: int
    spent: int
    available: int
    #: Peer awards this person has left to GIVE this calendar month.
    peer_awards_left: int


class StandingRead(BaseModel):
    """One row of the wall."""

    employee_id: uuid.UUID
    employee_name: str | None = None
    employee_avatar_url: str | None = None
    department: str | None = None
    points: int
    awards: int


class DepartmentStandingRead(BaseModel):
    """One department's row on the whiteboard."""

    department: str
    points: int
    awards: int
    #: How many different people were recognised - the number that says
    #: whether a department recognises each other or one person collects.
    people: int
    #: True for the department holding the period. Decided by the server so
    #: every screen crowns the same one.
    leading: bool = False


class Whiteboard(BaseModel):
    """Recognition by department, over one window.

    Carries the window it was built for, so a board that was fetched for the
    week cannot be captioned as the month by whatever renders it.
    """

    period: str
    departments: list[DepartmentStandingRead]
