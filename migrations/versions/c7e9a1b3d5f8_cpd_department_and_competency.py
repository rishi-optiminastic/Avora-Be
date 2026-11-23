"""CPD papers get a department, and questions a competency.

Revision ID: c7e9a1b3d5f8
Revises: a2c4e6f8b0d3
Create Date: 2026-11-23

Two labels, both nullable, so every existing assessment stays valid:

* `assessments.department` - the department a paper is written FOR. It groups
  HR's list and preselects the audience when assigning. Deliberately NOT a
  scoping rule: a department renamed in the directory would otherwise turn a
  live paper into one nobody can be given.
* `assessment_questions.competency` - which of the five CPD competencies the
  question tests ("Problem Solving"). A label rather than an enum so a sixth
  competency does not need a migration.

Additive only, so it is backward compatible: old code ignores both columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e9a1b3d5f8"
down_revision = "a2c4e6f8b0d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assessments", sa.Column("department", sa.String(length=120), nullable=True))
    op.add_column(
        "assessment_questions", sa.Column("competency", sa.String(length=80), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("assessment_questions", "competency")
    op.drop_column("assessments", "department")
