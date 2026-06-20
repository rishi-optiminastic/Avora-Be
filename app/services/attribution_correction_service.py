"""Attribution-correction business rules.

An employee proposes a correction for THEIR OWN work (never someone else's). A
manager/admin who can see that employee reviews it — but never their own
proposal (separation of duties). Listing is scoped: you see your own corrections,
a manager sees their reports'. Every action is audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.attribution_correction import AttributionCorrection, CorrectionStatus
from app.repositories.attribution_correction import AttributionCorrectionRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_entity import WorkEntityRepository
from app.schemas.attribution_correction import CorrectionCreate, CorrectionReview
from app.schemas.auth import CurrentUser


class AttributionCorrectionService:
    def __init__(
        self,
        corrections: AttributionCorrectionRepository,
        employees: EmployeeRepository,
        entities: WorkEntityRepository,
        audit: AuditRepository,
    ) -> None:
        self._corrections = corrections
        self._employees = employees
        self._entities = entities
        self._audit = audit

    async def propose(
        self, caller: CurrentUser, payload: CorrectionCreate
    ) -> AttributionCorrection:
        if payload.entity_id is not None:
            entity = await self._entities.get(payload.entity_id)
            if entity is None or not entity.is_active:
                raise ValidationError("Unknown or inactive project.")
        correction = await self._corrections.create(payload, employee_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attribution.correction_proposed",
            target=f"correction:{correction.id}",
        )
        return correction

    async def list_for_caller(
        self, caller: CurrentUser, *, status: CorrectionStatus | None = None
    ) -> Sequence[AttributionCorrection]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        return await self._corrections.list_for_employees(ids, status=status)

    async def review(
        self, caller: CurrentUser, correction_id: uuid.UUID, payload: CorrectionReview
    ) -> AttributionCorrection:
        correction = await self._corrections.get(correction_id)
        # 404 (not 403) when out of scope — never leak existence (rule 5.3).
        if correction is None or not await self._employees.can_read(caller, correction.employee_id):
            raise NotFoundError()
        if not caller.is_manager:
            raise AuthorizationError()
        if correction.employee_id == caller.employee_id:
            raise AuthorizationError()  # can't review your own proposal
        if correction.status is not CorrectionStatus.PENDING:
            raise ValidationError("This correction was already reviewed.")
        correction.status = (
            CorrectionStatus.APPROVED if payload.approve else CorrectionStatus.REJECTED
        )
        correction.reviewed_by_id = caller.employee_id
        correction.review_note = payload.review_note
        correction.updated_at = datetime.now(UTC)
        await self._corrections.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action=f"attribution.correction_{correction.status.value}",
            target=f"correction:{correction.id}",
        )
        return correction
