"""Who a thing goes to - shared by form sends and events.

One definition on purpose. Forms and events both needed "everyone / these
departments / these people", and two copies of that idea drift: one of them
grows a rule about inactive employees and the other does not.

The spec is a *claim* about who to include, never the list itself. The server
resolves it against the live roster (`AudienceResolver`), so "everyone" means
everyone at the moment of sending, and a client cannot smuggle in somebody it
should not be able to reach.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

# A guard rail, not a business rule: far above any real org, low enough that a
# mistake cannot queue a million rows.
MAX_AUDIENCE = 2000
MAX_DEPARTMENTS = 50


class AudienceKind(StrEnum):
    EVERYONE = "everyone"  # every active employee
    DEPARTMENTS = "departments"  # every active employee in these departments
    PEOPLE = "people"  # exactly these employees


class AudienceSpec(BaseModel):
    """Who to send to. Resolved server-side; see `AudienceResolver`."""

    kind: AudienceKind = AudienceKind.PEOPLE
    departments: list[str] = Field(default_factory=list, max_length=MAX_DEPARTMENTS)
    employee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_AUDIENCE)

    @model_validator(mode="after")
    def _needs_its_own_field(self) -> AudienceSpec:
        """A departments audience with no departments, or a people audience with
        nobody, is almost always a half-filled form rather than an intent to
        send to no one - and sending to no one silently is the worst outcome."""
        if self.kind is AudienceKind.DEPARTMENTS and not self.departments:
            raise ValueError("Pick at least one department.")
        if self.kind is AudienceKind.PEOPLE and not self.employee_ids:
            raise ValueError("Pick at least one person.")
        return self

    def stored_departments(self) -> list[str]:
        """Departments to persist: only meaningful for a departments audience,
        de-duplicated and trimmed so "Design " and "Design" are one."""
        if self.kind is not AudienceKind.DEPARTMENTS:
            return []
        return list(dict.fromkeys(d.strip() for d in self.departments if d.strip()))

    def stored_employee_ids(self) -> list[str]:
        if self.kind is not AudienceKind.PEOPLE:
            return []
        return [str(i) for i in dict.fromkeys(self.employee_ids)]


class AudienceRead(BaseModel):
    """How an audience is shown back, with the count it resolves to right now.

    `size` is computed at read time rather than stored: an "everyone" audience
    saved last month means something different today, and a cached number would
    quietly be wrong.
    """

    kind: AudienceKind
    departments: list[str] = Field(default_factory=list)
    employee_ids: list[uuid.UUID] = Field(default_factory=list)
    size: int = 0
