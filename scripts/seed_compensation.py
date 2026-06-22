"""Prefill employee compensation so payroll has real data (dev / ops).

For every ACTIVE employee without a compensation row, insert one in INR, MONTHLY,
with a role-banded amount (deterministic from the email so re-runs are stable).
`tech1@optiminastic.com` is set explicitly to ₹50,000/mo — the reference slip.
Existing compensation rows are left untouched (except tech1, kept in sync).

    uv run python scripts/seed_compensation.py
"""

from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy import select

from app.db.session import SessionFactory, engine
from app.models.compensation import PayPeriod
from app.models.employee import Employee, EmployeeStatus, Role
from app.repositories.compensation import CompensationRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.compensation import CompensationWrite

REFERENCE_EMAIL = "tech1@optiminastic.com"
REFERENCE_MONTHLY_MINOR = 50_000_00  # ₹50,000/mo

# Role → (low, high) monthly CTC in whole rupees; the band is sampled
# deterministically per employee so the payroll list looks varied but stable.
_BANDS: dict[Role, tuple[int, int]] = {
    Role.ADMIN: (80_000, 120_000),
    Role.SENIOR_MANAGER: (90_000, 130_000),
    Role.HR: (60_000, 85_000),
    Role.MANAGER: (60_000, 90_000),
    Role.EXECUTIVE: (70_000, 100_000),
    Role.EMPLOYEE: (35_000, 60_000),
    Role.IT_ADMIN: (45_000, 70_000),
    Role.VIEWER: (30_000, 45_000),
}


def _banded_monthly_minor(email: str, role: Role) -> int:
    low, high = _BANDS.get(role, (35_000, 60_000))
    digest = int(hashlib.sha256(email.encode()).hexdigest(), 16)
    step = 1_000  # round to a clean thousand
    span = (high - low) // step + 1
    rupees = low + (digest % span) * step
    return rupees * 100


async def seed_compensation() -> str:
    created = 0
    skipped = 0
    async with SessionFactory() as session:
        comp_repo = CompensationRepository(session)
        emp_repo = EmployeeRepository(session)

        # tech1: always pin to the reference structure.
        reference = await emp_repo.get_by_work_email(REFERENCE_EMAIL)
        if reference is not None:
            await comp_repo.upsert(
                reference.id,
                CompensationWrite(
                    amount_minor=REFERENCE_MONTHLY_MINOR,
                    currency="INR",
                    period=PayPeriod.MONTHLY,
                    note="Reference compensation",
                ),
                updated_by=reference.id,
            )

        rows = await session.execute(
            select(Employee).where(Employee.status == EmployeeStatus.ACTIVE)
        )
        for employee in rows.scalars().all():
            if employee.work_email == REFERENCE_EMAIL:
                continue
            if await comp_repo.get_for_employee(employee.id) is not None:
                skipped += 1
                continue
            await comp_repo.upsert(
                employee.id,
                CompensationWrite(
                    amount_minor=_banded_monthly_minor(employee.work_email, employee.role),
                    currency="INR",
                    period=PayPeriod.MONTHLY,
                    note="Seeded compensation",
                ),
                updated_by=employee.id,
            )
            created += 1
        await session.commit()
    return f"compensation seeded: {created} created, {skipped} already set"


async def _main() -> None:
    message = await seed_compensation()
    await engine.dispose()
    print(message)  # CLI output is intentional


if __name__ == "__main__":
    asyncio.run(_main())
