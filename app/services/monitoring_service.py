"""Monitoring read model — derives attendance and live activity from raw samples.

All reads are scoped to the caller's visible employee set (Security rule 5.3):
we resolve that set via the employee repository, then query activity only for
those ids. Derivation is intentionally simple and re-computable from raw data.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from app.core.categories import CategoryResolver, ProductivityCategory
from app.core.exceptions import NotFoundError
from app.models.activity import ActivitySample
from app.repositories.activity import ActivityRepository
from app.repositories.category_rule import CategoryRuleRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.monitoring import (
    ActivityNowRead,
    BrowsingRead,
    DomainStat,
    InsightRead,
)

# Tunables (Phase 1 — move to settings/config-per-org later).
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
        rules: CategoryRuleRepository,
    ) -> None:
        self._activity = activity
        self._employees = employees
        self._rules = rules

    @staticmethod
    def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
        start = datetime.combine(day.date(), time.min, tzinfo=UTC)
        return start, start + timedelta(days=1)

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
    def _browsing_row(
        employee_id: uuid.UUID,
        domains: list[tuple[str, int]],
        resolver: CategoryResolver,
        department: str | None,
    ) -> BrowsingRead:
        stats = [
            DomainStat(
                domain=d,
                category=(cat := resolver.classify(domain=d, department=department)),
                minutes=minutes,
                flagged=cat
                in (ProductivityCategory.UNPRODUCTIVE, ProductivityCategory.REQUIRES_REVIEW),
            )
            for d, minutes in domains
        ]
        productive = sum(s.minutes for s in stats if s.category is ProductivityCategory.PRODUCTIVE)
        unproductive = sum(
            s.minutes for s in stats if s.category is ProductivityCategory.UNPRODUCTIVE
        )
        total = sum(s.minutes for s in stats)
        top = sorted(stats, key=lambda s: s.minutes, reverse=True)[:TOP_DOMAINS]
        return BrowsingRead(
            employee_id=employee_id,
            total_minutes=total,
            productive_minutes=productive,
            neutral_minutes=max(0, total - productive - unproductive),
            unproductive_minutes=unproductive,
            focus_pct=round((productive / total) * 100) if total else 0,
            top_domains=top,
        )

    async def _resolver(self) -> CategoryResolver:
        return CategoryResolver(await self._rules.specs())

    async def browsing(self, caller: CurrentUser, day: datetime) -> list[BrowsingRead]:
        employees = await self._employees.all_in_scope(caller)
        ids = [e.id for e in employees]
        start, end = self._day_bounds(day)
        by_domain = await self._activity.browsing_by_domain(ids, start, end)
        resolver = await self._resolver()
        return [
            self._browsing_row(e.id, by_domain.get(e.id, []), resolver, e.department)
            for e in employees
        ]

    @staticmethod
    def _category_minutes(
        domains: list[tuple[str, int]], resolver: CategoryResolver, department: str | None
    ) -> tuple[int, int, int, int]:
        """(productive, neutral, unproductive, total) minutes for a day's domains."""
        productive = sum(
            c
            for d, c in domains
            if resolver.classify(domain=d, department=department) is ProductivityCategory.PRODUCTIVE
        )
        unproductive = sum(
            c
            for d, c in domains
            if resolver.classify(domain=d, department=department)
            is ProductivityCategory.UNPRODUCTIVE
        )
        total = sum(c for _, c in domains)
        return productive, max(0, total - productive - unproductive), unproductive, total

    @classmethod
    def _insight_row(
        cls,
        employee_id: uuid.UUID,
        per_day: dict[date, list[tuple[str, int]]],
        days: list[date],
        resolver: CategoryResolver,
        department: str | None,
    ) -> InsightRead:
        trend: list[int] = []
        for d in days:
            prod, _, _, total = cls._category_minutes(per_day.get(d, []), resolver, department)
            trend.append(round(prod / total * 100) if total else 0)

        prod, neutral, unproductive, total = cls._category_minutes(
            per_day.get(days[-1], []), resolver, department
        )
        focus = round(prod / total * 100) if total else 0
        at_risk = total > 0 and (
            focus < FOCUS_AT_RISK_PCT or unproductive >= DISTRACTING_AT_RISK_MIN
        )
        return InsightRead(
            employee_id=employee_id,
            focus_pct=focus,
            productive_minutes=prod,
            neutral_minutes=neutral,
            unproductive_minutes=unproductive,
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
        resolver = await self._resolver()
        return [
            self._insight_row(e.id, by_day.get(e.id, {}), days, resolver, e.department)
            for e in employees
        ]

    async def timeline(
        self, caller: CurrentUser, employee_id: uuid.UUID, day: datetime
    ) -> Sequence[ActivitySample]:
        # Only people in the caller's scope (404 over 403 — don't leak existence).
        if not await self._employees.can_read(caller, employee_id):
            raise NotFoundError()
        start, end = self._day_bounds(day)
        return await self._activity.samples_for_employee(employee_id, start, end)
