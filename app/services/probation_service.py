"""Probation review checklist — who is on probation, and how far their review got.

The step LIST is policy and lives in `app.core.probation`. This service owns two
things the template can't know: whose checklist a caller may see, and which steps
that caller may move.

Ticking is bounded by the step's own `owner` column, so the checklist means what
it says. HR signing off "Employee completes self-assessment" on the employee's
behalf would turn an accountability record into a formality, so the API refuses
it rather than trusting the UI to hide the control.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.probation import (
    ACKNOWLEDGE_STEP,
    RECENT_CONFIRMATION_DAYS,
    REVIEW_LEAD_DAYS,
    STEPS,
    StepOwner,
    step_or_none,
)
from app.core.probation_letter_pdf import (
    ConfirmationLetter,
    ExtensionLetter,
    render_confirmation_letter,
    render_extension_letter,
)
from app.core.tenure import (
    TenureStatus,
    anniversaries_reached,
    months_to_reach,
    probation_end,
    tenure_status,
)
from app.models.employee import Employee, Role
from app.models.probation_checklist import ProbationChecklistItem, ProbationStepStatus
from app.models.probation_decision import ProbationDecision, ProbationOutcome
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.probation_checklist import ProbationChecklistRepository
from app.repositories.probation_decision import ProbationDecisionRepository
from app.schemas.auth import CurrentUser
from app.schemas.probation import (
    MyProbationRead,
    ProbationChecklistRead,
    ProbationDecisionCreate,
    ProbationDecisionRead,
    ProbationRosterEntry,
    ProbationStepRead,
    ProbationStepUpdate,
)
from app.services.attendance_policy_service import AttendancePolicyService
from app.services.email_service import EmailAttachment, EmailError, EmailService
from app.services.leave_policy_service import LeavePolicyService

logger = get_logger("app.probation")

# The longest probation an extension may run to, in months from the joining
# date. Matches the per-person override's own limit on the employee schema.
MAX_PROBATION_MONTHS = 24


class ProbationService:
    def __init__(
        self,
        items: ProbationChecklistRepository,
        decisions: ProbationDecisionRepository,
        employees: EmployeeRepository,
        policies: tuple[LeavePolicyService, AttendancePolicyService],
        email: EmailService,
        audit: AuditRepository,
    ) -> None:
        self._items = items
        self._decisions = decisions
        self._employees = employees
        self._leave_policy, self._attendance_policy = policies
        self._email = email
        self._audit = audit

    # ---------------------------------------------------------------- helpers

    async def _org_today(self) -> date:
        """Today in the org's timezone, never the server's clock."""
        return await self._org_date(datetime.now(UTC))

    async def _org_date(self, moment: datetime) -> date:
        """The calendar date `moment` falls on in the org's timezone."""
        spec = await self._attendance_policy.spec()
        return moment.astimezone(ZoneInfo(spec.timezone)).date()

    async def _default_probation_months(self) -> int:
        policy = await self._leave_policy.get_or_create()
        return int(policy.probation_months)

    async def _probation_months(self, employee: Employee) -> int:
        """Their negotiated probation length, else the org default."""
        if employee.probation_months:
            return employee.probation_months
        return await self._default_probation_months()

    @staticmethod
    def _may_manage(caller: CurrentUser) -> bool:
        """HR and Admin run probation reviews. It is a people-ops process."""
        return caller.role in (Role.ADMIN, Role.HR)

    def _may_update_step(self, caller: CurrentUser, employee: Employee, owner: StepOwner) -> bool:
        """Whether `caller` may move a step this employee owns via `owner`."""
        is_self = caller.employee_id == employee.id
        is_their_manager = employee.manager_id == caller.employee_id
        if owner is StepOwner.EMPLOYEE:
            # Only the employee signs their own self-assessment and acknowledgement.
            return is_self
        if owner is StepOwner.HR:
            return self._may_manage(caller)
        if owner is StepOwner.MANAGER:
            return is_their_manager or self._may_manage(caller)
        return is_their_manager or self._may_manage(caller)  # HR_AND_MANAGER

    async def _visible_employee(self, caller: CurrentUser, employee_id: uuid.UUID) -> Employee:
        """The employee, if this caller is allowed to see their review at all.

        Scope comes from the shared repository clause (Security rule 5.3) rather
        than a check written here. A local "are you their manager?" test silently
        excluded senior managers, who can read their whole department everywhere
        else in the app.
        """
        if employee_id == caller.employee_id:
            # Nobody reads their own probation review - not even HR, who would
            # otherwise be reading the assessment of themselves. The checklist
            # is HR's working document: who ticked what, their private notes,
            # the manager's progress. What the probationer gets is `mine()`:
            # where they stand, and the one step that is theirs to take.
            # The same rule the probation FORMS use (`_may_open`).
            raise NotFoundError()
        if not await self._employees.can_read(caller, employee_id):
            # 404 over 403: revealing that a given person is under review is
            # itself scope leakage (API conventions §7).
            raise NotFoundError()
        employee = await self._employees.get(employee_id)
        if employee is None:
            raise NotFoundError()
        return employee

    # ------------------------------------------------------------------ reads

    async def roster(self, caller: CurrentUser) -> list[ProbationRosterEntry]:
        """Everyone currently on probation, within the caller's scope."""
        # One scoped query covers every role: HR/Admin get the org, a senior
        # manager their department, a manager their reports, anyone else just themselves.
        candidates = [
            e for e in await self._employees.all_in_scope(caller) if e.id != caller.employee_id
        ]
        today = await self._org_today()
        default_months = await self._default_probation_months()

        listed: list[tuple[Employee, date, bool]] = []
        for employee in candidates:
            if employee.hire_date is None:
                continue  # no hire date, no probation window to compute
            months = employee.probation_months or default_months
            ends_on = probation_end(employee.hire_date, months)
            band = tenure_status(employee.hire_date, today, probation_months=months)
            still_on = band is TenureStatus.PROBATION
            # Keep a just-confirmed person listed so their finished review can
            # still be opened; drop them once it is properly historic.
            recently_confirmed = not still_on and (today - ends_on).days <= RECENT_CONFIRMATION_DAYS
            if still_on or recently_confirmed:
                listed.append((employee, ends_on, still_on))

        settled = await self._items.counts_for(
            [e.id for e, _, _ in listed], [step.no for step in STEPS]
        )
        total = len(STEPS)
        entries = [
            ProbationRosterEntry(
                employee_id=employee.id,
                employee_name=employee.full_name,
                department=employee.department,
                hire_date=employee.hire_date,
                probation_end_date=ends_on,
                days_to_probation_end=(ends_on - today).days,
                review_due=still_on and (ends_on - today).days <= REVIEW_LEAD_DAYS,
                on_probation=still_on,
                settled_steps=settled.get(employee.id, 0),
                total_steps=total,
            )
            for employee, ends_on, still_on in listed
        ]
        # People still under review first, soonest confirmation at the top; the
        # recently-confirmed tail sorts most-recent-first behind them.
        entries.sort(
            key=lambda e: (not e.on_probation, e.days_to_probation_end or 0, e.employee_name)
        )
        await self._audit.append(
            actor=str(caller.employee_id), action="probation.roster.read", target="probation"
        )
        return entries

    async def checklist(
        self, caller: CurrentUser, employee_id: uuid.UUID
    ) -> ProbationChecklistRead:
        employee = await self._visible_employee(caller, employee_id)
        result = await self._build(caller, employee)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="probation.checklist.read",
            target=f"employee:{employee.id}",
        )
        return result

    async def mine(self, caller: CurrentUser) -> MyProbationRead | None:
        """Where the caller stands in their OWN probation, and nothing else.

        The counterpart to `checklist` refusing a self-read. They get the
        dates, the outcome once HR has recorded one, and whether they still
        owe the acknowledgement - never the 16 steps, the notes, or who has
        done what. None when they have no probation to report, which is most
        people most of the time.
        """
        employee = await self._employees.get(caller.employee_id)
        if employee is None or employee.hire_date is None:
            return None
        today = await self._org_today()
        months = await self._probation_months(employee)
        ends_on = probation_end(employee.hire_date, months)
        decision = await self._decisions.get(employee.id)
        on_probation = (
            tenure_status(employee.hire_date, today, probation_months=months)
            is TenureStatus.PROBATION
        )
        # Nothing to say: confirmed long ago with no outcome on file. Returning
        # a hollow record would put an empty card on everyone's dashboard.
        if not on_probation and decision is None:
            return None

        rows = {row.step_no: row for row in await self._items.list_for_employee(employee.id)}
        ack = rows.get(ACKNOWLEDGE_STEP)
        return MyProbationRead(
            employee_id=employee.id,
            hire_date=employee.hire_date,
            probation_end_date=ends_on,
            review_start_date=ends_on - timedelta(days=REVIEW_LEAD_DAYS),
            days_to_probation_end=(ends_on - today).days,
            is_on_probation=on_probation,
            decision=await self._decision_read(decision),
            # Only once there is something to acknowledge, and not for a
            # termination - Avora does not ask someone to sign off their own
            # dismissal.
            needs_acknowledgement=(
                decision is not None
                and decision.outcome is not ProbationOutcome.TERMINATED
                and (ack is None or ack.status is ProbationStepStatus.PENDING)
            ),
        )

    async def acknowledge(self, caller: CurrentUser) -> MyProbationRead | None:
        """The probationer signs off their own confirmation or extension letter.

        A dedicated action rather than `set_step`, because that one reads the
        whole checklist to answer - which is precisely what they may no longer
        see. This touches one step, the only one they own, and hands back the
        same narrow view as `mine`.
        """
        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise NotFoundError()
        decision = await self._decisions.get(employee.id)
        if decision is None or decision.outcome is ProbationOutcome.TERMINATED:
            # Nothing to acknowledge, and Avora never asks anyone to sign off
            # their own dismissal.
            raise ConflictError("There is no letter to acknowledge yet.")

        row = await self._items.get(employee.id, ACKNOWLEDGE_STEP)
        if row is None:
            row = ProbationChecklistItem(employee_id=employee.id, step_no=ACKNOWLEDGE_STEP)
            self._items.add(row)
        row.status = ProbationStepStatus.DONE
        row.note = "Acknowledged by the employee in Avora."
        row.actor_id = caller.employee_id
        row.acted_at = datetime.now(UTC)
        await self._items.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="probation.acknowledge",
            target=f"employee:{employee.id}",
        )
        return await self.mine(caller)

    async def _build(self, caller: CurrentUser, employee: Employee) -> ProbationChecklistRead:
        """Merge the template with this employee's progress. No audit: a write
        re-renders through here, and logging it as a READ every time would bury
        the actual step changes in the append-only trail."""
        today = await self._org_today()
        months = await self._probation_months(employee)
        ends_on = probation_end(employee.hire_date, months) if employee.hire_date else None
        rows = {row.step_no: row for row in await self._items.list_for_employee(employee.id)}
        decision = await self._decisions.get(employee.id)

        actor_ids = [row.actor_id for row in rows.values() if row.actor_id is not None]
        actors = await self._employees.get_many(actor_ids) if actor_ids else {}

        steps: list[ProbationStepRead] = []
        for position, step in enumerate(STEPS, start=1):
            row = rows.get(step.no)
            actor = actors.get(row.actor_id) if row and row.actor_id else None
            steps.append(
                ProbationStepRead(
                    no=step.no,
                    position=position,
                    title=step.title,
                    detail=list(step.detail),
                    owner=step.owner,
                    timeline=step.timeline,
                    status=row.status if row else ProbationStepStatus.PENDING,
                    note=row.note if row else None,
                    actor_id=row.actor_id if row else None,
                    actor_name=actor.full_name if actor else None,
                    acted_at=row.acted_at if row else None,
                    can_update=self._may_update_step(caller, employee, step.owner),
                )
            )

        return ProbationChecklistRead(
            employee_id=employee.id,
            employee_name=employee.full_name,
            hire_date=employee.hire_date,
            probation_end_date=ends_on,
            review_start_date=(
                ends_on - timedelta(days=REVIEW_LEAD_DAYS) if ends_on is not None else None
            ),
            days_to_probation_end=(ends_on - today).days if ends_on else None,
            is_on_probation=(
                employee.hire_date is not None
                and tenure_status(employee.hire_date, today, probation_months=months)
                is TenureStatus.PROBATION
            ),
            settled_steps=sum(1 for s in steps if s.status is not ProbationStepStatus.PENDING),
            total_steps=len(STEPS),
            steps=steps,
            decision=await self._decision_read(decision),
            can_decide=self._may_manage(caller),
        )

    async def _decision_read(
        self, decision: ProbationDecision | None
    ) -> ProbationDecisionRead | None:
        if decision is None:
            return None
        decider = (
            await self._employees.get(decision.decided_by)
            if decision.decided_by is not None
            else None
        )
        return ProbationDecisionRead(
            outcome=decision.outcome,
            effective_date=decision.effective_date,
            job_title=decision.job_title,
            note=decision.note,
            decided_by_name=decider.full_name if decider else None,
            decided_at=decision.decided_at,
            letter_sent_at=decision.letter_sent_at,
            original_end_date=decision.original_end_date,
            expectations=list(decision.expectations or []),
            letter_available=_letter_available(decision),
        )

    # ----------------------------------------------------------------- writes

    async def set_step(
        self,
        caller: CurrentUser,
        employee_id: uuid.UUID,
        step_no: int,
        payload: ProbationStepUpdate,
    ) -> ProbationChecklistRead:
        employee = await self._visible_employee(caller, employee_id)
        step = step_or_none(step_no)
        if step is None:
            raise ValidationError("That step is not part of the probation checklist.")
        if not self._may_update_step(caller, employee, step.owner):
            raise AuthorizationError("This step is not yours to complete.")

        row = await self._items.get(employee.id, step_no)
        if row is None:
            row = ProbationChecklistItem(employee_id=employee.id, step_no=step_no)
            self._items.add(row)
        row.status = payload.status
        row.note = payload.note
        # Pending means "undo": clear the trail rather than leaving a stale
        # signature on a step nobody has completed.
        settled = payload.status is not ProbationStepStatus.PENDING
        row.actor_id = caller.employee_id if settled else None
        row.acted_at = datetime.now(UTC) if settled else None
        await self._items.flush()

        await self._audit.append(
            actor=str(caller.employee_id),
            action="probation.step.update",
            target=f"employee:{employee.id}:step:{step_no}:{payload.status.value}",
        )
        return await self._build(caller, employee)

    async def decide(
        self,
        caller: CurrentUser,
        employee_id: uuid.UUID,
        payload: ProbationDecisionCreate,
        letter: EmailAttachment | None = None,
    ) -> ProbationChecklistRead:
        """Record the review's conclusion and, unless told not to, email it.

        HR/Admin only. The reporting manager contributes to the decision (step 7)
        but the letter is a company communication about someone's employment, so
        issuing it is people-ops, not line management.
        """
        employee = await self._visible_employee(caller, employee_id)
        if not self._may_manage(caller):
            raise AuthorizationError("Only HR or an admin can record a probation outcome.")

        _check_standard_letter_inputs(employee, payload, uploaded=letter is not None)

        decision = await self._decisions.get(employee.id)
        original_end = await self._end_before_extension(employee, decision, payload)
        effective_date = await self._apply_outcome(employee, decision, payload)
        if decision is None:
            decision = ProbationDecision(employee_id=employee.id, outcome=payload.outcome)
            self._decisions.add(decision)
        decision.outcome = payload.outcome
        decision.effective_date = effective_date
        decision.job_title = payload.job_title or employee.job_title
        decision.note = payload.note
        decision.original_end_date = original_end
        decision.expectations = list(payload.expectations) or None
        decision.decided_by = caller.employee_id
        decision.decided_at = datetime.now(UTC)
        # A re-decision has not been communicated yet, whatever the last one was.
        decision.letter_sent_at = None
        await self._decisions.flush()

        attachment = letter or await self._standard_letter(employee, decision)
        if payload.send_letter:
            # HR's own signed letter wins; otherwise the standard one from HR's template.
            await self._send_letter(employee, decision, attachment)
        await self._items.mark_done_by_system(
            employee.id, _decision_ticks(decision, has_letter=attachment is not None), _now()
        )

        await self._audit.append(
            actor=str(caller.employee_id),
            action="probation.decision.record",
            target=f"employee:{employee.id}:{payload.outcome.value}",
        )
        return await self._build(caller, employee)

    async def _end_before_extension(
        self,
        employee: Employee,
        previous: ProbationDecision | None,
        payload: ProbationDecisionCreate,
    ) -> date | None:
        """The end date an extension replaces - what its letter calls "originally
        set to end on". Read BEFORE `_apply_outcome` moves the window.

        A re-extension keeps the date from before the first one: the probation was
        originally due to end then, whatever intermediate end the last letter named.
        """
        if payload.outcome is not ProbationOutcome.EXTENDED or employee.hire_date is None:
            return None
        if (
            previous is not None
            and previous.outcome is ProbationOutcome.EXTENDED
            and previous.original_end_date is not None
        ):
            return previous.original_end_date
        return probation_end(employee.hire_date, await self._probation_months(employee))

    async def _apply_outcome(
        self,
        employee: Employee,
        previous: ProbationDecision | None,
        payload: ProbationDecisionCreate,
    ) -> date:
        """Make the outcome real for everything that reads the probation window
        (leave eligibility, the roster, tenure), and return the effective date
        to record.

        Probation is held as whole months from the joining date
        (`Employee.probation_months`), so:
          EXTENDED   - the end moves to the first monthly anniversary on or after
                       the date HR picked, and that is the date the letter names,
                       so nobody counts as confirmed before the letter says so.
          CONFIRMED  - if the window still runs past the confirmation date (an
                       early confirmation, or a reverted extension) it is cut
                       back to the last anniversary on or before it.
          TERMINATED - the window is left alone; offboarding ends employment.
        """
        if payload.outcome is ProbationOutcome.TERMINATED:
            return payload.effective_date
        hired_on = employee.hire_date
        if hired_on is None:
            raise ValidationError("Set a joining date before recording a probation outcome.")
        current_end = probation_end(hired_on, await self._probation_months(employee))

        if payload.outcome is ProbationOutcome.CONFIRMED:
            if current_end > payload.effective_date:
                employee.probation_months = max(
                    1, anniversaries_reached(hired_on, payload.effective_date)
                )
            return payload.effective_date

        months = months_to_reach(hired_on, payload.effective_date)
        new_end = probation_end(hired_on, months)
        if new_end <= await self._org_today():
            raise ValidationError("The new probation end date must be in the future.")
        re_extending = previous is not None and previous.outcome is ProbationOutcome.EXTENDED
        if not re_extending and new_end <= current_end:
            raise ValidationError(
                f"An extension must end after the current probation end date "
                f"({current_end:%d %b %Y})."
            )
        if months > MAX_PROBATION_MONTHS:
            raise ValidationError(
                f"Probation can run at most {MAX_PROBATION_MONTHS} months from the joining date."
            )
        employee.probation_months = months
        return new_end

    # ---------------------------------------------------------------- letters

    async def letter_pdf(self, caller: CurrentUser, employee_id: uuid.UUID) -> tuple[bytes, str]:
        """The standard letter for the recorded decision, re-rendered from the row.

        HR/Admin only, like issuing it. 404 when there is nothing to render: no
        decision, a termination (HR has no template for it), or an extension
        recorded without expectations.
        """
        employee = await self._visible_employee(caller, employee_id)
        if not self._may_manage(caller):
            raise AuthorizationError("Only HR or an admin can download probation letters.")
        decision = await self._decisions.get(employee.id)
        attachment = (
            await self._standard_letter(employee, decision) if decision is not None else None
        )
        if attachment is None:
            raise NotFoundError("There is no standard letter for this probation outcome.")
        await self._audit.append(
            actor=str(caller.employee_id),
            action="probation.letter.download",
            target=f"employee:{employee.id}:{attachment.filename}",
        )
        return attachment.content, attachment.filename

    async def _standard_letter(
        self, employee: Employee, decision: ProbationDecision
    ) -> EmailAttachment | None:
        """HR's template filled from the decision, or None when it cannot be."""
        if not _letter_available(decision) or decision.job_title is None:
            return None
        issued_on = await self._org_date(decision.decided_at or datetime.now(UTC))
        if decision.outcome is ProbationOutcome.CONFIRMED:
            pdf = render_confirmation_letter(
                ConfirmationLetter(
                    issued_on=issued_on,
                    employee_name=employee.full_name,
                    job_title=decision.job_title,
                    effective_date=decision.effective_date,
                )
            )
            kind = "confirmation"
        elif decision.original_end_date is not None and decision.expectations:
            pdf = render_extension_letter(
                ExtensionLetter(
                    issued_on=issued_on,
                    employee_name=employee.full_name,
                    job_title=decision.job_title,
                    original_end_date=decision.original_end_date,
                    new_end_date=decision.effective_date,
                    expectations=tuple(decision.expectations),
                )
            )
            kind = "extension"
        else:
            return None
        return EmailAttachment(
            filename=f"probation-{kind}-letter.pdf", content=pdf, content_type="application/pdf"
        )

    async def _send_letter(
        self,
        employee: Employee,
        decision: ProbationDecision,
        letter: EmailAttachment | None,
    ) -> None:
        """Email the outcome. Best-effort: the decision stands either way, and an
        unstamped `letter_sent_at` is how HR sees the send did not happen."""
        if decision.outcome is ProbationOutcome.TERMINATED:
            # HR supplied no termination template, and inventing the wording for a
            # dismissal is not ours to do. The outcome is recorded; the letter is
            # sent by hand.
            return
        when = decision.effective_date.strftime("%d %B %Y")
        try:
            if decision.outcome is ProbationOutcome.CONFIRMED:
                await self._email.send_probation_confirmed(
                    to=employee.work_email,
                    employee_name=employee.full_name,
                    job_title=decision.job_title or "your role",
                    effective_label=when,
                    letter=letter,
                )
            else:
                await self._email.send_probation_extended(
                    to=employee.work_email,
                    employee_name=employee.full_name,
                    new_end_label=when,
                    letter=letter,
                )
        except EmailError:
            logger.warning("probation letter failed for %s", employee.id)
            return
        decision.letter_sent_at = datetime.now(UTC)
        await self._decisions.flush()


