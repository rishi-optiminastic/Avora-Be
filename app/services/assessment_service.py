"""CPD assessment business rules.

HR writes a test, assigns it to an audience, and it marks itself.

The rules that carry weight:

* **The answer key never reaches a taker.** `QuestionRead` has no `correct`
  field, and the only schema that does is HR's. A taker sees the key only in
  the review AFTER submitting, when it can no longer help them.
* **A published test's questions are frozen.** Editing the paper somebody is
  halfway through, or has already sat, would make two people's marks mean
  different things. Published tests can be retitled and archived; the
  questions can only change while it is a draft.
* **Marking happens once, and the result is stored.** `score`, `max_score`,
  `percentage` and `passed` are snapshotted onto the attempt, and each
  answer keeps its own `is_correct`. Editing a question or raising the pass
  mark afterwards changes what happens next, never what somebody scored.
* **One attempt per person per test.** Re-assigning tops up the people who
  do not hold it, rather than handing anybody a second copy.
* **You sit your own test and nobody else's.** HR and admins read every
  result; everyone else reads only their own.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TypeVar

from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from app.core.marking import mark
from app.models.assessment import (
    Assessment,
    AssessmentAttempt,
    AssessmentQuestion,
    AssessmentStatus,
    AttemptStatus,
)
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind
from app.repositories.assessment import AssessmentRepository, AttemptCounts
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.assessment import (
    AnswerResult,
    AssessmentDetail,
    AssessmentRead,
    AssessmentWrite,
    AssignmentResult,
    AssignmentWrite,
    AttemptDetail,
    AttemptRead,
    QuestionForAuthor,
    QuestionRead,
    SubmissionWrite,
)
from app.schemas.auth import CurrentUser
from app.services.audience import AudienceResolver
from app.services.notification_service import NotificationService

_LINK = "/dashboard/learning"

#: The two attempt shapes share every joined-in field, so `_attempt_read`
#: builds either one rather than duplicating the joins.
_Attempt = TypeVar("_Attempt", bound=AttemptRead)


def _may_manage(caller: CurrentUser) -> bool:
    """HR and admins write tests, assign them and read everyone's results."""
    return caller.role in (Role.ADMIN, Role.HR)


