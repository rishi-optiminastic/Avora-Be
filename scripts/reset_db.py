"""Wipe ALL application data (dev / ops), then re-seed the bootstrap admin.

Preserves migration state (`alembic_version`) and the Better Auth tables
(`auth_*`) so logins + JWKS keep working — only the app/monitoring/HR data is
cleared. Run `seed_admin.py` afterwards to recreate the bootstrap admin.

    uv run python scripts/reset_db.py

DESTRUCTIVE: every employee, device, activity sample, screenshot, task, leave,
payroll, etc. is permanently removed. There is no undo.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.session import SessionFactory, engine

_KEEP_EXACT = {"alembic_version"}
_KEEP_PREFIX = "auth_"  # Better Auth owns these (users/sessions/JWKS) — leave them.


async def reset() -> str:
    async with SessionFactory() as session:
        rows = await session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = [
            name
            for (name,) in rows
            if name not in _KEEP_EXACT and not name.startswith(_KEEP_PREFIX)
        ]
        if tables:
            quoted = ", ".join(f'"{t}"' for t in sorted(tables))
            await session.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        await session.commit()
    return f"cleared {len(tables)} app tables (kept auth_* + alembic_version)"


async def _main() -> None:
    print(await reset())  # CLI output is intentional
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
