"""Keep Avora's copy of pay in step with Circle.

Circle wins for pay: HR edits it only in Circle, and payroll runs off the copy
kept here. Only the fields Circle owns are written (CTC, currency, period,
PF); the target bonus, note and every bank field stay Avora's. Bank details are
never synced: Circle holds the person's personal account, while Avora holds the
corporate salary account payroll pays into. Rules:

- A value Circle has replaces Avora's.
- A value Circle lacks (or sends in a form Avora would reject) never wipes
  Avora's: the existing value stays and the field NAME is logged.
- The effective date is filled from Circle's joining date only when empty and
  Circle's CTC is being applied.

Every applied change is audited as `circle-sync`, naming the fields, never
the values. Run per employee by `worker/circle_documents_scheduler.py`; the
caller commits.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.compensation import Compensation, PayPeriod
from app.models.employee import Employee
from app.repositories.audit import AuditRepository
from app.repositories.compensation import CircleValue, CompensationRepository
from app.services.circle_client import CircleClient
from app.services.circle_document_sync import SYNC_ACTOR
from app.services.circle_values import amount_minor, parse_date

logger = get_logger("app.circle_compensation")

CIRCLE_CURRENCY = "INR"


def circle_values(data: dict[str, Any]) -> dict[str, CircleValue]:
    """What Circle's reply says, as Avora columns; None = Circle has nothing
    usable for that field. Circle's bank block is ignored (see module doc)."""
    amount = amount_minor(data.get("annual_ctc_inr"))
    pf = data.get("pf_enabled")
    return {
        "amount_minor": amount,
        "currency": CIRCLE_CURRENCY if amount is not None else None,
        "period": PayPeriod.ANNUAL if amount is not None else None,
        "pf_enabled": pf if isinstance(pf, bool) else None,
    }


class CircleCompensationSync:
    def __init__(
        self,
        compensation: CompensationRepository,
        audit: AuditRepository,
        settings: Settings,
        client: CircleClient,
    ) -> None:
        self._compensation = compensation
        self._audit = audit
        self._settings = settings
        self._client = client

    async def sync(self, employee: Employee) -> list[str]:
        """Apply Circle's pay; returns the names of the fields changed."""
        try:
            data = await self._client.compensation(employee.work_email)
        except NotFoundError:
            return []
        record = await self._compensation.get_for_employee(employee.id)
        changes, kept = self._diff(record, circle_values(data))
        joining = parse_date(data.get("joining_date"))
        if joining is not None and _lacks_effective_date(record, changes):
            changes["effective_date"] = joining
        if kept:
            logger.info(
                "circle_compensation_kept",
                extra={"employee": str(employee.id), "fields": sorted(kept)},
            )
        if not changes:
            return []
        await self._compensation.apply_circle(employee.id, changes)
        changed = sorted(changes)
        await self._audit.append(
            actor=SYNC_ACTOR,
            action="compensation.circle_sync",
            target=f"employee:{employee.id}:{','.join(changed)}",
        )
        return changed

    def _diff(
        self, record: Compensation | None, incoming: dict[str, CircleValue]
    ) -> tuple[dict[str, CircleValue], list[str]]:
        """The columns to write, and the fields Avora keeps because Circle had
        nothing for them."""
        changes: dict[str, CircleValue] = {}
        kept: list[str] = []
        for field, value in incoming.items():
            current = self._current(record, field)
            if value is None:
                if current is not None:
                    kept.append(field)
                continue
            if value != current:
                changes[field] = value
        return changes, kept

    def _current(self, record: Compensation | None, field: str) -> CircleValue:
        if record is None:
            return None
        value: CircleValue = getattr(record, field)
        return value


def _lacks_effective_date(record: Compensation | None, changes: dict[str, CircleValue]) -> bool:
    """Fill-only, and only alongside Circle's CTC: the joining date says when
    Circle's pay began, not when a figure HR entered in Avora did."""
    if "amount_minor" not in changes:
        return False
    return record is None or record.effective_date is None
