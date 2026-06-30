"""Personal browsing-privacy rules.

A single configured owner (by work email) may curate a list of domains hidden
from the Browsing tab. This is intentionally NOT an org-wide capability: letting
every employee hide their own browsing would defeat the monitoring tool. Every
method first asserts the caller IS that owner; anyone else gets a 404 so the
feature stays invisible (rule 5.3 — don't leak existence).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.categories import extract_domain
from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.browsing_hidden_domain import BrowsingHiddenDomain
from app.repositories.audit import AuditRepository
from app.repositories.browsing_hidden_domain import BrowsingHiddenDomainRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser


class BrowsingPrivacyService:
    def __init__(
        self,
        hidden: BrowsingHiddenDomainRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._hidden = hidden
        self._employees = employees
        self._audit = audit
        self._settings = settings

    async def _require_owner(self, caller: CurrentUser) -> None:
        owner = self._settings.private_browsing_owner_email.strip().lower()
        if not owner:  # capability disabled entirely
            raise NotFoundError()
        employee = await self._employees.get(caller.employee_id)
        if employee is None or employee.work_email.strip().lower() != owner:
            raise NotFoundError()

    async def list(self, caller: CurrentUser) -> Sequence[BrowsingHiddenDomain]:
        await self._require_owner(caller)
        return await self._hidden.list_for_employee(caller.employee_id)

    async def add(self, caller: CurrentUser, raw_domain: str) -> BrowsingHiddenDomain:
        await self._require_owner(caller)
        domain = extract_domain(raw_domain)
        if not domain:
            raise ValidationError("Enter a valid domain, e.g. facebook.com")
        row = await self._hidden.add(caller.employee_id, domain)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="browsing.hide_domain",
            target=f"domain:{domain}",
        )
        return row

    async def remove(self, caller: CurrentUser, hidden_id: uuid.UUID) -> None:
        await self._require_owner(caller)
        removed = await self._hidden.remove(caller.employee_id, hidden_id)
        if removed == 0:
            raise NotFoundError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="browsing.unhide_domain",
            target=f"hidden_domain:{hidden_id}",
        )
