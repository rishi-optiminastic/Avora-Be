"""Work-session business rules — the employee's own clock-in / clock-out.

Self-service only: a caller clocks themselves in/out (the morning login and the
evening logout). One open session at a time; clocking in while already clocked in
is idempotent (returns the open session). Every action is audited.

When the org enables location-restricted clock-in, a dashboard clock-in must fall
within an active office geofence (unless the employee is exempt). The browser GPS
is a *claim* — we validate its shape, verify the distance server-side, and store
the coordinates on the session; we never trust it as proof on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.exceptions import ValidationError
from app.core.geo import haversine_m, is_valid_coord
from app.models.work_session import WorkSession
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.office_location import OfficeLocationRepository
from app.repositories.work_session import WorkSessionRepository
from app.schemas.auth import CurrentUser
from app.services.attendance_policy_service import AttendancePolicyService


class WorkSessionService:
    def __init__(
        self,
        sessions: WorkSessionRepository,
        audit: AuditRepository,
        offices: OfficeLocationRepository,
        policy: AttendancePolicyService,
        employees: EmployeeRepository,
    ) -> None:
        self._sessions = sessions
        self._audit = audit
        self._offices = offices
        self._policy = policy
        self._employees = employees

    async def clock_in(
        self,
        caller: CurrentUser,
        *,
        ip_address: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> WorkSession:
        open_session = await self._sessions.get_open(caller.employee_id)
        if open_session is not None:
            return open_session  # already clocked in — idempotent

        matched = await self._enforce_location(caller, latitude, longitude)
        valid = is_valid_coord(latitude, longitude)
        session = await self._sessions.create(
            employee_id=caller.employee_id,
            clock_in_at=datetime.now(UTC),
            source="dashboard",
            ip_address=ip_address,
            location=matched,
            latitude=latitude if valid else None,
            longitude=longitude if valid else None,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.clock_in",
            target=f"session:{session.id}",
        )
        return session

    async def _enforce_location(
        self, caller: CurrentUser, latitude: float | None, longitude: float | None
    ) -> str | None:
        """Apply the office geofence. Returns the matched office name (stored on the
        session) or None when the check doesn't apply. Raises ValidationError when
        the org requires location and the caller isn't inside an office."""
        policy = await self._policy.get_or_create()
        if not policy.require_location_for_clock_in:
            return None
        employee = await self._employees.get(caller.employee_id)
        if employee is not None and employee.location_check_exempt:
            return None
        if not is_valid_coord(latitude, longitude):
            raise ValidationError(
                "Location is required to clock in. Turn on location access in your "
                "browser and try again."
            )
        offices = await self._offices.list_active()
        if not offices:
            raise ValidationError(
                "Location-restricted clock-in is on but no office locations are set. "
                "Ask an admin to add one."
            )
        assert latitude is not None and longitude is not None  # narrowed above
        nearest_name, nearest_gap = None, float("inf")
        for office in offices:
            distance = haversine_m(latitude, longitude, office.latitude, office.longitude)
            if distance <= office.radius_m:
                return office.name
            gap = distance - office.radius_m
            if gap < nearest_gap:
                nearest_name, nearest_gap = office.name, gap
        raise ValidationError(
            f"You must be at an office to clock in. Nearest is {nearest_name}, "
            f"about {round(nearest_gap)} m away."
        )

    async def clock_out(self, caller: CurrentUser) -> WorkSession:
        session = await self._sessions.get_open(caller.employee_id)
        if session is None:
            raise ValidationError("You are not clocked in.")
        session.clock_out_at = datetime.now(UTC)
        session.clock_out_source = "dashboard"
        await self._sessions.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="attendance.clock_out",
            target=f"session:{session.id}",
        )
        return session

    async def current(self, caller: CurrentUser) -> WorkSession | None:
        return await self._sessions.get_open(caller.employee_id)
