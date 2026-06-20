"""Attendance-policy business rules.

Everyone may READ the policy (people should see the timings they're held to);
only Admin/HR may change it. The first read materialises a default row so the
rest of the system always has a policy to classify against.
"""

from __future__ import annotations

from app.core.attendance import PolicySpec
from app.core.exceptions import AuthorizationError
from app.models.attendance_policy import AttendancePolicy
from app.models.employee import Role
from app.repositories.attendance_policy import AttendancePolicyRepository
from app.repositories.audit import AuditRepository
from app.schemas.attendance_policy import AttendancePolicyUpdate, to_minutes
from app.schemas.auth import CurrentUser


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class AttendancePolicyService:
    def __init__(self, policies: AttendancePolicyRepository, audit: AuditRepository) -> None:
        self._policies = policies
        self._audit = audit

    async def get_or_create(self) -> AttendancePolicy:
        return await self._policies.get() or await self._policies.create_default()

    async def spec(self) -> PolicySpec:
        """The policy as a pure spec for the classifier (used by reads)."""
        p = await self.get_or_create()
        return PolicySpec(
            work_start_minute=p.work_start_minute,
            work_end_minute=p.work_end_minute,
            buffer_minutes=p.buffer_minutes,
            regularization_window_minutes=p.regularization_window_minutes,
            full_day_min_minutes=p.full_day_min_minutes,
            half_day_min_minutes=p.half_day_min_minutes,
            monthly_regularizations=p.monthly_regularizations,
            timezone=p.timezone,
        )

    async def update(
        self, caller: CurrentUser, payload: AttendancePolicyUpdate
    ) -> AttendancePolicy:
        if not _can_manage(caller):
            raise AuthorizationError()
        policy = await self.get_or_create()
        fields = payload.model_dump(exclude_unset=True)
        if "work_start" in fields:
            policy.work_start_minute = to_minutes(fields.pop("work_start"))
        if "work_end" in fields:
            policy.work_end_minute = to_minutes(fields.pop("work_end"))
        for key, value in fields.items():
            setattr(policy, key, value)
        policy.updated_by = caller.employee_id
        await self._policies.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance_policy.update",
            target=f"policy:{policy.id}",
        )
        return policy
