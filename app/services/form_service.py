"""Form builder and library.

HR and admins build forms; a draft is edited freely, a published form never
changes (its answers would stop meaning anything), so changing one means
starting a new draft version. Publishing that version retires the old one.

The library is the published forms a caller may see, using the workspace-file
visibility model (everyone, or listed departments and people). Answers are a
different matter: only HR and admins read them, and every read is audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from statistics import fmean

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.form import (
    AssignmentStatus,
    Form,
    FormField,
    FormFieldType,
    FormStatus,
    FormTrigger,
)
from app.models.workspace_file import WorkspaceVisibility
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.form import FormRepository, FormResponseRepository
from app.schemas.auth import CurrentUser
from app.schemas.form import (
    PROBATION_TRIGGERS,
    FieldResult,
    FormRead,
    FormResults,
    FormSummary,
    FormWrite,
    ResponseRead,
)
from app.services.form_scoring import field_weights, score_answers, weights_problem
from app.services.form_views import can_manage_forms, to_answer, to_read, to_summary
from app.services.probation_forms import ProbationFormLink
from app.services.workspace_file_service import WorkspaceFileService

_TRIGGERED = frozenset({FormTrigger.NEW_JOINER, FormTrigger.BUDDY}) | PROBATION_TRIGGERS


class FormService:
    def __init__(
        self,
        forms: FormRepository,
        responses: FormResponseRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        probation: ProbationFormLink,
    ) -> None:
        self._forms = forms
        self._responses = responses
        self._employees = employees
        self._audit = audit
        self._probation = probation

    # --- reading ---------------------------------------------------------- #
    async def library(self, caller: CurrentUser) -> list[FormSummary]:
        manage = can_manage_forms(caller)
        forms = await self._forms.list(include_drafts=manage, include_archived=manage)
        if not manage:
            department = await self._department(caller)
            forms = [f for f in forms if self._visible(caller, f, department)]
        return await self._summaries(forms, with_counts=manage)

    async def get(self, caller: CurrentUser, form_id: uuid.UUID) -> FormRead:
        form = await self._readable(caller, form_id)
        summary = (await self._summaries([form], with_counts=can_manage_forms(caller)))[0]
        return to_read(form, await self._forms.fields_of(form.id), summary)

    async def results(self, caller: CurrentUser, form_id: uuid.UUID) -> FormResults:
        form = await self._managed(caller, form_id)
        fields = await self._forms.fields_of(form.id)
        rows = await self._responses.for_form(form.id)
        answers = await self._responses.answers_for([r[0].id for r in rows])
        weights = field_weights(fields)
        details = (
            await self._probation.subject_details([r[2] for r in rows if r[2] is not None])
            if form.trigger in PROBATION_TRIGGERS
            else {}
        )
        responses: list[ResponseRead] = []
        for response, respondent_name, subject_id, subject_name in rows:
            scored, total = score_answers(
                weights, [to_answer(a) for a in answers.get(response.id, [])]
            )
            responses.append(
                ResponseRead(
                    assignment_id=response.assignment_id,
                    respondent_id=response.respondent_id,
                    respondent_name=respondent_name,
                    subject_id=subject_id,
                    subject_name=subject_name,
                    submitted_at=response.submitted_at,
                    answers=scored,
                    weighted_total=total,
                    subject_details=details.get(subject_id) if subject_id else None,
                )
            )
        await self._audit.append(
            actor=str(caller.employee_id), action="form.results.view", target=f"form:{form.id}"
        )
        summary = (await self._summaries([form], with_counts=True))[0]
        return FormResults(
            form=to_read(form, fields, summary),
            fields=_field_results(fields, responses),
            responses=responses,
        )

    # --- building --------------------------------------------------------- #
    async def create(self, caller: CurrentUser, payload: FormWrite) -> FormRead:
        self._require_manage(caller)
        family_id = uuid.uuid4()
        form = await self._forms.add(
            family_id=family_id, version=1, created_by=caller.employee_id, **_values(payload)
        )
        await self._forms.replace_fields(form, _field_values(payload))
        await self._log(caller, "form.create", form)
        return await self.get(caller, form.id)

    async def update(self, caller: CurrentUser, form_id: uuid.UUID, payload: FormWrite) -> FormRead:
        form = await self._managed(caller, form_id)
        _require_draft(form)
        for key, value in _values(payload).items():
            setattr(form, key, value)
        await self._forms.replace_fields(form, _field_values(payload))
        await self._log(caller, "form.update", form)
        return await self.get(caller, form.id)

    async def delete_draft(self, caller: CurrentUser, form_id: uuid.UUID) -> None:
        form = await self._managed(caller, form_id)
        _require_draft(form)
        await self._log(caller, "form.delete", form)
        await self._forms.delete(form)

    async def new_version(self, caller: CurrentUser, form_id: uuid.UUID) -> FormRead:
        """A new draft copied from this version, so a published form can change."""
        source = await self._managed(caller, form_id, lock=True)
        if await self._forms.in_family(source.family_id, FormStatus.DRAFT):
            raise ConflictError(_DRAFT_EXISTS)
        copy = await self._forms.add_unique(
            _DRAFT_EXISTS,
            family_id=source.family_id,
            version=await self._forms.next_version(source.family_id),
            created_by=caller.employee_id,
            **{key: getattr(source, key) for key in _COPIED},
        )
        await self._forms.replace_fields(
            copy,
            [
                {key: getattr(field, key) for key in _FIELD_KEYS}
                for field in await self._forms.fields_of(source.id)
            ],
        )
        await self._log(caller, "form.version", copy)
        return await self.get(caller, copy.id)

    async def publish(self, caller: CurrentUser, form_id: uuid.UUID) -> FormRead:
        # Locked: two clicks on Publish must not both go through.
        form = await self._managed(caller, form_id, lock=True)
        _require_draft(form)
        problem = weights_problem(await self._forms.fields_of(form.id))
        if problem is not None:
            raise ValidationError(problem)
        await self._guard_one_per_trigger(form)
        # A new version replacing a live one with the SAME trigger keeps its
        # go-live date, so joiners already waiting still get the form. Anything
        # else (a first publish, a republish after the form was withdrawn, or a
        # version that changes who it goes to) starts from today: publishing
        # must never mail people who joined before, or during a gap.
        form.auto_assign_since = None
        for previous in await self._forms.in_family(form.family_id, FormStatus.PUBLISHED):
            previous.status = FormStatus.ARCHIVED
            if previous.trigger is form.trigger:
                form.auto_assign_since = previous.auto_assign_since
            else:
                # It no longer goes out that way: drop what has not gone out.
                await self._forms.cancel_unsent(form.family_id, previous.trigger)
        await self._forms.flush()  # retire the old version before the new goes live
        if form.trigger in _TRIGGERED and form.auto_assign_since is None:
            form.auto_assign_since = datetime.now(UTC).date()
        form.status = FormStatus.PUBLISHED
        form.published_at = datetime.now(UTC)
        # The database allows one live form per automatic trigger; a publish
        # racing another one ends here as a 409, never as two live forms.
        await self._forms.flush_unique(_TRIGGER_TAKEN)
        await self._log(caller, "form.publish", form)
        return await self.get(caller, form.id)

    async def archive(self, caller: CurrentUser, form_id: uuid.UUID) -> FormRead:
        form = await self._managed(caller, form_id, lock=True)
        if form.status is not FormStatus.PUBLISHED:
            raise ConflictError("Only a published form can be archived.")
        form.status = FormStatus.ARCHIVED
        # Withdrawn: whatever has not reached anyone yet does not go out,
        # including what older versions of this form were still holding.
        await self._forms.cancel_unsent(form.family_id)
        await self._log(caller, "form.archive", form)
        return await self.get(caller, form.id)

    # --- helpers ---------------------------------------------------------- #
    async def _guard_one_per_trigger(self, form: Form) -> None:
        """Two live joiner forms would send every joiner two forms."""
        if form.trigger not in _TRIGGERED:
            return
        live = await self._forms.published_for_trigger(form.trigger)
        if any(other.family_id != form.family_id for other in live):
            raise ConflictError(_TRIGGER_TAKEN)

    async def _readable(self, caller: CurrentUser, form_id: uuid.UUID) -> Form:
        form = await self._forms.get(form_id)
        if form is None:
            raise NotFoundError()
        if can_manage_forms(caller):
            return form
        published = form.status is FormStatus.PUBLISHED
        if not published or not self._visible(caller, form, await self._department(caller)):
            raise NotFoundError()  # never reveal a draft or a form outside your access
        return form

    async def _managed(
        self, caller: CurrentUser, form_id: uuid.UUID, *, lock: bool = False
    ) -> Form:
        if not can_manage_forms(caller):
            raise NotFoundError()
        form = await (self._forms.get_for_update(form_id) if lock else self._forms.get(form_id))
        if form is None:
            raise NotFoundError()
        return form

    @staticmethod
    def _require_manage(caller: CurrentUser) -> None:
        if not can_manage_forms(caller):
            raise NotFoundError()

    @staticmethod
    def _visible(caller: CurrentUser, form: Form, department: str | None) -> bool:
        if form.visibility is WorkspaceVisibility.EVERYONE:
            return True
        if department and department in (form.visible_departments or []):
            return True
        return str(caller.employee_id) in (form.visible_employee_ids or [])

    async def _department(self, caller: CurrentUser) -> str | None:
        employee = await self._employees.get(caller.employee_id)
        return employee.department if employee is not None else None

    async def _summaries(self, forms: Sequence[Form], *, with_counts: bool) -> list[FormSummary]:
        """`with_counts` is the HR/admin view: send counts and who a form is
        shared with. Everyone else sees neither."""
        ids = [f.id for f in forms]
        questions = await self._forms.question_counts(ids)
        counts = await self._forms.assignment_counts(ids) if with_counts else {}
        summaries = [
            to_summary(
                f,
                question_count=questions.get(f.id, 0),
                response_count=counts.get((f.id, AssignmentStatus.SUBMITTED), 0),
                pending_count=counts.get((f.id, AssignmentStatus.PENDING), 0),
            )
            for f in forms
        ]
        if with_counts:
            return summaries
        return [
            s.model_copy(update={"visible_departments": [], "visible_employee_ids": []})
            for s in summaries
        ]

    async def _log(self, caller: CurrentUser, action: str, form: Form) -> None:
        await self._audit.append(
            actor=str(caller.employee_id), action=action, target=f"form:{form.id}"
        )


_DRAFT_EXISTS = "This form already has a draft. Edit that one instead."
_TRIGGER_TAKEN = "Another published form is already sent automatically for this. Archive it first."

# Columns a new version copies from the one it starts from.
_COPIED = (
    "title",
    "intro",
    "rating_labels",
    "trigger",
    "trigger_days",
    "due_days",
    "visibility",
    "visible_departments",
    "visible_employee_ids",
)
_FIELD_KEYS = ("type", "label", "help_text", "required", "options", "with_comment", "weight")


def _require_draft(form: Form) -> None:
    if form.status is not FormStatus.DRAFT:
        raise ConflictError(
            "A published form can't be edited, so its answers keep their meaning. "
            "Start a new version instead."
        )


def _values(payload: FormWrite) -> dict[str, object]:
    visibility, departments, employee_ids = WorkspaceFileService._normalize_access(payload)
    # The library ACL only decides who can browse a form. A triggered form still
    # reaches every joiner (and buddy) whether or not they are listed.
    return {
        "title": payload.title.strip(),
        "intro": (payload.intro or "").strip() or None,
        "rating_labels": payload.rating_labels,
        "trigger": payload.trigger,
        "trigger_days": payload.trigger_days,
        "due_days": payload.due_days,
        "visibility": visibility,
        "visible_departments": departments,
        "visible_employee_ids": employee_ids,
    }


def _field_values(payload: FormWrite) -> list[dict[str, object]]:
    return [
        {
            "type": f.type,
            "label": f.label.strip(),
            "help_text": (f.help_text or "").strip() or None,
            "required": f.required,
            "options": f.options,
            "with_comment": f.with_comment,
            "weight": f.weight,
        }
        for f in payload.fields
    ]


def _field_results(fields: Sequence[FormField], responses: list[ResponseRead]) -> list[FieldResult]:
    by_field: dict[uuid.UUID, list[tuple[int | None, str | None]]] = {}
    for response in responses:
        for answer in response.answers:
            by_field.setdefault(answer.field_id, []).append((answer.rating, answer.choice))
    results: list[FieldResult] = []
    for field in fields:
        if field.type is FormFieldType.SECTION:
            continue
        values = by_field.get(field.id, [])
        ratings = [r for r, _ in values if r is not None]
        counts: dict[str, int] = {}
        for rating, choice in values:
            key = str(rating) if rating is not None else choice
            if key:
                counts[key] = counts.get(key, 0) + 1
        results.append(
            FieldResult(
                field_id=field.id,
                label=field.label,
                type=field.type,
                answered=len(values),
                average=round(fmean(ratings), 2) if ratings else None,
                weight=field.weight,
                counts=counts,
            )
        )
    return results
