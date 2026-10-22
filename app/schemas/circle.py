"""Circle response shapes (Golden rule #5: explicit shapes): the compensation
pre-fill, and the records Avora mirrors from Circle."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class CompensationPrefill(BaseModel):
    """Circle's figures, shaped like Avora's compensation + bank forms.

    Nothing is saved: the UI fills the forms with these and HR saves them
    through the normal endpoints. A value Circle has but Avora would reject
    (e.g. a malformed IFSC) comes back as None with a warning instead.
    """

    found: bool
    # False when Avora's Circle link is not set up at all (vs. Circle simply
    # having nothing for this person), so the UI can say which.
    configured: bool = True
    employee_code: str | None = None
    annual_ctc_text: str | None = None
    amount_minor: int | None = None
    currency: str = "INR"
    period: str = "annual"
    pf_enabled: bool | None = None
    effective_date: date | None = None
    bank_name: str | None = None
    account_number: str | None = None
    ifsc_code: str | None = None
    warnings: list[str] = []


class CircleProfileRead(BaseModel):
    """Personal and employment details Circle owns, read-only in Avora.

    ID numbers are masked (last 4 characters) - Avora never holds more.
    `linked` False means Circle does not know the person (yet), so only what
    Avora had before (e.g. a masked UAN HR entered) can show.
    """

    linked: bool
    circle_url: str | None = None
    synced_at: datetime | None = None
    employee_code: str | None = None
    work_location: str | None = None
    employment_type: str | None = None
    phone: str | None = None
    personal_email: str | None = None
    address: str | None = None
    emergency_contact: str | None = None
    pan_masked: str | None = None
    aadhaar_masked: str | None = None
    uan_masked: str | None = None


class AppraisalRead(ORMModel):
    """One appraisal from Circle; salaries are Circle's text, as written."""

    circle_id: str
    period: str | None
    status: str | None
    current_salary: str | None
    revised_salary: str | None
    effective_date: date | None
    score: float | None


class AssetRead(ORMModel):
    circle_id: str
    name: str | None
    asset_type: str | None
    serial_number: str | None
    status: str | None
    assigned_date: date | None