class AssessmentService:
    def __init__(
        self,
        assessments: AssessmentRepository,
        employees: EmployeeRepository,
        notifications: NotificationService,
        audit: AuditRepository,
    ) -> None:
        self._assessments = assessments
        self._employees = employees
        # Built here rather than injected, matching FormAssignmentService and
        # EventService: there is exactly one answer to "who does this reach"
        # and all three ask it the same way.
        self._audience = AudienceResolver(employees)
        self._notifications = notifications
        self._audit = audit

    # --- authoring --------------------------------------------------------- #
    async def create(self, caller: CurrentUser, payload: AssessmentWrite) -> AssessmentDetail:
        if not _may_manage(caller):
            raise AuthorizationError()
        row = await self._assessments.add(
            title=payload.title,
            description=payload.description,
            department=payload.department,
            pass_mark=payload.pass_mark,
            time_limit_minutes=payload.time_limit_minutes,
            created_by=caller.employee_id,
            status=AssessmentStatus.DRAFT,
        )
        await self._write_questions(row, payload)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="assessment.create",
            target=f"assessment:{row.id}",
        )
        return await self._detail(row)

    async def update(
        self, caller: CurrentUser, assessment_id: uuid.UUID, payload: AssessmentWrite
    ) -> AssessmentDetail:
        """Change a test. Questions only while it is a draft."""
        if not _may_manage(caller):
            raise AuthorizationError()
        row = await self._require(assessment_id)
        # Checked before anything is written. A rollback would undo the title
        # change too, but relying on one to keep a refusal clean is how a
        # half-applied update eventually ships.
        if payload.questions and row.status is not AssessmentStatus.DRAFT:
            raise ConflictError(
                "This test has been published, so its questions are fixed. "
                "Archive it and publish a new version instead."
            )

        row.title = payload.title
        row.description = payload.description
        row.department = payload.department
        row.pass_mark = payload.pass_mark
        row.time_limit_minutes = payload.time_limit_minutes

        # An EMPTY list means "leave the paper alone", which is what lets a
        # published test be retitled. Clearing every question is therefore
        # not expressible here - and deliberately so, since a test with no
        # questions cannot be published anyway.
        if payload.questions:
            await self._assessments.clear_questions(row.id)
            await self._write_questions(row, payload)

        await self._audit.append(
            actor=str(caller.employee_id),
            action="assessment.update",
            target=f"assessment:{row.id}",
        )
        return await self._detail(row)

    async def _write_questions(self, row: Assessment, payload: AssessmentWrite) -> None:
        for position, question in enumerate(payload.questions):
            await self._assessments.add_question(
                assessment_id=row.id,
                position=position,
                prompt=question.prompt.strip(),
                kind=question.kind,
                options=list(question.options),
                correct=sorted(set(question.correct)),
                points=question.points,
                competency=(question.competency or "").strip() or None,
            )

    async def publish(self, caller: CurrentUser, assessment_id: uuid.UUID) -> AssessmentDetail:
        """Make a test assignable. Refused while it has no questions - an
        empty paper marks everybody zero and passes nobody."""
        if not _may_manage(caller):
            raise AuthorizationError()
        row = await self._require(assessment_id)
        if row.status is AssessmentStatus.ARCHIVED:
            raise ConflictError("That test is archived. Copy it into a new one instead.")
        questions = await self._assessments.questions_for(row.id)
        if not questions:
            raise ConflictError("Add at least one question before publishing.")
        # A question with no key marks EVERYBODY wrong - `is_right` cannot
        # satisfy an empty key, by design - so publishing one would quietly
        # cost every taker a mark. A draft is allowed to be incomplete;
        # something people sit is not.
        unkeyed = [q.position + 1 for q in questions if not q.correct]
        if unkeyed:
            numbers = ", ".join(str(n) for n in unkeyed)
            raise ConflictError(
                "These questions have no correct answer marked yet, so nobody could "
                f"pass them: {numbers}. Set the answer key before publishing."
            )
        row.status = AssessmentStatus.PUBLISHED
        row.published_at = datetime.now(UTC)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="assessment.publish",
            target=f"assessment:{row.id}:{len(questions)}",
        )
        return await self._detail(row)

    async def archive(self, caller: CurrentUser, assessment_id: uuid.UUID) -> AssessmentDetail:
        """Stop it being assigned again. Never deleted: attempts reference it
        and somebody's record of having passed is worth keeping."""
        if not _may_manage(caller):
            raise AuthorizationError()
        row = await self._require(assessment_id)
        row.status = AssessmentStatus.ARCHIVED
        await self._audit.append(
            actor=str(caller.employee_id),
            action="assessment.archive",
            target=f"assessment:{row.id}",
        )
        return await self._detail(row)

    # --- assigning --------------------------------------------------------- #
    async def assign(
        self, caller: CurrentUser, assessment_id: uuid.UUID, payload: AssignmentWrite
    ) -> AssignmentResult:
        """Give a test to an audience: everyone, some departments, or named
        people. The same audience shape as events and form sends.

        Tops up rather than duplicating: the unique index allows one attempt
        per person per test, and re-assigning after a new joiner arrives is a
        normal thing to do.
        """
        if not _may_manage(caller):
            raise AuthorizationError()
        row = await self._require(assessment_id)
        if row.status is not AssessmentStatus.PUBLISHED:
            raise ConflictError("Publish the test before assigning it.")

        people = await self._audience.resolve(payload.audience)
        held = await self._assessments.holders_of(row.id)
        fresh = [p for p in people if p.id not in held]

        for person in fresh:
            attempt = await self._assessments.add_attempt(
                assessment_id=row.id,
                employee_id=person.id,
                assigned_by=caller.employee_id,
                due_on=payload.due_on,
                status=AttemptStatus.ASSIGNED,
            )
            await self._notifications.notify(
                recipient_id=person.id,
                kind=NotificationKind.SYSTEM,
                title=f"Assessment to complete: {row.title}",
                body=row.description[:200] if row.description else None,
                link=f"{_LINK}/{attempt.id}",
                entity_type="assessment_attempt",
                entity_id=attempt.id,
                actor_id=caller.employee_id,
            )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="assessment.assign",
            target=f"assessment:{row.id}:{len(fresh)}",
        )
        return AssignmentResult(assigned=len(fresh), already_held=len(people) - len(fresh))

    # --- sitting it -------------------------------------------------------- #
    async def open_attempt(self, caller: CurrentUser, attempt_id: uuid.UUID) -> AttemptDetail:
        """The paper, for whoever it belongs to.

        While unsubmitted this returns `questions` WITHOUT the answer key.
        Once submitted it returns `answers` instead, key included - at which
        point knowing it cannot change the mark.
        """
        attempt = await self._assessments.get_attempt(attempt_id)
        if attempt is None:
            raise NotFoundError()
        mine = attempt.employee_id == caller.employee_id
        if not mine and not _may_manage(caller):
            # 404 rather than 403: whose test it is, is not everyone's business.
            raise NotFoundError()
        assessment = await self._require(attempt.assessment_id)
        questions = await self._assessments.questions_for(assessment.id)

        if attempt.status is AttemptStatus.SUBMITTED:
            return await self._submitted_detail(attempt, assessment, questions)
        if not mine:
            # HR may see that it is outstanding, but not read the paper on
            # somebody's behalf - and especially not the key beside it.
            return self._attempt_read(attempt, assessment, AttemptDetail)
        detail = self._attempt_read(attempt, assessment, AttemptDetail)
        return detail.model_copy(
            update={
                "questions": [QuestionRead.model_validate(q) for q in questions],
                "description": assessment.description,
                "time_limit_minutes": assessment.time_limit_minutes,
            }
        )

    async def submit(
        self, caller: CurrentUser, attempt_id: uuid.UUID, payload: SubmissionWrite
    ) -> AttemptDetail:
        """Mark the paper, once.

        Only the person it belongs to may submit: HR marking on somebody's
        behalf would make the record meaningless, and the whole point of an
        auto-marked test is that nobody has to.
        """
        attempt = await self._assessments.get_attempt(attempt_id)
        if attempt is None or attempt.employee_id != caller.employee_id:
            raise NotFoundError()
        # Re-read UNDER the lock, not just locked. Two submits arriving
        # together both take the lock in turn, and the second would otherwise
        # still be holding its own stale ASSIGNED copy from the identity map -
        # marking the paper twice and hitting the unique index on
        # (attempt, question) as a 500 rather than this 409.
        locked = await self._assessments.lock_attempt(attempt.id)
        if locked is None:  # pragma: no cover - deleted between the two reads
            raise NotFoundError()
        attempt = locked
        if attempt.status is AttemptStatus.SUBMITTED:
            raise ConflictError("You have already submitted this assessment.")

        assessment = await self._require(attempt.assessment_id)
        questions = await self._assessments.questions_for(assessment.id)
        result = mark(
            [(q.id, q.options, q.correct, q.points) for q in questions],
            {a.question_id: a.chosen for a in payload.answers},
            pass_mark=assessment.pass_mark,
        )

        for one in result.marks:
            await self._assessments.add_answer(
                attempt_id=attempt.id,
                question_id=one.question_id,
                chosen=list(one.chosen),
                is_correct=one.is_correct,
                points_awarded=one.points_awarded,
            )
        attempt.status = AttemptStatus.SUBMITTED
        attempt.score = result.score
        attempt.max_score = result.max_score
        attempt.percentage = result.percentage
        attempt.passed = result.passed
        # Snapshotted with the verdict it produced. See the model.
        attempt.pass_mark = assessment.pass_mark
        attempt.submitted_at = datetime.now(UTC)
        await self._assessments.flush()

        await self._audit.append(
            actor=str(caller.employee_id),
            action="assessment.submit",
            target=f"assessment_attempt:{attempt.id}:{result.percentage}",
        )
        return await self._submitted_detail(attempt, assessment, questions)

    # --- reading ----------------------------------------------------------- #
    async def list_assessments(self, caller: CurrentUser) -> list[AssessmentRead]:
        manages = _may_manage(caller)
        rows = await self._assessments.list_all(include_drafts=manages)
        ids = [r.id for r in rows]
        totals = await self._assessments.question_totals(ids)
        counts = await self._assessments.counts_for(ids)
        # Only HR can act on an unkeyed draft, and only HR is shown drafts at
        # all, so nobody else pays for the query.
        unkeyed = await self._assessments.unkeyed_counts(ids) if manages else {}
        return [self._summary(r, totals, counts, unkeyed=unkeyed.get(r.id, 0)) for r in rows]

    async def detail(self, caller: CurrentUser, assessment_id: uuid.UUID) -> AssessmentDetail:
        """HR's view of a test, answer key included."""
        if not _may_manage(caller):
            raise NotFoundError()
        return await self._detail(await self._require(assessment_id))

    async def results(self, caller: CurrentUser, assessment_id: uuid.UUID) -> list[AttemptRead]:
        """Everyone's marks for one test. HR and admins only."""
        if not _may_manage(caller):
            raise NotFoundError()
        assessment = await self._require(assessment_id)
        attempts = await self._assessments.attempts_for_assessment(assessment.id)
        people = await self._employees.get_many([a.employee_id for a in attempts])
        return [
            self._attempt_read(a, assessment, AttemptRead, people.get(a.employee_id))
            for a in attempts
        ]

    async def history(self, caller: CurrentUser, employee_id: uuid.UUID) -> list[AttemptRead]:
        """One person's tests and marks, for their profile.

        Your own always; anyone else's only for HR and admins. A manager
        cannot read their report's marks here - a test result is closer to a
        payslip than to an attendance record.
        """
        if employee_id != caller.employee_id and not _may_manage(caller):
            raise NotFoundError()
        attempts = await self._assessments.attempts_for_employee(employee_id)
        # One query, not one per attempt: a profile with a year of CPD on it
        # was issuing a round trip per row just to read each test's title.
        assessments = await self._assessments.get_many([a.assessment_id for a in attempts])
        person = await self._employees.get(employee_id)
        return [
            self._attempt_read(a, assessments[a.assessment_id], AttemptRead, person)
            for a in attempts
            if assessments.get(a.assessment_id) is not None
        ]

    # --- shaping ----------------------------------------------------------- #
    async def _require(self, assessment_id: uuid.UUID) -> Assessment:
        row = await self._assessments.get(assessment_id)
        if row is None:
            raise NotFoundError()
        return row

    async def _detail(self, row: Assessment) -> AssessmentDetail:
        questions = await self._assessments.questions_for(row.id)
        totals = await self._assessments.question_totals([row.id])
        counts = await self._assessments.counts_for([row.id])
        summary = self._summary(
            row, totals, counts, unkeyed=sum(1 for q in questions if not q.correct)
        )
        return AssessmentDetail(
            **summary.model_dump(),
            questions=[QuestionForAuthor.model_validate(q) for q in questions],
        )

    @staticmethod
    def _summary(
        row: Assessment,
        totals: dict[uuid.UUID, tuple[int, int]],
        counts: dict[uuid.UUID, AttemptCounts],
        unkeyed: int = 0,
    ) -> AssessmentRead:
        count, points = totals.get(row.id, (0, 0))
        seen = counts.get(row.id)
        return AssessmentRead.model_validate(row).model_copy(
            update={
                "question_count": count,
                "total_points": points,
                "unkeyed_count": unkeyed,
                "assigned_count": seen.assigned if seen else 0,
                "submitted_count": seen.submitted if seen else 0,
            }
        )

    @staticmethod
    def _attempt_read(
        attempt: AssessmentAttempt,
        assessment: Assessment | None,
        model: type[_Attempt],
        person: Employee | None = None,
    ) -> _Attempt:
        """One attempt as either the row shape or the opened one.

        Generic over the two because the joined-in bits - the test's title,
        its pass mark, whose attempt it is - are identical, and writing them
        twice is how the history and the results table start disagreeing.
        """
        read: _Attempt = model.model_validate(attempt).model_copy(
            update={
                "assessment_title": assessment.title if assessment else "",
                # The one it was judged against, falling back to the live
                # one only while it has not been sat - where it is a preview
                # of the bar rather than a claim about a result.
                "pass_mark": attempt.pass_mark
                if attempt.pass_mark is not None
                else (assessment.pass_mark if assessment else 0),
                "employee_name": person.full_name if person else None,
            }
        )
        return read

    async def _submitted_detail(
        self,
        attempt: AssessmentAttempt,
        assessment: Assessment,
        questions: Sequence[AssessmentQuestion],
    ) -> AttemptDetail:
        """The review after marking: what they picked, what was right, why.

        The key appears HERE and nowhere earlier. Showing it now is the point
        of a CPD test - a mark with no explanation teaches nobody anything.
        """
        answers = {a.question_id: a for a in await self._assessments.answers_for(attempt.id)}
        person = await self._employees.get(attempt.employee_id)
        detail = self._attempt_read(attempt, assessment, AttemptDetail, person)
        return detail.model_copy(
            update={
                "description": assessment.description,
                "answers": [
                    AnswerResult(
                        question_id=q.id,
                        prompt=q.prompt,
                        competency=q.competency,
                        options=list(q.options),
                        chosen=list(answers[q.id].chosen) if q.id in answers else [],
                        is_correct=answers[q.id].is_correct if q.id in answers else False,
                        points_awarded=answers[q.id].points_awarded if q.id in answers else 0.0,
                        points=q.points,
                        correct=sorted(q.correct),
                    )
                    for q in questions
                ],
            }
        )
