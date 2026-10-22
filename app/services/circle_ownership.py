"""Which fields Circle owns once a person is Circle-linked, and the refusal
Avora gives when someone tries to edit one here.

The Circle sync marks a person linked (`Employee.circle_linked_at`) the first
time Circle knows them. From then on HR edits these only in Circle; Avora
updates itself from Circle and refuses a manual change with a 409 that says
where to go instead. Re-sending the current value is not a change, so a form
that posts back every field still saves the fields Avora owns.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.models.employee import Employee

EDIT_IN_CIRCLE = "Edit this in Circle."


def circle_edit_url(settings: Settings, employee: Employee) -> str:
    """The person's page in Circle (keyed by Circle's employee code), or
    Circle's home when the code is not known yet."""
    base = settings.circle_app_url.rstrip("/")
    code = employee.employee_number
    return f"{base}/employees/{quote(code, safe='')}" if code else base


def refuse_circle_owned_changes(
    employee: Employee,
    current: Mapping[str, object] | None,
    incoming: Mapping[str, object],
    *,
    label: str,
) -> None:
    """Raise 409 when a Circle-linked person's Circle-owned value would change.
    `current` None means Avora holds nothing yet, so any value is a change."""
    if not employee.circle_linked:
        return
    if current is None or any(current.get(key) != value for key, value in incoming.items()):
        raise ConflictError(f"{label} comes from Circle. {EDIT_IN_CIRCLE}")
