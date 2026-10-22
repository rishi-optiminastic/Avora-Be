"""Circle-owned records: personal details, appraisals, assets; masked UAN only

Circle is the system of record for pay, bank, personal and employment details,
appraisal history and assets. The Circle scheduler copies them here and Avora
shows them read-only once a person is in Circle (`employees.circle_linked_at`).

ID numbers are kept masked only (last 4 characters). The full UAN Avora used to
keep in `employees.uan_number` is masked into `employee_circle_profiles` and
the column is dropped, so no full ID number stays in Avora. The downgrade puts
the column back EMPTY: the full numbers live in Circle, never here again.

Revision ID: c7e9a1b3d5f7
Revises: a3c5e7b9d1f2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e9a1b3d5f7"
down_revision: str | None = "a3c5e7b9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CIPHERTEXT = 2048
_MASKED = 16
_SHORT = 64
_LONG = 256


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _owner() -> list[object]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
    ]


def _create_profiles() -> None:
    op.create_table(
        "employee_circle_profiles",
        *_owner(),
        sa.Column("phone_encrypted", sa.String(length=_CIPHERTEXT), nullable=True),
        sa.Column("personal_email_encrypted", sa.String(length=_CIPHERTEXT), nullable=True),
        sa.Column("address_encrypted", sa.String(length=_CIPHERTEXT), nullable=True),
        sa.Column("emergency_contact_encrypted", sa.String(length=_CIPHERTEXT), nullable=True),
        sa.Column("employment_type", sa.String(length=_SHORT), nullable=True),
        sa.Column("pan_masked", sa.String(length=_MASKED), nullable=True),
        sa.Column("aadhaar_masked", sa.String(length=_MASKED), nullable=True),
        sa.Column("uan_masked", sa.String(length=_MASKED), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("employee_id", name="uq_employee_circle_profiles_employee"),
    )
    op.create_index(
        "ix_employee_circle_profiles_employee_id", "employee_circle_profiles", ["employee_id"]
    )


def _create_appraisals() -> None:
    op.create_table(
        "employee_appraisals",
        *_owner(),
        sa.Column("circle_id", sa.String(length=_SHORT), nullable=False),
        sa.Column("period", sa.String(length=_SHORT), nullable=True),
        sa.Column("status", sa.String(length=_SHORT), nullable=True),
        sa.Column("current_salary", sa.String(length=_SHORT), nullable=True),
        sa.Column("revised_salary", sa.String(length=_SHORT), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("employee_id", "circle_id", name="uq_employee_appraisals_circle_id"),
    )
    op.create_index("ix_employee_appraisals_employee_id", "employee_appraisals", ["employee_id"])


def _create_assets() -> None:
    op.create_table(
        "employee_assets",
        *_owner(),
        sa.Column("circle_id", sa.String(length=_SHORT), nullable=False),
        sa.Column("name", sa.String(length=_LONG), nullable=True),
        sa.Column("asset_type", sa.String(length=_SHORT), nullable=True),
        sa.Column("serial_number", sa.String(length=_LONG), nullable=True),
        sa.Column("status", sa.String(length=_SHORT), nullable=True),
        sa.Column("assigned_date", sa.Date(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("employee_id", "circle_id", name="uq_employee_assets_circle_id"),
    )
    op.create_index("ix_employee_assets_employee_id", "employee_assets", ["employee_id"])


def upgrade() -> None:
    op.add_column(
        "employees", sa.Column("circle_linked_at", sa.DateTime(timezone=True), nullable=True)
    )
    _create_profiles()
    _create_appraisals()
    _create_assets()
    # Keep what HR entered, masked: everything but the last 4 digits becomes X.
    # A value too short to be a UAN would reveal most of itself, so it goes.
    op.execute(
        """
        INSERT INTO employee_circle_profiles (id, employee_id, uan_masked, created_at, updated_at)
        SELECT gen_random_uuid(), id,
               repeat('X', length(trim(uan_number)) - 4) || right(trim(uan_number), 4),
               now(), now()
        FROM employees
        WHERE uan_number IS NOT NULL AND length(trim(uan_number)) >= 8
        """
    )
    op.drop_column("employees", "uan_number")


def downgrade() -> None:
    # Restored empty on purpose: Avora no longer holds full ID numbers.
    op.add_column("employees", sa.Column("uan_number", sa.String(length=12), nullable=True))
    op.drop_index("ix_employee_assets_employee_id", table_name="employee_assets")
    op.drop_table("employee_assets")
    op.drop_index("ix_employee_appraisals_employee_id", table_name="employee_appraisals")
    op.drop_table("employee_appraisals")
    op.drop_index(
        "ix_employee_circle_profiles_employee_id", table_name="employee_circle_profiles"
    )
    op.drop_table("employee_circle_profiles")
    op.drop_column("employees", "circle_linked_at")
