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

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.probation import (
    RECENT_CONFIRMATION_DAYS,
    REVIEW_LEAD_DAYS,
    STEPS,
    StepOwner,
    step_or_none,
)
from app.core.tenure import TenureStatus, probation_end, tenure_status
from app.models.employee import Employee, Role
from app.models.probation_checklist import ProbationChecklistItem, ProbationStepStatus
from app.models.probation_decision import ProbationDecision, ProbationOutcome
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.probation_checklist import ProbationChecklistRepository
from app.repositories.probation_decision import ProbationDecisionRepository
from app.schemas.auth import CurrentUser
from app.schemas.probation import (
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
        spec = await self._attendance_policy.spec()
        return datetime.now(UTC).astimezone(ZoneInfo(spec.timezone)).date()

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
        candidates = await self._employees.all_in_scope(caller)
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

        decision = await self._decisions.get(employee.id)
        if decision is None:
            decision = ProbationDecision(employee_id=employee.id, outcome=payload.outcome)
            self._decisions.add(decision)
        decision.outcome = payload.outcome
        decision.effective_date = payload.effective_date
        decision.job_title = payload.job_title or employee.job_title
        decision.note = payload.note
        decision.decided_by = caller.employee_id
        decision.decided_at = datetime.now(UTC)
        # A re-decision has not been communicated yet, whatever the last one was.
        decision.letter_sent_at = None
        await self._decisions.flush()

        if payload.send_letter:
            await self._send_letter(employee, decision, letter)

        await self._audit.append(
            actor=str(caller.employee_id),
            action="probation.decision.record",
            target=f"employee:{employee.id}:{payload.outcome.value}",
        )
        return await self._build(caller, employee)

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
