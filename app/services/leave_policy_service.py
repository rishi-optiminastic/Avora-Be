"""Leave-policy business rules.

Everyone may READ the policy (people should see the leave entitlement they have);
only Admin/HR may change it. The first read materialises a default row so the
balance logic always has quotas to compute against.
"""

from __future__ import annotations

from app.core.exceptions import AuthorizationError
from app.models.employee import Role
from app.models.leave_policy import LeavePolicy
from app.repositories.audit import AuditRepository
from app.repositories.leave_policy import LeavePolicyRepository
from app.schemas.auth import CurrentUser
from app.schemas.leave_policy import LeavePolicyUpdate


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class LeavePolicyService:
    def __init__(self, policies: LeavePolicyRepository, audit: AuditRepository) -> None:
        self._policies = policies
        self._audit = audit

    async def get_or_create(self) -> LeavePolicy:
        return await self._policies.get() or await self._policies.create_default()

    async def update(self, caller: CurrentUser, payload: LeavePolicyUpdate) -> LeavePolicy:
        if not _can_manage(caller):
            raise AuthorizationError()
        policy = await self.get_or_create()
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(policy, key, value)
        policy.updated_by = caller.employee_id
        await self._policies.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="leave_policy.update",
            target=f"policy:{policy.id}",
        )
        return policy
