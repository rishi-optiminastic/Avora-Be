"""Bootstrap an admin employee by work email (dev / ops).

The very first admin can't be created through the admin-only API (chicken &
egg), so this server-side script provisions one directly. It maps to a Better
Auth login by `work_email`, so after running it that account is authorised.

    uv run python scripts/seed_admin.py [email]   # defaults to the address below
"""

from __future__ import annotations

import asyncio
import sys

from app.db.session import SessionFactory, engine
from app.models.employee import Role
from app.repositories.employee import EmployeeRepository

DEFAULT_EMAIL = "tech1@optiminastic.com"


async def seed_admin(raw_email: str) -> str:
    email = raw_email.strip().lower()
    async with SessionFactory() as session:
        repo = EmployeeRepository(session)
        existing = await repo.get_by_work_email(email)
        if existing is not None:
            existing.role = Role.ADMIN
            existing.is_active = True
            await session.flush()
            action = "promoted existing employee to"
        else:
            await repo.create_from_invite(
                work_email=email,
                full_name=email.split("@", 1)[0].replace(".", " ").title(),
                role=Role.ADMIN,
            )
            action = "created"
        await session.commit()
    return f"{action} admin: {email}"


async def _main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EMAIL
    message = await seed_admin(email)
    await engine.dispose()
    print(message)  # CLI output is intentional


if __name__ == "__main__":
    asyncio.run(_main())
