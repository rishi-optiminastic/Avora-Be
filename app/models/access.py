"""Access - what the company owns in tech, and who holds each piece of it.

This replaces the "Tech Tracker" spreadsheet. The sheet answered "what do we
pay for" well and "who can get into Vercel" not at all: holders lived in
free-text "Shared With" columns, so offboarding meant reading every row.

Three tables carry it:

* `TechAsset` - one thing owned: a Google account, a tool login, a
  subscription, a domain, a server, an API key, a phone number. An asset can
  sit under a parent account (Vercel under devops@), which is how "anyone who
  holds devops@ can reach Vercel" is answered without a row per pair.
* `AccessGrant` - one person holding one asset at a level. Never deleted:
  revoking stamps `revoked_at`, so history survives offboarding.
* `AccessRequest` - an ask for an asset. One approval from the requester's
  manager, HR or an admin creates the grant immediately.

The asset's password or key is stored encrypted (`secret_ciphertext`, Fernet,
see `core/access_crypto.py`). It is never part of a read schema; the only way
out is the audited reveal, which re-checks the grant server-side.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TechAssetKind(StrEnum):
    ACCOUNT = "account"  # an identity other things hang off: devops@, a Gmail
    TOOL = "tool"  # a login to a product: GitHub, Canva, Razorpay
    SUBSCRIPTION = "subscription"  # something billed on a cycle
    DOMAIN = "domain"
    HOSTING = "hosting"  # a server, database, deploy platform
    API_KEY = "api_key"
    PHONE = "phone"  # a number used for 2FA or recovery


class AccessLevel(StrEnum):
    """How much a holder can do, strongest first (the sheet's Access Level list)."""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class AssetPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    TRIAL = "trial"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AccessRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"  # withdrawn by the requester before a decision


class TechAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tech_assets"
    __table_args__ = (Index("ix_tech_assets_kind_name", "kind", "name"),)

    kind: Mapped[TechAssetKind] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Free text on purpose (AI, Hosting, Design ...): the sheet's list grows.
    category: Mapped[str | None] = mapped_column(String(64), default=None)
    # The account this sits under. SET NULL so archiving or removing an account
    # never takes the tools beneath it with it.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tech_assets.id", ondelete="SET NULL"), default=None, index=True
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    login_url: Mapped[str | None] = mapped_column(String(500), default=None)
    # The username or email used to sign in. Not secret: holders need to see it.
    login_id: Mapped[str | None] = mapped_column(String(320), default=None)
    priority: Mapped[AssetPriority] = mapped_column(default=AssetPriority.MEDIUM)
    status: Mapped[AssetStatus] = mapped_column(default=AssetStatus.ACTIVE)
    two_factor_method: Mapped[str | None] = mapped_column(String(64), default=None)

    # Money and renewal live in columns, not `details`, because they are
    # filtered (renewals due) and gated (who may see cost). Integer minor units.
    cost_minor: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    billing_cycle: Mapped[str | None] = mapped_column(String(32), default=None)
    renews_on: Mapped[date | None] = mapped_column(Date, default=None, index=True)

    # Kind-specific fields from the sheet (registrar, nameservers, plan, card
    # last 4, recovery email ...). Shape is per kind; nothing secret goes here.
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(String(1000), default=None)

    secret_ciphertext: Mapped[str | None] = mapped_column(Text, default=None)
    secret_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    secret_rotated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def has_secret(self) -> bool:
        return self.secret_ciphertext is not None


class AccessGrant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "access_grants"
    __table_args__ = (
        # One LIVE grant per person per asset; revoked rows stay as history.
        Index(
            "uq_access_grants_live",
            "asset_id",
            "employee_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tech_assets.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[AccessLevel] = mapped_column(default=AccessLevel.VIEWER)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_requests.id", ondelete="SET NULL"), default=None
    )
    note: Mapped[str | None] = mapped_column(String(200), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )


class AccessRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "access_requests"
    __table_args__ = (Index("ix_access_requests_employee_status", "employee_id", "status"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tech_assets.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[AccessLevel] = mapped_column(default=AccessLevel.VIEWER)
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[AccessRequestStatus] = mapped_column(
        default=AccessRequestStatus.PENDING, index=True
    )
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    review_note: Mapped[str | None] = mapped_column(String(500), default=None)
