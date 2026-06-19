"""Monitoring read model — derives attendance and live activity from raw samples.

All reads are scoped to the caller's visible employee set (Security rule 5.3):
we resolve that set via the employee repository, then query activity only for
those ids. Derivation is intentionally simple and re-computable from raw data.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from app.core.categories import ProductivityCategory, classify
from app.core.exceptions import NotFoundError
from app.models.activity import ActivitySample
from app.repositories.activity import ActivityRepository, DailyAgg
from app.repositories.employee import EmployeeRepository
from app.repositories.work_session import WorkSessionRepository
from app.schemas.auth import CurrentUser
from app.schemas.monitoring import (
    ActivityNowRead,
    AttendanceRead,
    AttendanceStatus,
    BrowsingRead,
    DomainStat,
    InsightRead,
)

# Tunables (Phase 1 — move to settings/config-per-org later).
LATE_AFTER_HOUR = 10  # login at/after 10:00 UTC counts as "late"
IDLE_SAMPLE_SECONDS = 300  # a sample with ≥5 min idle marks the person idle
ONLINE_WINDOW_MINUTES = 15  # last sample within 15 min ⇒ currently online
TOP_DOMAINS = 6  # how many domains to surface per person on the browsing view
INSIGHT_WINDOW_DAYS = 7  # productivity trend length
FOCUS_AT_RISK_PCT = 50  # today's focus below this ⇒ at risk
DISTRACTING_AT_RISK_MIN = 60  # ≥ this many distracting minutes today ⇒ at risk


class MonitoringService:
    def __init__(
        self,
        activity: ActivityRepository,
        employees: EmployeeRepository,
        sessions: WorkSessionRepository,
    ) -> None:
        self._activity = activity
        self._employees = employees
        self._sessions = sessions

    @staticmethod
    def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
        start = datetime.combine(day.date(), time.min, tzinfo=UTC)
        return start, start + timedelta(days=1)

    @staticmethod
    def _row(employee_id: uuid.UUID, agg: DailyAgg | None) -> AttendanceRead:
        if agg is None:
            return AttendanceRead(
                employee_id=employee_id,
                status=AttendanceStatus.ABSENT,
                login_at=None,
                logout_at=None,
                worked_minutes=0,
                idle_minutes=0,
                active_minutes=0,
                productivity_pct=0,
            )
        worked = max(0, int((agg.logout_at - agg.login_at).total_seconds() // 60))
        idle = min(worked, agg.idle_seconds // 60)
        active = max(0, worked - idle)
        status = (
            AttendanceStatus.LATE
            if agg.login_at.astimezone(UTC).hour >= LATE_AFTER_HOUR
            else AttendanceStatus.PRESENT
        )
        return AttendanceRead(
            employee_id=employee_id,
            status=status,
            login_at=agg.login_at,
            logout_at=agg.logout_at,
            worked_minutes=worked,
            idle_minutes=idle,
            active_minutes=active,
            productivity_pct=round((active / worked) * 100) if worked else 0,
        )

    @staticmethod
    def _row_from_session(
        employee_id: uuid.UUID,
        span: tuple[datetime, datetime | None],
        agg: DailyAgg | None,
        now: datetime,
    ) -> AttendanceRead:
        """Attendance anchored on an explicit clock-in/out, with active/idle
        minutes still drawn from activity samples when we have them."""

        def _aware(dt: datetime) -> datetime:
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

        login_at = _aware(span[0])
        clock_out = _aware(span[1]) if span[1] is not None else None
        logout_at = clock_out or now  # open session ⇒ still working
        worked = max(0, int((logout_at - login_at).total_seconds() // 60))
        idle = min(worked, agg.idle_seconds // 60) if agg else 0
        active = max(0, worked - idle)
        status = (
            AttendanceStatus.LATE
            if login_at.astimezone(UTC).hour >= LATE_AFTER_HOUR
            else AttendanceStatus.PRESENT
        )
        return AttendanceRead(
            employee_id=employee_id,
            status=status,
            login_at=login_at,
            logout_at=logout_at,
            worked_minutes=worked,
            idle_minutes=idle,
            active_minutes=active,
            productivity_pct=round((active / worked) * 100) if worked else 0,
        )

    async def attendance(self, caller: CurrentUser, day: datetime) -> list[AttendanceRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start, end = self._day_bounds(day)
        aggs = await self._activity.daily_aggregates(ids, start, end)
        # Explicit clock-in/out wins over activity-inferred times when present.
        spans = await self._sessions.day_spans(ids, start, end)
        now = datetime.now(UTC)
        rows: list[AttendanceRead] = []
        for e in employees:
            span = spans.get(e.id)
            if span is not None:
                rows.append(self._row_from_session(e.id, span, aggs.get(e.id), now))
            else:
                rows.append(self._row(e.id, aggs.get(e.id)))
        return rows

    async def live_now(self, caller: CurrentUser, now: datetime) -> list[ActivityNowRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        since = now - timedelta(minutes=ONLINE_WINDOW_MINUTES)
        latest = await self._activity.latest_since(ids, since)
        result: list[ActivityNowRead] = []
        for e in employees:
            sample = latest.get(e.id)
            if sample is None:
                result.append(
                    ActivityNowRead(
                        employee_id=e.id,
                        online=False,
                        idle=False,
                        active_window=None,
                        idle_seconds=0,
                        last_seen_at=None,
                    )
                )
            else:
                result.append(
                    ActivityNowRead(
                        employee_id=e.id,
                        online=True,
                        idle=sample.idle_seconds >= IDLE_SAMPLE_SECONDS,
                        active_window=sample.active_window,
                        idle_seconds=sample.idle_seconds,
                        last_seen_at=sample.received_at,
                    )
                )
        return result

    @staticmethod
    def _browsing_row(employee_id: uuid.UUID, domains: list[tuple[str, int]]) -> BrowsingRead:
        stats = [
            DomainStat(domain=d, category=classify(d), minutes=minutes) for d, minutes in domains
        ]
        productive = sum(s.minutes for s in stats if s.category is ProductivityCategory.PRODUCTIVE)
        distracting = sum(
            s.minutes for s in stats if s.category is ProductivityCategory.DISTRACTING
        )
        total = sum(s.minutes for s in stats)
        top = sorted(stats, key=lambda s: s.minutes, reverse=True)[:TOP_DOMAINS]
        return BrowsingRead(
            employee_id=employee_id,
            total_minutes=total,
            productive_minutes=productive,
            neutral_minutes=max(0, total - productive - distracting),
            distracting_minutes=distracting,
            focus_pct=round((productive / total) * 100) if total else 0,
            top_domains=top,
        )

    async def browsing(self, caller: CurrentUser, day: datetime) -> list[BrowsingRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start, end = self._day_bounds(day)
        by_domain = await self._activity.browsing_by_domain(ids, start, end)
        return [self._browsing_row(e.id, by_domain.get(e.id, [])) for e in employees]

    @staticmethod
    def _category_minutes(domains: list[tuple[str, int]]) -> tuple[int, int, int, int]:
        """(productive, neutral, distracting, total) minutes for a day's domains."""
        productive = sum(c for d, c in domains if classify(d) is ProductivityCategory.PRODUCTIVE)
        distracting = sum(c for d, c in domains if classify(d) is ProductivityCategory.DISTRACTING)
        total = sum(c for _, c in domains)
        return productive, max(0, total - productive - distracting), distracting, total

    @classmethod
    def _insight_row(
        cls,
        employee_id: uuid.UUID,
        per_day: dict[date, list[tuple[str, int]]],
        days: list[date],
    ) -> InsightRead:
        trend: list[int] = []
        for d in days:
            prod, _, _, total = cls._category_minutes(per_day.get(d, []))
            trend.append(round(prod / total * 100) if total else 0)

        prod, neutral, distracting, total = cls._category_minutes(per_day.get(days[-1], []))
        focus = round(prod / total * 100) if total else 0
        at_risk = total > 0 and (
            focus < FOCUS_AT_RISK_PCT or distracting >= DISTRACTING_AT_RISK_MIN
        )
        return InsightRead(
            employee_id=employee_id,
            focus_pct=focus,
            productive_minutes=prod,
            neutral_minutes=neutral,
            distracting_minutes=distracting,
            total_minutes=total,
            at_risk=at_risk,
            trend=trend,
        )

    async def insights(self, caller: CurrentUser, today: datetime) -> list[InsightRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start, _ = self._day_bounds(today - timedelta(days=INSIGHT_WINDOW_DAYS - 1))
        _, end = self._day_bounds(today)
        by_day = await self._activity.browsing_by_day(ids, start, end)
        days = [(today - timedelta(days=i)).date() for i in range(INSIGHT_WINDOW_DAYS - 1, -1, -1)]
        return [self._insight_row(e.id, by_day.get(e.id, {}), days) for e in employees]

    async def timeline(
        self, caller: CurrentUser, employee_id: uuid.UUID, day: datetime
    ) -> Sequence[ActivitySample]:
        # Only people in the caller's scope (404 over 403 — don't leak existence).
        if not await self._employees.can_read(caller, employee_id):
            raise NotFoundError()
        start, end = self._day_bounds(day)
        return await self._activity.samples_for_employee(employee_id, start, end)
