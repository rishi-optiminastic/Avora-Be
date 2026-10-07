"""Circle compensation pre-fill response (Golden rule #5: explicit shapes)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


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
