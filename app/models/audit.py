"""AuditLog — append-only, hash-chained record of sensitive actions.

Write-only from the app's perspective: there is no endpoint that updates or
deletes audit rows (Security rule 5.7). Each row chains to the previous via a
hash so tampering is detectable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, _utcnow


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=_utcnow,
        index=True,
    )

    # Who acted (employee id or "system"/"hr-webhook"/"agent:<device_id>").
    actor: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    # What was acted on (e.g. "employee:<id>") — never the sensitive value itself.
    # Text, not a bounded String: the payroll export records exactly whose bank
    # details left the system (§5.7), which is a list of employee ids — 17 people
    # already overflows 256 characters, and an audit write that raises takes the
    # user's whole operation down with it.
    target: Mapped[str | None] = mapped_column(Text, default=None)

    # Hash chain: sha256(prev_hash || canonical(this row)).
    prev_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    entry_hash: Mapped[str] = mapped_column(String(64), index=True)
