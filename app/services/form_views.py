"""Turn form rows into response schemas. Shared by the form services so a form
looks the same whichever endpoint returns it."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.models.employee import Role
from app.models.form import Form, FormAnswer, FormField
from app.repositories.form import AssignmentRow
from app.schemas.auth import CurrentUser
from app.schemas.form import AnswerRead, AssignmentRead, FormFieldRead, FormRead, FormSummary


def can_manage_forms(caller: CurrentUser) -> bool:
    """HR and admins build forms, send them and read the answers."""
    return caller.role in (Role.ADMIN, Role.HR)


def to_summary(
    form: Form, *, question_count: int, response_count: int = 0, pending_count: int = 0
) -> FormSummary:
    return FormSummary(
        id=form.id,
        family_id=form.family_id,
        version=form.version,
        status=form.status,
        title=form.title,
        trigger=form.trigger,
        trigger_days=form.trigger_days,
        due_days=form.due_days,
        auto_assign_since=form.auto_assign_since,
        visibility=form.visibility,
        visible_departments=list(form.visible_departments or []),
        visible_employee_ids=[uuid.UUID(i) for i in (form.visible_employee_ids or [])],
        published_at=form.published_at,
        updated_at=form.updated_at,
        question_count=question_count,
        response_count=response_count,
        pending_count=pending_count,
    )


def to_field(field: FormField) -> FormFieldRead:
    return FormFieldRead(
        id=field.id,
        position=field.position,
        type=field.type,
        label=field.label,
        help_text=field.help_text,
        required=field.required,
        options=list(field.options or []),
        with_comment=field.with_comment,
        weight=field.weight,
    )


def to_read(form: Form, fields: Sequence[FormField], summary: FormSummary) -> FormRead:
    return FormRead(
        **summary.model_dump(),
        intro=form.intro,
        rating_labels=list(form.rating_labels or []),
        fields=[to_field(f) for f in fields],
    )


def to_assignment(row: AssignmentRow) -> AssignmentRead:
    assignment, form_title, assignee_name, subject_name = row
    return AssignmentRead(
        id=assignment.id,
        form_id=assignment.form_id,
        form_title=form_title,
        assignee_id=assignment.assignee_id,
        assignee_name=assignee_name,
        subject_id=assignment.subject_id,
        subject_name=subject_name,
        trigger=assignment.trigger,
        status=assignment.status,
        available_on=assignment.available_on,
        due_on=assignment.due_on,
        submitted_at=assignment.submitted_at,
    )


def to_answer(answer: FormAnswer) -> AnswerRead:
    return AnswerRead(
        field_id=answer.field_id, rating=answer.rating, text=answer.text, choice=answer.choice
    )
