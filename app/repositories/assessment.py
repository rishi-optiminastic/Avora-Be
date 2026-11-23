"""Assessment data access — the only place these queries are built."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentAttempt,
    AssessmentQuestion,
    AssessmentStatus,
    AttemptStatus,
)

MAX_PAGE = 200


@dataclass(frozen=True)
class AttemptCounts:
    """How many hold one assessment, and how many have sat it."""

    assigned: int
    submitted: int


class AssessmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- assessments ------------------------------------------------------- #
    async def add(self, **values: object) -> Assessment:
        row = Assessment(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, assessment_id: uuid.UUID) -> Assessment | None:
        return await self._session.get(Assessment, assessment_id)

    async def get_many(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Assessment]:
        """Batch lookup keyed by id - avoids a round trip per row when a
        history joins each attempt back to the test it was."""
        if not ids:
            return {}
        rows = await self._session.scalars(
            select(Assessment).where(Assessment.id.in_(list(dict.fromkeys(ids))))
        )
        return {row.id: row for row in rows}

    async def list_all(self, *, include_drafts: bool) -> Sequence[Assessment]:
        """Newest first. Drafts are HR's own working copies, so everyone else
        sees only what has been published."""
        query = select(Assessment).order_by(Assessment.created_at.desc())
        if not include_drafts:
            query = query.where(Assessment.status == AssessmentStatus.PUBLISHED)
        rows = await self._session.scalars(query.limit(MAX_PAGE))
        return list(rows)

    # --- questions --------------------------------------------------------- #
    async def add_question(self, **values: object) -> AssessmentQuestion:
        row = AssessmentQuestion(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def questions_for(self, assessment_id: uuid.UUID) -> Sequence[AssessmentQuestion]:
        rows = await self._session.scalars(
            select(AssessmentQuestion)
            .where(AssessmentQuestion.assessment_id == assessment_id)
            .order_by(AssessmentQuestion.position)
        )
        return list(rows)

    async def clear_questions(self, assessment_id: uuid.UUID) -> None:
        """Replace the paper wholesale. Only ever called on a DRAFT: the
        service refuses to touch a published test's questions, because people
        may already have sat it."""
        await self._session.execute(
            delete(AssessmentQuestion).where(AssessmentQuestion.assessment_id == assessment_id)
        )

    async def question_totals(
        self, assessment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """(count, total points) per assessment, in one query for the list."""
        if not assessment_ids:
            return {}
        rows = await self._session.execute(
            select(
                AssessmentQuestion.assessment_id,
                func.count(),
                func.coalesce(func.sum(AssessmentQuestion.points), 0),
            )
            .where(AssessmentQuestion.assessment_id.in_(assessment_ids))
            .group_by(AssessmentQuestion.assessment_id)
        )
        return {r[0]: (int(r[1]), int(r[2])) for r in rows.all()}

    async def unkeyed_counts(self, assessment_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """How many questions still have no answer key, per assessment.

        Counted in Python rather than in SQL: `correct` is a JSON column and
        "is this array empty" has no spelling that works on both Postgres and
        the SQLite the tests run against. Only the two columns are selected
        and a paper is capped at `MAX_QUESTIONS`, so the rows are cheap.
        """
        if not assessment_ids:
            return {}
        rows = await self._session.execute(
            select(AssessmentQuestion.assessment_id, AssessmentQuestion.correct).where(
                AssessmentQuestion.assessment_id.in_(assessment_ids)
            )
        )
        out: dict[uuid.UUID, int] = dict.fromkeys(assessment_ids, 0)
        for assessment_id, correct in rows.all():
            if not correct:
                out[assessment_id] = out.get(assessment_id, 0) + 1
        return out

    # --- attempts ---------------------------------------------------------- #
    async def add_attempt(self, **values: object) -> AssessmentAttempt:
        row = AssessmentAttempt(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_attempt(self, attempt_id: uuid.UUID) -> AssessmentAttempt | None:
        return await self._session.get(AssessmentAttempt, attempt_id)

    async def holders_of(self, assessment_id: uuid.UUID) -> set[uuid.UUID]:
        """Everyone who already has this test, so a re-assign tops up."""
        rows = await self._session.execute(
            select(AssessmentAttempt.employee_id).where(
                AssessmentAttempt.assessment_id == assessment_id
            )
        )
        return set(rows.scalars().all())

    async def attempts_for_assessment(
        self, assessment_id: uuid.UUID
    ) -> Sequence[AssessmentAttempt]:
        rows = await self._session.scalars(
            select(AssessmentAttempt)
            .where(AssessmentAttempt.assessment_id == assessment_id)
            .order_by(AssessmentAttempt.submitted_at.desc().nulls_last())
        )
        return list(rows)

    async def attempts_for_employee(self, employee_id: uuid.UUID) -> Sequence[AssessmentAttempt]:
        """Someone's history, newest sitting first, unsat ones at the top."""
        rows = await self._session.scalars(
            select(AssessmentAttempt)
            .where(AssessmentAttempt.employee_id == employee_id)
            .order_by(
                AssessmentAttempt.submitted_at.desc().nulls_first(),
                AssessmentAttempt.created_at.desc(),
            )
        )
        return list(rows)

    async def counts_for(
        self, assessment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, AttemptCounts]:
        """Held and sat per assessment, one query for the whole list."""
        if not assessment_ids:
            return {}
        rows = await self._session.execute(
            select(
                AssessmentAttempt.assessment_id,
                func.count(),
                func.count()
                .filter(AssessmentAttempt.status == AttemptStatus.SUBMITTED)
                .label("submitted"),
            )
            .where(AssessmentAttempt.assessment_id.in_(assessment_ids))
            .group_by(AssessmentAttempt.assessment_id)
        )
        return {r[0]: AttemptCounts(assigned=int(r[1]), submitted=int(r[2])) for r in rows.all()}

    # --- answers ----------------------------------------------------------- #
    async def add_answer(self, **values: object) -> AssessmentAnswer:
        row = AssessmentAnswer(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def answers_for(self, attempt_id: uuid.UUID) -> Sequence[AssessmentAnswer]:
        rows = await self._session.scalars(
            select(AssessmentAnswer).where(AssessmentAnswer.attempt_id == attempt_id)
        )
        return list(rows)

    async def lock_attempt(self, attempt_id: uuid.UUID) -> AssessmentAttempt | None:
        """Take the row lock AND return the freshly-read attempt.

        Returning it is the point. Locking by id alone leaves the caller
        holding whatever SQLAlchemy's identity map already had - so two
        submits arriving together would both lock in turn, and the second
        would still see its own stale ASSIGNED copy, mark the paper again,
        and hit the unique index on (attempt, question) as a 500 instead of
        the 409 it should be.

        `populate_existing` is what forces that refresh.
        """
        row: AssessmentAttempt | None = await self._session.scalar(
            select(AssessmentAttempt)
            .where(AssessmentAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return row

    async def flush(self) -> None:
        await self._session.flush()