def _now() -> datetime:
    return datetime.now(UTC)


# Checklist steps (stable `no` keys from app.core.probation) recording an outcome settles.
_STEP_DECISION = 7
_STEP_LETTER = 13
_STEP_HRMS = 15


def _decision_ticks(decision: ProbationDecision, *, has_letter: bool) -> dict[int, str]:
    """The steps recording `decision` settles, with the note each one leaves.

    A termination only settles the decision itself: its letter is written by
    hand and offboarding, not this record, updates the HRMS.
    """
    label = decision.outcome.value
    ticks = {_STEP_DECISION: f"outcome recorded ({label})."}
    if decision.outcome is ProbationOutcome.TERMINATED:
        return ticks
    ticks[_STEP_HRMS] = "probation end date and status updated from the recorded outcome."
    if has_letter:
        ticks[_STEP_LETTER] = (
            "letter prepared and emailed to the employee."
            if decision.letter_sent_at is not None
            else "letter prepared (not emailed from Avora)."
        )
    return ticks


def _letter_available(decision: ProbationDecision) -> bool:
    """Whether HR's template can be filled for this decision."""
    if decision.job_title is None:
        return False
    if decision.outcome is ProbationOutcome.CONFIRMED:
        return True
    if decision.outcome is ProbationOutcome.EXTENDED:
        return bool(decision.expectations) and decision.original_end_date is not None
    return False  # TERMINATED: HR supplied no template, and we do not invent one


def _check_standard_letter_inputs(
    employee: Employee, payload: ProbationDecisionCreate, *, uploaded: bool
) -> None:
    """Refuse up front, before anything is recorded, a decision whose standard
    letter is due but cannot be filled in."""
    generating = (
        payload.send_letter and not uploaded and payload.outcome is not ProbationOutcome.TERMINATED
    )
    if not generating:
        return
    if not (payload.job_title or employee.job_title):
        raise ValidationError("Add the job title the letter should name.")
    if payload.outcome is ProbationOutcome.EXTENDED and not payload.expectations:
        raise ValidationError(
            "List at least one expectation for the extension letter, or upload a signed letter."
        )
