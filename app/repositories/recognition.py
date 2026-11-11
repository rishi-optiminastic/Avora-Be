"""Recognition data access — the feed, one award, and its comment thread."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.recognition import (
    Recognition,
    RecognitionBadge,
    RecognitionComment,
    RecognitionSource,
)

MAX_PAGE = 100


@dataclass(frozen=True)
class DepartmentStanding:
    """One department's place on the whiteboard.

    `people` is how many DIFFERENT people were recognised, which is the number
    that says whether a department is actually recognising each other or one
    person is collecting badges.
    """

    department: str
    points: int
    awards: int
    people: int


@dataclass(frozen=True)
class Standing:
    """One person's place on the board: what they have earned, and how often."""

    employee_id: uuid.UUID
    points: int
    awards: int


class RecognitionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        employee_id: uuid.UUID,
        awarded_by_id: uuid.UUID,
        badge: RecognitionBadge,
        note: str,
        source: RecognitionSource,
        points: int,
    ) -> Recognition:
        row = Recognition(
            employee_id=employee_id,
            awarded_by_id=awarded_by_id,
            badge=badge,
            note=note,
            source=source,
            points=points,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def peer_awards_given_since(self, awarder_id: uuid.UUID, since: datetime) -> int:
        """How many PEER awards this person has given since `since`.

        Only peer ones are counted. A manager recognising their own reports is
        the job, is already visible org-wide, and is not rationed.
        """
        total = await self._session.scalar(
            select(func.count())
            .select_from(Recognition)
            .where(
                Recognition.awarded_by_id == awarder_id,
                Recognition.source == RecognitionSource.PEER,
                Recognition.created_at >= since,
            )
        )
        return int(total or 0)

    async def points_earned(self, employee_id: uuid.UUID) -> int:
        """Everything this person has ever been awarded, in points."""
        total = await self._session.scalar(
            select(func.coalesce(func.sum(Recognition.points), 0)).where(
                Recognition.employee_id == employee_id
            )
        )
        return int(total or 0)

    async def standings(self, *, since: datetime | None = None, limit: int = 20) -> list[Standing]:
        """Points and award count per person, most points first.

        One query for the whole board - a per-person total would be an N+1 on
        a page whose only job is showing everyone at once.
        """
        query = (
            select(
                Recognition.employee_id,
                func.coalesce(func.sum(Recognition.points), 0).label("points"),
                func.count().label("awards"),
            )
            .group_by(Recognition.employee_id)
            .order_by(func.coalesce(func.sum(Recognition.points), 0).desc())
            .limit(min(limit, MAX_PAGE))
        )
        if since is not None:
            query = query.where(Recognition.created_at >= since)
        rows = await self._session.execute(query)
        return [
            Standing(employee_id=r.employee_id, points=int(r.points), awards=int(r.awards))
            for r in rows.all()
        ]

    async def department_standings(
        self, *, since: datetime | None = None
    ) -> list[DepartmentStanding]:
        """Recognition per department, most awards first.

        Ranked by AWARD COUNT, not points - unlike the individual wall. The
        whiteboard asks "who is recognising each other most", and a single
        high-value badge should not win a department the week. Points ride
        along as the tiebreak and the detail.

        Joined to `employees.department`, which is free text: a row with no
        department is left out rather than bucketed under an invented name.
        """
        query = (
            select(
                Employee.department,
                func.coalesce(func.sum(Recognition.points), 0).label("points"),
                func.count().label("awards"),
                func.count(func.distinct(Recognition.employee_id)).label("people"),
            )
            .join(Employee, Employee.id == Recognition.employee_id)
            .where(Employee.department.is_not(None))
            .group_by(Employee.department)
            .order_by(func.count().desc(), func.coalesce(func.sum(Recognition.points), 0).desc())
        )
        if since is not None:
            query = query.where(Recognition.created_at >= since)
        rows = await self._session.execute(query)
        return [
            DepartmentStanding(
                department=r.department,
                points=int(r.points),
                awards=int(r.awards),
                people=int(r.people),
            )
            for r in rows.all()
        ]

    async def get(self, recognition_id: uuid.UUID) -> Recognition | None:
        return await self._session.get(Recognition, recognition_id)

    async def feed(
        self,
        *,
        employee_id: uuid.UUID | None = None,
        badge: RecognitionBadge | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Recognition]:
        """Newest first. The feed is org-wide on purpose - recognition nobody
        else can see is a private message, not a programme."""
        stmt = select(Recognition)
        if employee_id is not None:
            stmt = stmt.where(Recognition.employee_id == employee_id)
        if badge is not None:
            stmt = stmt.where(Recognition.badge == badge)
        stmt = (
            stmt.order_by(Recognition.created_at.desc())
            .limit(min(limit, MAX_PAGE))
            .offset(max(0, offset))
        )
        rows = await self._session.execute(stmt)
        return rows.scalars().all()

    async def delete(self, row: Recognition) -> None:
        await self._session.delete(row)
        await self._session.flush()

    # --- comments ----------------------------------------------------------- #

    async def add_comment(
        self, *, recognition_id: uuid.UUID, author_id: uuid.UUID, body: str
    ) -> RecognitionComment:
        comment = RecognitionComment(recognition_id=recognition_id, author_id=author_id, body=body)
        self._session.add(comment)
        await self._session.flush()
        return comment

    async def get_comment(self, comment_id: uuid.UUID) -> RecognitionComment | None:
        return await self._session.get(RecognitionComment, comment_id)

    async def comments_for(
        self, recognition_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[RecognitionComment]]:
        """Every thread for a page of the feed in one query, oldest first, so
        rendering the feed is two round trips rather than one per card."""
        if not recognition_ids:
            return {}
        rows = await self._session.execute(
            select(RecognitionComment)
            .where(RecognitionComment.recognition_id.in_(recognition_ids))
            .order_by(RecognitionComment.created_at.asc())
        )
        out: dict[uuid.UUID, list[RecognitionComment]] = {}
        for comment in rows.scalars().all():
            out.setdefault(comment.recognition_id, []).append(comment)
        return out

    async def delete_comment(self, comment: RecognitionComment) -> None:
        await self._session.delete(comment)
        await self._session.flush()
