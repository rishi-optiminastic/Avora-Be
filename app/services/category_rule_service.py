"""Category-rule business rules.

Admins manage any rule (incl. org-wide). Senior managers and managers may manage
rules ONLY for their own department (never org-wide) — so a manager can't change
how another team's activity is classified (Security rule 5.3). Every write is
audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.category_rule import CategoryRule
from app.models.employee import Role
from app.repositories.audit import AuditRepository
from app.repositories.category_rule import CategoryRuleRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.category_rule import CategoryRuleCreate, CategoryRuleUpdate

_DEPT_MANAGERS = (Role.SENIOR_MANAGER, Role.MANAGER)


class CategoryRuleService:
    def __init__(
        self,
        rules: CategoryRuleRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._rules = rules
        self._employees = employees
        self._audit = audit

    async def _caller_department(self, caller: CurrentUser) -> str | None:
        employee = await self._employees.get(caller.employee_id)
        return employee.department if employee else None

    async def _assert_can_manage(self, caller: CurrentUser, department: str | None) -> None:
        if caller.is_admin:
            return
        if caller.role in _DEPT_MANAGERS:
            own = await self._caller_department(caller)
            if department is not None and own is not None and department == own:
                return
        raise AuthorizationError()

    async def list_visible(self, caller: CurrentUser) -> Sequence[CategoryRule]:
        if not (caller.is_admin or caller.role in _DEPT_MANAGERS):
            raise AuthorizationError()
        return await self._rules.list_all()

    async def create(self, caller: CurrentUser, payload: CategoryRuleCreate) -> CategoryRule:
        if not payload.assignable():
            raise ValidationError("That category can't be assigned by a rule.")
        await self._assert_can_manage(caller, payload.department)
        rule = await self._rules.create(payload, actor_id=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="category_rule.create",
            target=f"rule:{rule.id}",
        )
        return rule

    async def update(
        self, caller: CurrentUser, rule_id: uuid.UUID, payload: CategoryRuleUpdate
    ) -> CategoryRule:
        rule = await self._rules.get(rule_id)
        if rule is None:
            raise NotFoundError()
        await self._assert_can_manage(caller, rule.department)
        fields = payload.model_dump(exclude_unset=True)
        if "department" in fields:  # moving scope needs rights on the destination too
            await self._assert_can_manage(caller, fields["department"])
        for key, value in fields.items():
            setattr(rule, key, value)
        rule.updated_by = caller.employee_id
        await self._rules.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="category_rule.update",
            target=f"rule:{rule.id}",
        )
        return rule

    async def delete(self, caller: CurrentUser, rule_id: uuid.UUID) -> None:
        rule = await self._rules.get(rule_id)
        if rule is None:
            raise NotFoundError()
        await self._assert_can_manage(caller, rule.department)
        await self._rules.delete(rule)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="category_rule.delete",
            target=f"rule:{rule_id}",
        )
