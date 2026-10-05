"""What Avora shows from Circle (Golden rule #5: explicit response shapes)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.models.document import DocumentCategory


class CompensationPrefill(BaseModel):
    """Circle's figures, shaped like Avora's compensation + bank forms.

    Nothing is saved: the UI fills the forms with these and HR saves them
    through the normal endpoints. A value Circle has but Avora would reject
    (e.g. a malformed IFSC) comes back as None with a warning instead.
    """

    found: bool
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


class CircleDocumentRead(BaseModel):
    id: str
    title: str
    category: DocumentCategory
    circle_category: str | None
    content_type: str | None
    byte_size: int | None
    uploaded_at: str | None


class CircleDocumentList(BaseModel):
    """`configured` is False when the Circle connection is off, so the UI can
    hide the section rather than show an error."""

    configured: bool
    documents: list[CircleDocumentRead]
