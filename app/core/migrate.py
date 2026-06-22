"""Apply Alembic migrations programmatically on startup.

Why: with `uvicorn --reload`, editing a model reloads the server immediately, so
the ORM starts SELECTing a new column a beat before `alembic upgrade head` is run
by hand — the recurring `UndefinedColumn` 500. Bringing the DB to head during app
startup (dev only) closes that gap, and because it runs before any request, the
connection pool only ever sees the up-to-date schema.

We build the Alembic Config WITHOUT the ini file so it doesn't re-run
`fileConfig` and clobber the app's structured logging; `env.py` supplies the DB
URL from Settings itself.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

# app/core/migrate.py -> parents[2] == the backend root (holds migrations/).
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _config() -> Config:
    cfg = Config()  # no ini -> env.py skips fileConfig, app logging is preserved
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    return cfg


def upgrade_to_head() -> None:
    """Run `alembic upgrade head`. Sync (Alembic spins its own event loop), so
    callers in an async context must offload it to a thread."""
    command.upgrade(_config(), "head")
