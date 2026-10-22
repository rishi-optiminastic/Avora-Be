"""What Avora mirrors from Circle about one person, beyond pay and bank.

Circle is the system of record for all of it: HR edits it in Circle and the
Circle scheduler copies it here (`worker/circle_documents_scheduler.py`). Avora
shows it read-only and never edits it.

Kept in their own tables, not on `employees`, so none of it can leak through
`EmployeeRead` - every read goes through `CircleRecordService`, which allows
only the person, HR/Admin and payroll (Security rules 5.3, 5.6). Contact
details are Fernet-encrypted at rest; ID numbers arrive and are kept ONLY in
masked form (last 4 characters), so the full PAN, Aadhaar and UAN never live
in Avora at all.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Column sizes, shared with the sync that clips Circle's free text to fit.
CONTACT_CIPHERTEXT_LENGTH = 2048
MASKED_ID_LENGTH = 16
SHORT_TEXT_LENGTH = 64
LONG_TEXT_LENGTH = 256


class EmployeeCircleProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Personal and employment details, one row per person."""

    __tablename__ = "employee_circle_profiles"
    __table_args__ = (UniqueConstraint("employee_id", name="uq_employee_circle_profiles_employee"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    # Fernet ciphertext (app.core.pii_crypto); decrypted only for an
    # authorized viewer.
    phone_encrypted: Mapped[str | None] = mapped_column(
        String(CONTACT_CIPHERTEXT_LENGTH), default=None
    )
    personal_email_encrypted: Mapped[str | None] = mapped_column(
        String(CONTACT_CIPHERTEXT_LENGTH), default=None
    )
    address_encrypted: Mapped[str | None] = mapped_column(
        String(CONTACT_CIPHERTEXT_LENGTH), default=None
    )
    emergency_contact_encrypted: Mapped[str | None] = mapped_column(
        String(CONTACT_CIPHERTEXT_LENGTH), default=None
    )
    employment_type: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH), default=None)
    # Masked only, e.g. "XXXXXX234F" / "XXXX XXXX 1234" / "XXXXXXXX1234".
    pan_masked: Mapped[str | None] = mapped_column(String(MASKED_ID_LENGTH), default=None)
    aadhaar_masked: Mapped[str | None] = mapped_column(String(MASKED_ID_LENGTH), default=None)
    uan_masked: Mapped[str | None] = mapped_column(String(MASKED_ID_LENGTH), default=None)
    # Last time the Circle sync looked at this person (changed or not).
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class EmployeeAppraisal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One appraisal from the person's Circle history. Salaries are Circle's
    free text ("12 LPA"), shown as written - never used for payroll."""

    __tablename__ = "employee_appraisals"
    __table_args__ = (
        UniqueConstraint("employee_id", "circle_id", name="uq_employee_appraisals_circle_id"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    circle_id: Mapped[str] = mapped_column(String(SHORT_TEXT_LENGTH))
    period: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH), default=None)
    status: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH), default=None)
    current_salary: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH), default=None)
    revised_salary: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH), default=None)
    effective_date: Mapped[date | None] = mapped_column(Date, default=None)
    score: Mapped[float | None] = mapped_column(Float, default=None)


class EmployeeAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Company equipment Circle's asset register has assigned to the person."""

    __tablename__ = "employee_assets"
    __table_args__ = (
        UniqueConstraint("employee_id", "circle_id", name="uq_employee_assets_circle_id"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    circle_id: Mapped[str] = mapped_column(String(SHORT_TEXT_LENGTH))
    name: Mapped[str | None] = mapped_column(String(LONG_TEXT_LENGTH), default=None)
    asset_type: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH), default=None)
    serial_number: Mapped[str | None] = mapped_column(String(LONG_TEXT_LENGTH), default=None)
    status: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH), default=None)
    assigned_date: Mapped[date | None] = mapped_column(Date, default=None)
