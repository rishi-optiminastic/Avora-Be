"""Browsing hidden domain — a domain one employee has chosen to hide.

A deliberately single-owner privacy escape hatch: the configured owner curates a
personal list of domains. The *viewed* employee's hidden set is applied for every
viewer, so a hidden domain (and the time spent on it) never surfaces in anyone's
Browsing tab. Stored normalised (lowercase host, no scheme/path) so matching is a
plain suffix check.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BrowsingHiddenDomain(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "browsing_hidden_domains"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "domain", name="uq_browsing_hidden_domains_employee_domain"
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(256))
