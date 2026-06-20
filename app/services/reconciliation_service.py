"""Attendance reconciliation — biometric punches vs laptop-agent activity.

"Run both, reconcile" (the product owner's choice): biometric `WorkSession`s are
the formal attendance record, but we also compare them against the agent's
activity for the same local day and flag mismatches — punched-but-no-activity,
active-but-no-punch, or login/logout times that disagree beyond a tolerance.
Reads are scoped to the caller via `all_in_scope` (Security rule 5.3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.repositories.activity import ActivityRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.work_session import WorkSessionRepository
from app.schemas.auth import CurrentUser
from app.schemas.reconciliation import ReconciliationReport, ReconciliationRow, ReconStatus
from app.services.attendance_policy_service import AttendancePolicyService

TOLERANCE_MINUTES = 30  # login/logout disagreement beyond this is a "time_gap"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _worked(login: datetime | None, logout: datetime | None) -> int | None:
    if login is None or logout is None:
        return None
    return max(0, int((_aware(logout) - _aware(login)).total_seconds() // 60))


def _gap(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return abs(int((_aware(a) - _aware(b)).total_seconds() // 60))


class ReconciliationService:
    def __init__(
        self,
        employees: EmployeeRepository,
        sessions: WorkSessionRepository,
        activity: ActivityRepository,
        policy: AttendancePolicyService,
    ) -> None:
        self._employees = employees
        self._sessions = sessions
        self._activity = activity
        self._policy = policy

    async def for_day(self, caller: CurrentUser, date: str | None) -> ReconciliationReport:
        spec = await self._policy.spec()
        tz = ZoneInfo(spec.timezone)
        local_date = date or datetime.now(UTC).astimezone(tz).date().isoformat()
        y, m, d = (int(x) for x in local_date.split("-"))
        start = datetime(y, m, d, tzinfo=tz)
        start_utc, end_utc = start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        bio = await self._sessions.biometric_day_spans(ids, start_utc, end_utc)
        agent = await self._activity.daily_aggregates(ids, start_utc, end_utc)

        rows: list[ReconciliationRow] = []
        matched = 0
        for e in employees:
            b = bio.get(e.id)
            a = agent.get(e.id)
            if b is None and a is None:
                continue  # absent in both — not a discrepancy, skip
            b_login = b.login_at if b else None
            b_logout = b.logout_at if b else None
            a_login = a.login_at if a else None
            a_logout = a.logout_at if a else None
            status, flags = self._classify(b_login, b_logout, a_login, a_logout)
            if status == "match":
                matched += 1
            rows.append(
                ReconciliationRow(
                    employee_id=e.id,
                    name=e.full_name,
                    department=e.department,
                    biometric_login=b_login,
                    biometric_logout=b_logout,
                    biometric_worked_minutes=_worked(b_login, b_logout),
                    agent_login=a_login,
                    agent_logout=a_logout,
                    agent_worked_minutes=_worked(a_login, a_logout),
                    status=status,
                    flags=flags,
                )
            )

        rows.sort(key=lambda r: (r.status == "match", r.name))
        return ReconciliationReport(
            date=local_date,
            timezone=spec.timezone,
            tolerance_minutes=TOLERANCE_MINUTES,
            employees=len(rows),
            matched=matched,
            discrepancies=len(rows) - matched,
            rows=rows,
        )

    @staticmethod
    def _classify(
        b_login: datetime | None,
        b_logout: datetime | None,
        a_login: datetime | None,
        a_logout: datetime | None,
    ) -> tuple[ReconStatus, list[str]]:
        has_bio = b_login is not None
        has_agent = a_login is not None
        if has_bio and not has_agent:
            return "no_activity", ["Punched in but no laptop activity"]
        if has_agent and not has_bio:
            return "no_punch", ["Laptop active but no biometric punch"]

        flags: list[str] = []
        in_gap = _gap(b_login, a_login)
        out_gap = _gap(b_logout, a_logout)
        if in_gap is not None and in_gap > TOLERANCE_MINUTES:
            flags.append(f"Login differs by {in_gap} min")
        if out_gap is not None and out_gap > TOLERANCE_MINUTES:
            flags.append(f"Logout differs by {out_gap} min")
        return ("time_gap", flags) if flags else ("match", [])
