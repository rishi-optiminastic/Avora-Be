"""Org settings — the single workspace identity / white-label row (a singleton).

One row, upserted by the service. Holds the workspace name + location shown in
the dashboard chrome, the product wordmark (so the app can be white-labelled),
and an optional uploaded logo (S3 key, or in-DB bytes as a fallback — mirrors the
employee avatar). Everyone may read it (so the chrome renders for all); only
Admin may change it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OrgSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "org_settings"

    # Workspace identity shown in the sidebar card, breadcrumb and greeting.
    name: Mapped[str] = mapped_column(String(120), default="Optiminastic")
    # Short qualifier, e.g. "HQ" / "Mumbai" (rendered as "name · location").
    location: Mapped[str] = mapped_column(String(120), default="HQ")
    # The product wordmark in the sidebar — editable for true white-labelling.
    product_name: Mapped[str] = mapped_column(String(60), default="Avora")

    # Optional uploaded logo. S3-backed when configured (`logo_object_key`),
    # otherwise the raw bytes live in `logo_content` (the in-DB fallback).
    logo_object_key: Mapped[str | None] = mapped_column(String(512), default=None)
    logo_content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    logo_content_type: Mapped[str | None] = mapped_column(String(64), default=None)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )

    @property
    def has_logo(self) -> bool:
        return bool(self.logo_object_key or self.logo_content)
