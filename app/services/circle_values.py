"""Reading Circle's values safely - shared by the compensation pre-fill and the
Circle sync, so both accept and reject exactly the same things.

Circle reports what it has, as it has it; everything here turns one raw value
into something Avora would store, or None when it is unusable.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.schemas.compensation import BankDetailsWrite

_PAISE_PER_RUPEE = 100
# Matches CompensationWrite's ceiling (10**15 minor units).
_MAX_ANNUAL_RUPEES = 10**13

BANK_FIELD_LABELS = (
    ("bank_name", "Bank name"),
    ("account_number", "Account number"),
    ("ifsc_code", "IFSC"),
)


def bank_fields(bank: dict[str, Any]) -> tuple[dict[str, str | None], list[str]]:
    """Run Circle's bank values through Avora's own validator, one field at a
    time, so one bad value does not discard the good ones. Returns the values
    and a warning per rejected field (labels only, never the value)."""
    values: dict[str, str | None] = {}
    warnings: list[str] = []
    for field, label in BANK_FIELD_LABELS:
        try:
            values[field] = getattr(
                BankDetailsWrite.model_validate({field: bank.get(field)}), field
            )
        except PydanticValidationError:
            values[field] = None
            warnings.append(f"{label} from Circle is not valid here; enter it manually.")
    return values, warnings


def text_or_none(raw: Any) -> str | None:
    """Circle's free-form values: numbers become text, anything else is dropped."""
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        return None
    text = str(raw).strip()
    return text or None


def clipped(raw: Any, length: int) -> str | None:
    """Free text cut to fit a column of `length` characters."""
    text = text_or_none(raw)
    return text[:length] if text else None


def amount_minor(raw: Any) -> int | None:
    """Annual rupees (int or float, never a bool) to paise; None if unusable."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    if not math.isfinite(raw) or not 0 < raw <= _MAX_ANNUAL_RUPEES:
        return None
    return round(raw * _PAISE_PER_RUPEE)


def parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def score(raw: Any) -> float | None:
    """A finite, non-negative appraisal score (1-5 or a percentage)."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw) if math.isfinite(raw) and raw >= 0 else None


# --- ID numbers -----------------------------------------------------------------

_ID_VISIBLE = 4
_ID_MIN_LENGTH = 8
_AADHAAR_LENGTH = 12
_NOT_ALNUM = re.compile(r"[^0-9A-Za-z]")


def mask_id_number(raw: Any, *, grouped: bool = False) -> str | None:
    """Everything but the last 4 characters as X - the only form of a PAN,
    Aadhaar or UAN Avora ever stores.

    Circle already sends them masked; masking again here is idempotent and
    means a full number can never be stored even if one is sent. `grouped`
    writes a 12-digit Aadhaar as "XXXX XXXX 1234". A value too short to be
    real, or with nothing readable in its last 4, is dropped.
    """
    cleaned = _NOT_ALNUM.sub("", str(raw or "")).upper()
    tail = cleaned[-_ID_VISIBLE:]
    if len(cleaned) < _ID_MIN_LENGTH or set(tail) == {"X"}:
        return None
    if grouped and len(cleaned) == _AADHAAR_LENGTH:
        return f"XXXX XXXX {tail}"
    return "X" * (len(cleaned) - _ID_VISIBLE) + tail
