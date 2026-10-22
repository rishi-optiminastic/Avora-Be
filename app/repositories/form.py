"""Form data access - the only place these queries are built.

Listings join in names (assignee, subject, respondent) in one query each, so
nothing loops per row (no N+1).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, Row, Select, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.exceptions import ConflictError
from app.models.employee import Employee
from app.models.form import (
    AssignmentStatus,
    Form,
    FormAnswer,
    FormAssignment,
    FormField,
    FormFieldType,
    FormResponse,
    FormStatus,
    FormTrigger,
)

# An assignment plus its form title, assignee name and subject name. The names
# are NULL at runtime when nobody is set (a buddy form still waiting for a buddy).
type AssignmentRow = Row[tuple[FormAssignment, str, str, str]]
# A response plus respondent name, its assignment's subject id and subject name.
type ResponseRow = Row[tuple[FormResponse, str, uuid.UUID | None, str]]

_Assignee = aliased(Employee, name="assignee")
# Waiting for someone to be on file before it can go out.
_WAITING = (AssignmentStatus.NEEDS_BUDDY, AssignmentStatus.NEEDS_MANAGER)
_Subject = aliased(Employee, name="subject")


class FormRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- forms ------------------------------------------------------------ #
    async def add(self, **values: Any) -> Form:
        form = Form(**values)
        self._session.add(form)
        await self._session.flush()
        return form

    async def get(self, form_id: uuid.UUID) -> Form | None:
        return await self._session.get(Form, form_id)

    async def get_for_update(self, form_id: uuid.UUID) -> Form | None:
        """The form, row-locked until commit, so two requests changing it
        (two Publish clicks) run one after the other."""
        result = await self._session.execute(
            select(Form).where(Form.id == form_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def add_unique(self, conflict: str, **values: Any) -> Form:
        """Add a form; a uniqueness clash (a racing request already made it)
        becomes a 409 with `conflict` as the message, not a 500."""
        form = Form(**values)
        self._session.add(form)
        await self.flush_unique(conflict)
        return form

    async def flush(self) -> None:
        await self._session.flush()

    async def flush_unique(self, conflict: str) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError(conflict) from exc

    async def list(self, *, include_drafts: bool, include_archived: bool) -> Sequence[Form]:
        statuses = [FormStatus.PUBLISHED]
        if include_drafts:
            statuses.append(FormStatus.DRAFT)
        if include_archived:
            statuses.append(FormStatus.ARCHIVED)
        rows = await self._session.execute(
            select(Form).where(Form.status.in_(statuses)).order_by(Form.updated_at.desc())
        )
        return rows.scalars().all()

    async def delete(self, form: Form) -> None:
        await self._session.delete(form)
        await self._session.flush()

    async def next_version(self, family_id: uuid.UUID) -> int:
        current = await self._session.scalar(
            select(func.max(Form.version)).where(Form.family_id == family_id)
        )
        return int(current or 0) + 1

    async def get_many(self, form_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Form]:
        if not form_ids:
            return {}
        rows = await self._session.execute(select(Form).where(Form.id.in_(set(form_ids))))
        return {form.id: form for form in rows.scalars().all()}

    async def live_triggers(self, family_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, FormTrigger]:
        """For each form, the trigger its published version sends by (absent
        when no version is published)."""
        if not family_ids:
            return {}
        rows = await self._session.execute(
            select(Form.family_id, Form.trigger).where(
                Form.family_id.in_(set(family_ids)), Form.status == FormStatus.PUBLISHED
            )
        )
        return dict(rows.tuples().all())

    async def in_family(self, family_id: uuid.UUID, status: FormStatus) -> Sequence[Form]:
        rows = await self._session.execute(
            select(Form).where(Form.family_id == family_id, Form.status == status)
        )
        return rows.scalars().all()

    async def published_for_trigger(self, trigger: FormTrigger) -> Sequence[Form]:
        rows = await self._session.execute(
            select(Form).where(Form.status == FormStatus.PUBLISHED, Form.trigger == trigger)
        )
        return rows.scalars().all()

    # --- fields ----------------------------------------------------------- #
    async def replace_fields(self, form: Form, fields: Sequence[dict[str, Any]]) -> None:
        """Only ever called on a draft: a draft has no answers to orphan."""
        for existing in await self.fields_of(form.id):
            await self._session.delete(existing)
        await self._session.flush()
        for position, values in enumerate(fields):
            self._session.add(FormField(form_id=form.id, position=position, **values))
        await self._session.flush()

    async def fields_of(self, form_id: uuid.UUID) -> Sequence[FormField]:
        rows = await self._session.execute(
            select(FormField).where(FormField.form_id == form_id).order_by(FormField.position)
        )
        return rows.scalars().all()

    async def question_counts(self, form_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not form_ids:
            return {}
        rows = await self._session.execute(
            select(FormField.form_id, func.count(FormField.id))
            .where(FormField.form_id.in_(form_ids), FormField.type != FormFieldType.SECTION)
            .group_by(FormField.form_id)
        )
        return {form_id: int(count) for form_id, count in rows.all()}

    # --- counts for HR ---------------------------------------------------- #
    async def assignment_counts(
        self, form_ids: Sequence[uuid.UUID]
    ) -> dict[tuple[uuid.UUID, AssignmentStatus], int]:
        if not form_ids:
            return {}
        rows = await self._session.execute(
            select(FormAssignment.form_id, FormAssignment.status, func.count(FormAssignment.id))
            .where(FormAssignment.form_id.in_(form_ids))
            .group_by(FormAssignment.form_id, FormAssignment.status)
        )
        return {(form_id, status): int(count) for form_id, status, count in rows.all()}

    async def cancel_unsent(self, family_id: uuid.UUID, trigger: FormTrigger | None = None) -> None:
        """Cancel the assignments of any version of this form that nobody has
        been told about yet (only those made by `trigger`, when given)."""
        versions = select(Form.id).where(Form.family_id == family_id)
        conditions: list[ColumnElement[bool]] = [FormAssignment.form_id.in_(versions)]
        if trigger is not None:
            conditions.append(FormAssignment.trigger == trigger)
        await self._session.execute(
            update(FormAssignment)
            .where(
                *conditions,
                or_(
                    FormAssignment.status.in_(_WAITING),
                    and_(
                        FormAssignment.status == AssignmentStatus.PENDING,
                        FormAssignment.notified_at.is_(None),
                    ),
                ),
            )
            .values(status=AssignmentStatus.CANCELLED)
            .execution_options(synchronize_session="fetch")
        )


class FormAssignmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _named(self) -> Select[tuple[FormAssignment, str, str, str]]:
        return (
            select(FormAssignment, Form.title, _Assignee.full_name, _Subject.full_name)
            .join(Form, FormAssignment.form_id == Form.id)
            .outerjoin(_Assignee, FormAssignment.assignee_id == _Assignee.id)
            .outerjoin(_Subject, FormAssignment.subject_id == _Subject.id)
        )

    async def add(self, **values: Any) -> FormAssignment:
        assignment = FormAssignment(**values)
        self._session.add(assignment)
        await self._session.flush()
        return assignment

    async def get(self, assignment_id: uuid.UUID) -> FormAssignment | None:
        return await self._session.get(FormAssignment, assignment_id)

    async def assignment_ids_for(
        self, form_id: uuid.UUID, assignee_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """This person's live assignments for this form, newest first. Lets a
        page link straight to "your copy" instead of a list to search."""
        rows = await self._session.scalars(
            select(FormAssignment.id)
            .where(
                FormAssignment.form_id == form_id,
                FormAssignment.assignee_id == assignee_id,
                FormAssignment.status != AssignmentStatus.CANCELLED,
            )
            .order_by(FormAssignment.created_at.desc())
        )
        return list(rows.all())

    async def assignee_ids_for_form(self, form_id: uuid.UUID) -> set[uuid.UUID]:
        """Everyone who already holds this form, so a re-send tops up rather
        than handing somebody a second copy.

        CANCELLED assignments are excluded on purpose: a cancelled form is one
        HR explicitly took back, and sending it again is a deliberate act.
        """
        rows = await self._session.scalars(
            select(FormAssignment.assignee_id).where(
                FormAssignment.form_id == form_id,
                FormAssignment.assignee_id.is_not(None),
                FormAssignment.status != AssignmentStatus.CANCELLED,
            )
        )
        return {row for row in rows.all() if row is not None}

    async def get_for_update(
        self, assignment_id: uuid.UUID, *, skip_locked: bool = False
    ) -> FormAssignment | None:
        """Row-locked until commit. With `skip_locked`, None when someone else
        holds it (a second scheduler already mailing this person)."""
        result = await self._session.execute(
            select(FormAssignment)
            .where(FormAssignment.id == assignment_id)
            .with_for_update(skip_locked=skip_locked)
        )
        return result.scalar_one_or_none()

    async def add_many(self, rows: Sequence[dict[str, Any]]) -> None:
        self._session.add_all([FormAssignment(**values) for values in rows])
        await self._session.flush()

    async def unsent(self) -> Sequence[FormAssignment]:
        """Waiting for a buddy or manager, or pending and nobody told yet."""
        rows = await self._session.execute(
            select(FormAssignment).where(
                or_(
                    FormAssignment.status.in_(_WAITING),
                    and_(
                        FormAssignment.status == AssignmentStatus.PENDING,
                        FormAssignment.notified_at.is_(None),
                    ),
                )
            )
        )
        return rows.scalars().all()

    async def get_named(self, assignment_id: uuid.UUID) -> AssignmentRow | None:
        result = await self._session.execute(
            self._named().where(FormAssignment.id == assignment_id)
        )
        return result.first()

    async def for_assignee(self, employee_id: uuid.UUID, *, today: date) -> Sequence[AssignmentRow]:
        """What this person can see: opened, and still pending or done."""
        result = await self._session.execute(
            self._named()
            .where(
                FormAssignment.assignee_id == employee_id,
                FormAssignment.available_on <= today,
                FormAssignment.status.in_((AssignmentStatus.PENDING, AssignmentStatus.SUBMITTED)),
            )
            .order_by(FormAssignment.status, FormAssignment.available_on.desc())
        )
        return result.all()

    async def with_status(self, status: AssignmentStatus) -> Sequence[AssignmentRow]:
        result = await self._session.execute(
            self._named()
            .where(FormAssignment.status == status)
            .order_by(FormAssignment.available_on)
        )
        return result.all()

    async def for_form(self, form_id: uuid.UUID) -> Sequence[AssignmentRow]:
        result = await self._session.execute(
            self._named()
            .where(FormAssignment.form_id == form_id)
            .order_by(FormAssignment.created_at.desc())
        )
        return result.all()

    async def subjects_with(self, trigger: FormTrigger) -> set[uuid.UUID]:
        """Everyone who already has an automatic assignment for this trigger."""
        rows = await self._session.execute(
            select(FormAssignment.subject_id).where(FormAssignment.trigger == trigger)
        )
        return {subject for subject in rows.scalars().all() if subject is not None}

    async def raw_with_status(self, status: AssignmentStatus) -> Sequence[FormAssignment]:
        rows = await self._session.execute(
            select(FormAssignment).where(FormAssignment.status == status)
        )
        return rows.scalars().all()

    async def claim_unalerted(self, waiting: AssignmentStatus) -> Sequence[FormAssignment]:
        """Forms waiting (for a buddy, or a manager) that HR has not heard
        about, locked until commit; rows another process holds are skipped, so
        two runs can't both send HR the same alert."""
        rows = await self._session.execute(
            select(FormAssignment)
            .where(
                FormAssignment.status == waiting,
                FormAssignment.hr_alerted_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        return rows.scalars().all()

    async def unnotified(self, *, today: date) -> Sequence[FormAssignment]:
        rows = await self._session.execute(
            select(FormAssignment).where(
                FormAssignment.status == AssignmentStatus.PENDING,
                FormAssignment.notified_at.is_(None),
                # Someone to tell, or the HR pool (told as a group).
                or_(
                    FormAssignment.assignee_id.is_not(None),
                    FormAssignment.trigger == FormTrigger.PROBATION_HR,
                ),
                FormAssignment.available_on <= today,
            )
        )
        return rows.scalars().all()

    async def pool_for(self, employee_id: uuid.UUID, *, today: date) -> Sequence[AssignmentRow]:
        """Unclaimed HR probation forms open to this HR/admin person: every
        one except those about themselves."""
        result = await self._session.execute(
            self._named()
            .where(
                FormAssignment.trigger == FormTrigger.PROBATION_HR,
                FormAssignment.assignee_id.is_(None),
                FormAssignment.status == AssignmentStatus.PENDING,
                FormAssignment.available_on <= today,
                FormAssignment.subject_id != employee_id,
            )
            .order_by(FormAssignment.available_on.desc())
        )
        return result.all()

    async def for_subject(
        self, subject_id: uuid.UUID, triggers: Sequence[FormTrigger]
    ) -> Sequence[AssignmentRow]:
        """This person's automatic assignments for these triggers (at most one
        each), whatever their status."""
        result = await self._session.execute(
            self._named().where(
                FormAssignment.subject_id == subject_id, FormAssignment.trigger.in_(triggers)
            )
        )
        return result.all()

    async def flush(self) -> None:
        await self._session.flush()


class FormResponseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        assignment: FormAssignment,
        respondent_id: uuid.UUID,
        submitted_at: datetime,
        answers: Sequence[dict[str, Any]],
    ) -> FormResponse:
        response = FormResponse(
            assignment_id=assignment.id,
            form_id=assignment.form_id,
            respondent_id=respondent_id,
            submitted_at=submitted_at,
        )
        self._session.add(response)
        try:
            await self._session.flush()
        except IntegrityError as exc:  # a racing submit of the same form won
            raise ConflictError("This form has already been submitted.") from exc
        for values in answers:
            self._session.add(
                FormAnswer(response_id=response.id, answered_by_id=respondent_id, **values)
            )
        await self._session.flush()
        return response

    async def append(
        self,
        *,
        assignment: FormAssignment,
        respondent_id: uuid.UUID,
        submitted_at: datetime,
        answers: Sequence[dict[str, Any]],
    ) -> FormResponse:
        """Add one stage's answers, creating the response on the first stage.

        A two-stage form has ONE response with two authors, so the second
        submitter appends to the first's rather than starting a second
        response - `form_responses.assignment_id` is unique, and more to the
        point two half-responses would have to be stitched together by every
        reader forever.

        `submitted_at` is left at the FIRST stage's time and moved to the last
        one by the caller when the form is actually finished, so "submitted"
        keeps meaning "complete".
        """
        response = await self.for_assignment(assignment.id)
        if response is None:
            return await self.add(
                assignment=assignment,
                respondent_id=respondent_id,
                submitted_at=submitted_at,
                answers=answers,
            )
        for values in answers:
            self._session.add(
                FormAnswer(response_id=response.id, answered_by_id=respondent_id, **values)
            )
        # The person who FINISHES it is the respondent of record; the per-answer
        # `answered_by_id` keeps each half attributed to whoever wrote it.
        response.respondent_id = respondent_id
        response.submitted_at = submitted_at
        await self._session.flush()
        return response

    async def contributor_ids(self, assignment_id: uuid.UUID) -> set[uuid.UUID]:
        """Everyone who wrote any part of this assignment.

        On a two-stage form the first person stops being the assignee the
        moment they hand it on, so this is what keeps them able to see the half
        they wrote instead of a 404 the instant they submit it.
        """
        rows = await self._session.execute(
            select(FormAnswer.answered_by_id)
            .join(FormResponse, FormAnswer.response_id == FormResponse.id)
            .where(FormResponse.assignment_id == assignment_id)
        )
        return {row for (row,) in rows.all() if row is not None}

    async def for_assignment(self, assignment_id: uuid.UUID) -> FormResponse | None:
        result = await self._session.execute(
            select(FormResponse).where(FormResponse.assignment_id == assignment_id)
        )
        return result.scalar_one_or_none()

    async def for_assignments(
        self, assignment_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, FormResponse]:
        if not assignment_ids:
            return {}
        rows = await self._session.execute(
            select(FormResponse).where(FormResponse.assignment_id.in_(set(assignment_ids)))
        )
        return {r.assignment_id: r for r in rows.scalars().all()}

    async def for_form(self, form_id: uuid.UUID) -> Sequence[ResponseRow]:
        respondent = aliased(Employee, name="respondent")
        result = await self._session.execute(
            select(
                FormResponse, respondent.full_name, FormAssignment.subject_id, _Subject.full_name
            )
            .join(FormAssignment, FormResponse.assignment_id == FormAssignment.id)
            .outerjoin(respondent, FormResponse.respondent_id == respondent.id)
            .outerjoin(_Subject, FormAssignment.subject_id == _Subject.id)
            .where(FormResponse.form_id == form_id)
            .order_by(FormResponse.submitted_at.desc())
        )
        return result.all()

    async def answers_for(
        self, response_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[FormAnswer]]:
        if not response_ids:
            return {}
        rows = await self._session.execute(
            select(FormAnswer).where(FormAnswer.response_id.in_(response_ids))
        )
        grouped: dict[uuid.UUID, list[FormAnswer]] = {}
        for answer in rows.scalars().all():
            grouped.setdefault(answer.response_id, []).append(answer)
        return grouped
