"""Who has to fill in which form, and filling it in.

Forms reach people two ways:

- HR sends a published form by hand (`assign`).
- The daily job (`run_due`) sends the two onboarding forms on their own: the
  new-joiner form to each joiner N days after their start date, and the buddy
  form to that joiner's buddy M days after it. It looks at every active
  employee rather than hooking the three places an employee is created, so
  nobody is missed whichever way they arrived, and a buddy who arrives later
  from Circle still gets their form. One assignment per (trigger, joiner) ever:
  re-running is harmless and a rejoiner is never asked twice.

A joiner with no buddy when the buddy form is due never blocks anything: the
form waits as NEEDS_BUDDY, HR is told once, and it goes out as soon as a buddy
is on file.

The same job sends the three probation review forms (see `probation_forms`):
to the probationer, to their manager (or NEEDS_MANAGER, mirroring the buddy
path), and to the HR pool, where any HR or admin may claim it by submitting.
"Today" is the org's day (its timezone), never the server's.

Telling people (email + in-app) happens after the assignments are saved and
each send is caught on its own, so a mail failure can never undo an
assignment or, by rolling back, cause the same mail to go out twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.employee import Employee, Role
from app.models.form import (
    AssignmentStatus,
    Form,
    FormAssignment,
    FormFieldType,
    FormStatus,
    FormTrigger,
)
from app.models.notification import NotificationKind
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.form import FormAssignmentRepository, FormRepository, FormResponseRepository
from app.schemas.auth import CurrentUser
from app.schemas.form import (
    PROBATION_TRIGGERS,
    AssignmentCreate,
    AssignmentDetail,
    AssignmentRead,
    SubmissionWrite,
)
from app.services.email_service import EmailService
from app.services.form_answers import check_answers
from app.services.form_emails import (
    form_assigned_email,
    hr_pool_form_email,
    missing_buddy_email,
    missing_manager_email,
)
from app.services.form_scoring import field_weights, score_answers
from app.services.form_settlement import Settle, Situation, is_unsent, settle, usable_buddy
from app.services.form_views import can_manage_forms, to_answer, to_assignment, to_read, to_summary
from app.services.notification_service import NotificationService
from app.services.probation_forms import (
    ProbationFormLink,
    is_pool,
    is_probation_form,
    is_review_due,
    probation_values,
)

logger = get_logger("app.forms")

SYSTEM_ACTOR = "system:forms"
_DEFAULT_DUE = 7
_MAX_DUE_AHEAD = 365
_FORMS_PATH = "/dashboard/forms"


# --- pure rules (unit-tested directly) ------------------------------------ #
def opens_on(start: date, days: int | None) -> date:
    return start + timedelta(days=days or 0)


def is_due(employee: Employee, form: Form, today: date) -> bool:
    """Send this triggered form for this joiner today?

    Only people with a joining date on record (from Circle, or set by HR), who
    started on or after the form went live, once the set number of days has
    passed. Without a joining date there is no telling a new joiner from
    existing staff added to Avora later, so nobody is guessed at: HR can send
    the form by hand."""
    start = employee.hire_date
    if start is None or form.auto_assign_since is None or not employee.is_active:
        return False
    return start >= form.auto_assign_since and opens_on(start, form.trigger_days) <= today


@dataclass
class RunResult:
    created: int = 0
    activated: int = 0
    cancelled: int = 0


class FormAssignmentService:
    def __init__(
        self,
        forms: FormRepository,
        assignments: FormAssignmentRepository,
        responses: FormResponseRepository,
        employees: EmployeeRepository,
        notifications: NotificationService,
        email: EmailService,
        audit: AuditRepository,
        settings: Settings,
        probation: ProbationFormLink,
    ) -> None:
        self._forms = forms
        self._assignments = assignments
        self._responses = responses
        self._employees = employees
        self._notifications = notifications
        self._email = email
        self._audit = audit
        self._settings = settings
        self._probation = probation

    # --- the person filling it in ---------------------------------------- #
    async def mine(self, caller: CurrentUser) -> list[AssignmentRead]:
        """Their own forms, plus, for HR and admins, the unclaimed HR probation
        forms they could fill in."""
        today = await self._today()
        rows = list(await self._assignments.for_assignee(caller.employee_id, today=today))
        if can_manage_forms(caller):
            rows = list(await self._assignments.pool_for(caller.employee_id, today=today)) + rows
        return [to_assignment(r) for r in rows]

    async def detail(self, caller: CurrentUser, assignment_id: uuid.UUID) -> AssignmentDetail:
        row = await self._assignments.get_named(assignment_id)
        if row is None or not self._may_open(caller, row[0], await self._today()):
            raise NotFoundError()
        assignment = row[0]
        form = await self._forms.get(assignment.form_id)
        if form is None:  # pragma: no cover - cascades with the form
            raise NotFoundError()
        fields = await self._forms.fields_of(form.id)
        response = await self._responses.for_assignment(assignment.id)
        answers = (
            (await self._responses.answers_for([response.id])).get(response.id, [])
            if response
            else []
        )
        if assignment.assignee_id != caller.employee_id and not is_pool(assignment):
            # HR/admin reading someone else's answers is a sensitive read (5.7).
            await self._audit.append(
                actor=str(caller.employee_id),
                action="form.answers.view",
                target=f"assignment:{assignment.id}",
            )
        summary = to_summary(
            form, question_count=sum(f.type is not FormFieldType.SECTION for f in fields)
        )
        scored, total = score_answers(field_weights(fields), [to_answer(a) for a in answers])
        details = None
        if is_probation_form(assignment.trigger) and assignment.subject_id is not None:
            found = await self._probation.subject_details([assignment.subject_id])
            details = found.get(assignment.subject_id)
        return AssignmentDetail(
            **to_assignment(row).model_dump(),
            form=to_read(form, fields, summary),
            answers=scored,
            weighted_total=total if response else None,
            subject_details=details,
        )

    async def submit(
        self, caller: CurrentUser, assignment_id: uuid.UUID, payload: SubmissionWrite
    ) -> AssignmentDetail:
        # Locked: a double-click submits once, the second gets a 409. Two people
        # in HR submitting the shared HR form at once queue here too: the
        # first claims it, the second finds it submitted.
        assignment = await self._assignments.get_for_update(assignment_id)
        # Only the assignee submits; HR can read it but never answer for
        # someone. The one exception is the shared HR probation form.
        if assignment is None or not (
            assignment.assignee_id == caller.employee_id or _may_claim(caller, assignment)
        ):
            raise NotFoundError()
        if not self._may_open(caller, assignment, await self._today()):
            raise NotFoundError()
        if assignment.status is not AssignmentStatus.PENDING:
            raise ConflictError("This form has already been submitted.")
        rows = check_answers(await self._forms.fields_of(assignment.form_id), payload.answers)
        now = datetime.now(UTC)
        assignment.assignee_id = caller.employee_id  # claims the HR pool form
        await self._responses.add(
            assignment=assignment, respondent_id=caller.employee_id, submitted_at=now, answers=rows
        )
        assignment.status = AssignmentStatus.SUBMITTED
        assignment.submitted_at = now
        await self._assignments.flush()
        await self._probation.on_submitted(assignment)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="form.submit",
            target=f"assignment:{assignment.id}",
        )
        return await self.detail(caller, assignment.id)

    # --- HR --------------------------------------------------------------- #
    async def assign(
        self, caller: CurrentUser, form_id: uuid.UUID, payload: AssignmentCreate
    ) -> list[AssignmentRead]:
        form = await self._forms.get(form_id)
        if form is None or not can_manage_forms(caller):
            raise NotFoundError()
        if form.status is not FormStatus.PUBLISHED:
            raise ConflictError("Publish the form before sending it.")
        people = await self._employees.get_many(payload.assignee_ids)
        if len(people) != len(set(payload.assignee_ids)) or not all(
            p.is_active for p in people.values()
        ):
            raise ValidationError("Every recipient must be a current employee.")
        if payload.subject_id is not None:
            about = await self._employees.get(payload.subject_id)
            if about is None or not about.is_active:
                raise ValidationError("The person this form is about must be a current employee.")
        today = await self._today()
        if payload.due_on is not None and payload.due_on < today:
            raise ValidationError("The due date can't be in the past.")
        if payload.due_on is not None and payload.due_on > today + timedelta(days=_MAX_DUE_AHEAD):
            raise ValidationError("Pick a due date within the next year.")
        due_on = payload.due_on or today + timedelta(days=form.due_days)
        created: list[FormAssignment] = []
        for person_id in dict.fromkeys(payload.assignee_ids):
            created.append(
                await self._assignments.add(
                    form_id=form.id,
                    assignee_id=person_id,
                    subject_id=payload.subject_id,
                    trigger=FormTrigger.MANUAL,
                    status=AssignmentStatus.PENDING,
                    available_on=today,
                    due_on=due_on,
                    assigned_by=caller.employee_id,
                )
            )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="form.assign",
            target=f"form:{form.id}:{len(created)}",
        )
        # Nobody is mailed from inside this request: if it rolled back after
        # sending, people would get links to forms that don't exist, and a
        # retry would mail them twice. The forms scheduler tells them on its
        # next run, after this has been committed. The form already shows in
        # their list.
        return [await self._read(a.id) for a in created]

    async def for_form(self, caller: CurrentUser, form_id: uuid.UUID) -> list[AssignmentRead]:
        form = await self._forms.get(form_id)
        if form is None or not can_manage_forms(caller):
            raise NotFoundError()
        return [to_assignment(r) for r in await self._assignments.for_form(form.id)]

    async def needs_buddy(self, caller: CurrentUser) -> list[AssignmentRead]:
        if not can_manage_forms(caller):
            raise NotFoundError()
        rows = await self._assignments.with_status(AssignmentStatus.NEEDS_BUDDY)
        return [to_assignment(r) for r in rows]

    async def needs_manager(self, caller: CurrentUser) -> list[AssignmentRead]:
        if not can_manage_forms(caller):
            raise NotFoundError()
        rows = await self._assignments.with_status(AssignmentStatus.NEEDS_MANAGER)
        return [to_assignment(r) for r in rows]

    # --- the daily job ---------------------------------------------------- #
    async def run_due(self, today: date) -> RunResult:
        """Create today's automatic assignments. The caller commits, then calls
        `notify_due` and `alert_missing_buddies`."""
        result = RunResult()
        for trigger in (FormTrigger.NEW_JOINER, FormTrigger.BUDDY):
            live = await self._forms.published_for_trigger(trigger)
            if live:
                result.created += await self._create_due(live[0], today)
        for trigger in sorted(PROBATION_TRIGGERS):
            live = await self._forms.published_for_trigger(trigger)
            if live:
                result.created += await self._create_probation_due(live[0], today)
        await self._settle_unsent(today, result)
        return result

    async def org_today(self) -> date:
        return await self._probation.org_today()

    async def unnotified_ids(self, today: date) -> list[uuid.UUID]:
        return [a.id for a in await self._assignments.unnotified(today=today)]

    async def notify_one(self, assignment_id: uuid.UUID) -> bool:
        """Tell one assignee. The scheduler commits after EACH call, so a crash
        part-way through never forgets who was already mailed (and mails them
        again). Never raises for a mail failure; `notified_at` marks success."""
        # Skip-locked: if another scheduler is mailing this person right now,
        # leave it to them rather than mailing twice.
        assignment = await self._assignments.get_for_update(assignment_id, skip_locked=True)
        if assignment is None or not is_unsent(assignment):
            return False
        result = RunResult()
        # Re-checked here: things change between the sweep and the email.
        await self._apply_settlements([assignment], await self._today(), result)
        form = await self._forms.get(assignment.form_id)
        if form is None or assignment.status is not AssignmentStatus.PENDING:
            return False
        if is_pool(assignment):
            return await self._tell_pool(assignment, form)
        return await self._tell(assignment, form)

    async def notify_due(self, today: date) -> int:
        """Tell every untold assignee, in one transaction. For tests and
        one-off use; the scheduler uses `notify_one` per assignment."""
        told = 0
        for assignment_id in await self.unnotified_ids(today):
            told += int(await self.notify_one(assignment_id))
        return told

    async def alert_missing_managers(self) -> int:
        """Tell HR, once, about probationers whose manager form can't go out."""
        waiting = list(await self._assignments.claim_unalerted(AssignmentStatus.NEEDS_MANAGER))
        if not waiting:
            return 0
        people = await self._employees.get_many([a.subject_id for a in waiting if a.subject_id])
        names = sorted(people[a.subject_id].full_name for a in waiting if a.subject_id in people)
        subject, html = missing_manager_email(names=names, forms_url=self._url(_FORMS_PATH))
        if not await self._mail_hr(subject, html):
            return 0
        now = datetime.now(UTC)
        for assignment in waiting:
            assignment.hr_alerted_at = now
        await self._assignments.flush()
        return len(waiting)

    async def alert_missing_buddies(self) -> int:
        waiting = list(await self._assignments.claim_unalerted(AssignmentStatus.NEEDS_BUDDY))
        if not waiting:
            return 0
        joiners = await self._employees.get_many([a.subject_id for a in waiting if a.subject_id])
        names = sorted(joiners[a.subject_id].full_name for a in waiting if a.subject_id in joiners)
        subject, html = missing_buddy_email(joiner_names=names, forms_url=self._url(_FORMS_PATH))
        if not await self._mail_hr(subject, html):
            return 0
        now = datetime.now(UTC)
        for assignment in waiting:
            assignment.hr_alerted_at = now
        await self._assignments.flush()
        return len(waiting)

    async def _create_due(self, form: Form, today: date) -> int:
        done = await self._assignments.subjects_with(form.trigger)
        due = [
            joiner
            for joiner in await self._employees.list_all_active()
            if joiner.id not in done and is_due(joiner, form, today)
        ]
        buddies = await self._employees.get_many(
            [j.buddy_id for j in due if j.buddy_id is not None]
        )
        await self._assignments.add_many(
            [
                self._auto_values(
                    form, joiner, buddies.get(joiner.buddy_id) if joiner.buddy_id else None, today
                )
                for joiner in due
            ]
        )
        created = len(due)
        if created:
            await self._audit.append(
                actor=SYSTEM_ACTOR, action="form.auto_assign", target=f"form:{form.id}:{created}"
            )
        return created

    async def _create_probation_due(self, form: Form, today: date) -> int:
        done = await self._assignments.subjects_with(form.trigger)
        candidates = [e for e in await self._employees.list_all_active() if e.id not in done]
        ends = await self._probation.end_dates(candidates)
        due = [e for e in candidates if is_review_due(e, ends.get(e.id), form, today)]
        managers = await self._employees.get_many(
            [e.manager_id for e in due if e.manager_id is not None]
        )
        created: list[FormAssignment] = []
        for person in due:
            manager = managers.get(person.manager_id) if person.manager_id else None
            values = probation_values(form, person, manager, ends[person.id], today)
            created.append(await self._assignments.add(**values))
        for assignment in created:
            await self._probation.on_opened(assignment)
        if created:
            await self._audit.append(
                actor=SYSTEM_ACTOR,
                action="form.auto_assign",
                target=f"form:{form.id}:{len(created)}",
            )
        return len(created)

    def _auto_values(
        self, form: Form, joiner: Employee, buddy: Employee | None, today: date
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "form_id": form.id,
            "subject_id": joiner.id,
            "trigger": form.trigger,
            "available_on": opens_on(joiner.hire_date or today, form.trigger_days),
            "due_on": today + timedelta(days=form.due_days),
            "status": AssignmentStatus.PENDING,
        }
        if form.trigger is FormTrigger.NEW_JOINER:
            values["assignee_id"] = joiner.id
        elif usable_buddy(joiner, buddy):
            values["assignee_id"] = joiner.buddy_id
        else:
            # No buddy, or one who has since left: wait for a real one.
            values["status"] = AssignmentStatus.NEEDS_BUDDY
        return values

    async def _settle_unsent(self, today: date, result: RunResult) -> None:
        """Re-check everything not yet sent (see `form_settlement`)."""
        await self._apply_settlements(list(await self._assignments.unsent()), today, result)

    async def _apply_settlements(
        self, assignments: list[FormAssignment], today: date, result: RunResult
    ) -> None:
        if not assignments:
            return
        forms = await self._forms.get_many([a.form_id for a in assignments])
        live = await self._forms.live_triggers([f.family_id for f in forms.values()])
        people = await self._employees.get_many(
            [i for a in assignments for i in (a.subject_id, a.assignee_id) if i is not None]
        )
        related = await self._employees.get_many(
            [i for p in people.values() for i in (p.buddy_id, p.manager_id) if i is not None]
        )
        activated: list[FormAssignment] = []
        for assignment in assignments:
            form = forms.get(assignment.form_id)
            joiner = people.get(assignment.subject_id) if assignment.subject_id else None
            now = Situation(
                live_trigger=live.get(form.family_id) if form else None,
                joiner=joiner,
                assignee=people.get(assignment.assignee_id) if assignment.assignee_id else None,
                buddy=related.get(joiner.buddy_id) if joiner and joiner.buddy_id else None,
                manager=related.get(joiner.manager_id) if joiner and joiner.manager_id else None,
            )
            verdict = settle(assignment, now)
            self._apply(assignment, verdict, now, form, today, result)
            if verdict is Settle.TO_MANAGER:
                activated.append(assignment)
        await self._assignments.flush()
        for assignment in activated:
            await self._probation.on_opened(assignment)

    @staticmethod
    def _apply(
        assignment: FormAssignment,
        verdict: Settle,
        now: Situation,
        form: Form | None,
        today: date,
        result: RunResult,
    ) -> None:
        if verdict is Settle.CANCEL:
            assignment.status = AssignmentStatus.CANCELLED
            result.cancelled += 1
        elif verdict is Settle.TO_BUDDY and now.buddy is not None:
            assignment.assignee_id = now.buddy.id
            assignment.status = AssignmentStatus.PENDING
            assignment.due_on = today + timedelta(days=form.due_days if form else _DEFAULT_DUE)
            result.activated += 1
        elif verdict is Settle.WAIT_FOR_BUDDY:
            assignment.assignee_id = None
            assignment.status = AssignmentStatus.NEEDS_BUDDY
            assignment.hr_alerted_at = None  # HR needs to hear about this one
        elif verdict is Settle.TO_MANAGER and now.manager is not None:
            assignment.assignee_id = now.manager.id
            assignment.status = AssignmentStatus.PENDING
            assignment.due_on = today + timedelta(days=form.due_days if form else _DEFAULT_DUE)
            result.activated += 1
        elif verdict is Settle.WAIT_FOR_MANAGER:
            assignment.assignee_id = None
            assignment.status = AssignmentStatus.NEEDS_MANAGER
            assignment.hr_alerted_at = None

    # --- helpers ---------------------------------------------------------- #
    @staticmethod
    def _may_open(caller: CurrentUser, assignment: FormAssignment, today: date) -> bool:
        """The assignee, once it has opened (HR and admins too, for their own
        forms: an HR joiner waits for day 15 like anyone). HR/admin may read
        anyone else's, except the probation reviews about themselves; the
        unclaimed HR pool form opens for them like their own."""
        if assignment.assignee_id == caller.employee_id:
            return assignment.available_on <= today and assignment.status in (
                AssignmentStatus.PENDING,
                AssignmentStatus.SUBMITTED,
            )
        if not can_manage_forms(caller):
            return False
        if is_probation_form(assignment.trigger) and assignment.subject_id == caller.employee_id:
            return False  # nobody reads their own probation evaluations
        if is_pool(assignment):
            return assignment.available_on <= today
        return True

    async def _read(self, assignment_id: uuid.UUID) -> AssignmentRead:
        row = await self._assignments.get_named(assignment_id)
        if row is None:  # pragma: no cover - just created
            raise NotFoundError()
        return to_assignment(row)

    async def _tell(self, assignment: FormAssignment, form: Form) -> bool:
        """Email + in-app note to the assignee. Never raises."""
        if assignment.assignee_id is None:
            return False
        people = await self._employees.get_many(
            [i for i in (assignment.assignee_id, assignment.subject_id) if i is not None]
        )
        person = people.get(assignment.assignee_id)
        if person is None or not person.is_active:
            return False
        about = people.get(assignment.subject_id) if assignment.subject_id else None
        subject_name = about.full_name if about and about.id != person.id else None
        link = f"{_FORMS_PATH}/fill/{assignment.id}"
        subject, html = form_assigned_email(
            recipient_name=person.full_name.split(" ")[0] or person.full_name,
            form_title=form.title,
            subject_name=subject_name,
            due_label=assignment.due_on.strftime("%d %b %Y") if assignment.due_on else None,
            form_url=self._url(link),
        )
        try:
            await self._email.send(to=person.work_email, subject=subject, html=html)
        except Exception:
            logger.warning("form_email_failed", extra={"assignment": str(assignment.id)})
            return False
        await self._notifications.notify(
            recipient_id=person.id,
            kind=NotificationKind.SYSTEM,
            title=f"Your feedback: {form.title}",
            link=link,
            entity_type="form_assignment",
            entity_id=assignment.id,
        )
        assignment.notified_at = datetime.now(UTC)
        await self._assignments.flush()
        return True

    async def _tell_pool(self, assignment: FormAssignment, form: Form) -> bool:
        """Email + in-app note to every HR and admin, once, about a shared HR
        probation form. Never the person it is about. Never raises."""
        about = await self._employees.get(assignment.subject_id) if assignment.subject_id else None
        link = f"{_FORMS_PATH}/fill/{assignment.id}"
        subject, html = hr_pool_form_email(
            form_title=form.title,
            subject_name=about.full_name if about else None,
            due_label=assignment.due_on.strftime("%d %b %Y") if assignment.due_on else None,
            form_url=self._url(link),
        )
        told = False
        for person in await self._hr_people(exclude=assignment.subject_id):
            try:
                await self._email.send(to=person.work_email, subject=subject, html=html)
            except Exception:
                logger.warning("form_pool_email_failed", extra={"recipient": str(person.id)})
                continue
            told = True
            await self._notifications.notify(
                recipient_id=person.id,
                kind=NotificationKind.SYSTEM,
                title=f"HR review: {form.title}",
                link=link,
                entity_type="form_assignment",
                entity_id=assignment.id,
            )
        if told:
            assignment.notified_at = datetime.now(UTC)
            await self._assignments.flush()
        return told

    async def _hr_people(self, exclude: uuid.UUID | None = None) -> list[Employee]:
        people = {e.id: e for e in await self._employees.list_admins()}
        people.update({e.id: e for e in await self._employees.list_by_role(Role.HR)})
        return [p for p in people.values() if p.id != exclude]

    async def _mail_hr(self, subject: str, html: str) -> bool:
        sent = False
        for person in await self._hr_people():
            try:
                await self._email.send(to=person.work_email, subject=subject, html=html)
                sent = True
            except Exception:
                logger.warning("form_hr_email_failed", extra={"recipient": str(person.id)})
        return sent

    def _url(self, path: str) -> str:
        return f"{self._settings.app_base_url.rstrip('/')}{path}"

    async def _today(self) -> date:
        return await self._probation.org_today()


def _may_claim(caller: CurrentUser, assignment: FormAssignment) -> bool:
    """Any HR or admin may fill in the shared HR probation form, except the
    one about themselves."""
    return (
        is_pool(assignment)
        and can_manage_forms(caller)
        and assignment.subject_id != caller.employee_id
    )
