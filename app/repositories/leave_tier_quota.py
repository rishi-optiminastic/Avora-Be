"""Tenure-tier leave-quota data access.

Reads are org-wide (the tiers are policy, not per-person data), so there is no
row-scope clause here — the *service* gates who may write, mirroring
`LeavePolicyRepository`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenure import TenureStatus
from app.models.leave import LeaveType
from app.models.leave_tier_quota import LeaveTierQuota

# The entitlement each band grants, seeded on first read so the balance engine
# always has something to resolve against. `None` for a leave type simply means
# "no row" — that band inherits the org LeavePolicy for it.
#
#   probation — 4 sick, plus bereavement, which the policy grants "from Day 1,
#               including during probation". Everything else is a PERMANENT-
#               employee benefit and is withheld until confirmation: planned,
#               annual, birthday, marriage, paternity and maternity.
#   confirmed — sick rises to 6 (the probation 4 plus 2 more, counted against the
#               same leave year so days already taken still count), and planned
#               leave starts accruing at 1/month toward its annual 8. Annual leave
#               still needs a full year, so it stays at 0.
#   tenured   — deliberately ABSENT, and that is the design: a band expresses a
#               tenure RESTRICTION, while the full entitlement lives in the org
#               LeavePolicy, which HR edits in Settings. Hard-coding tenured here
#               would quietly take planned/annual/sick away from that screen.
#
# Planned/annual/sick are three SEPARATE balances (HR confirmed), not one pooled
# 20 — sick leave can never be spent as planned time off.
_SEED: dict[TenureStatus, dict[LeaveType, tuple[int | None, float | None]]] = {
    TenureStatus.PROBATION: {
        LeaveType.SICK: (4, None),
        LeaveType.BIRTHDAY: (0, None),
        LeaveType.PLANNED: (0, None),
        LeaveType.ANNUAL: (0, None),
        LeaveType.MARRIAGE: (0, None),
        LeaveType.PATERNITY: (0, None),
        LeaveType.MATERNITY: (0, None),
        # Bereavement is deliberately absent: it applies from Day 1, so it falls
        # through to the org policy like it does for everyone else.
    },
    TenureStatus.CONFIRMED: {
        LeaveType.SICK: (6, None),
        # Everything a permanent employee gets is now unlocked; only annual leave
        # still waits for a full year of service.
        LeaveType.BIRTHDAY: (1, None),
        LeaveType.PLANNED: (None, 1.0),  # 1 a month, capped by the org policy
        LeaveType.ANNUAL: (0, None),
    },
}


class LeaveTierQuotaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> Sequence[LeaveTierQuota]:
        rows = await self._session.scalars(select(LeaveTierQuota))
        return list(rows)

    async def list_for_tier(self, tier: TenureStatus) -> Sequence[LeaveTierQuota]:
        rows = await self._session.scalars(
            select(LeaveTierQuota).where(LeaveTierQuota.tier == tier)
        )
        return list(rows)

    async def seed_defaults(self) -> Sequence[LeaveTierQuota]:
        """Materialise the default tier rows once, then return every row.

        Idempotent and additive: an existing (tier, type) row is left exactly as
        the org edited it, so seeding can never quietly undo a policy change.
        """
        existing = {(row.tier, row.leave_type) for row in await self.list_all()}
        added = False
        for tier, quotas in _SEED.items():
            for leave_type, (annual_days, accrual) in quotas.items():
                if (tier, leave_type) in existing:
                    continue
                self._session.add(
                    LeaveTierQuota(
                        tier=tier,
                        leave_type=leave_type,
                        annual_days=annual_days,
                        monthly_accrual_days=accrual,
                    )
                )
                added = True
        if added:
            await self._session.flush()
        return await self.list_all()

    async def upsert(
        self,
        *,
        tier: TenureStatus,
        leave_type: LeaveType,
        annual_days: int | None,
        monthly_accrual_days: float | None,
        updated_by: uuid.UUID,
    ) -> LeaveTierQuota:
        record: LeaveTierQuota | None = await self._session.scalar(
            select(LeaveTierQuota).where(
                LeaveTierQuota.tier == tier, LeaveTierQuota.leave_type == leave_type
            )
        )
        if record is None:
            record = LeaveTierQuota(tier=tier, leave_type=leave_type)
            self._session.add(record)
        record.annual_days = annual_days
        record.monthly_accrual_days = monthly_accrual_days
        record.updated_by = updated_by
        await self._session.flush()
        return record
