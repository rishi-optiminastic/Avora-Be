"""Category rule — admin/manager-configured productivity classification.

Overrides the built-in domain map: maps a domain or app pattern to a category,
either org-wide (department is NULL) or for a single department. Admins manage
any; senior managers/managers manage their own department's rules.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.categories import MatchType, ProductivityCategory
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CategoryRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "category_rules"
    __table_args__ = (Index("ix_category_rules_department", "department"),)

    match_type: Mapped[MatchType] = mapped_column(default=MatchType.DOMAIN)
    pattern: Mapped[str] = mapped_column(String(256))
    category: Mapped[ProductivityCategory] = mapped_column()
    # NULL = org-wide; otherwise scoped to this department.
    department: Mapped[str | None] = mapped_column(String(128), default=None)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
