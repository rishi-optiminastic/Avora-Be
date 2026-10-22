"""Turning an `AudienceSpec` into the people it actually means.

Kept out of both FormAssignmentService and EventService so there is exactly one
answer to "who does this reach", and so the rules below are stated once:

* Only **active** employees are ever included. An audience saved before someone
  left must not keep reaching them, and "everyone" has to mean the current
  roster rather than the roster at the time it was written.
* A departments audience matches on the trimmed, case-insensitive department
  name, because `employees.department` is free text typed by HR and "Design"
  and "design " are the same team.
* A people audience is validated, not trusted: every id must be a current
  employee, and an unknown one is a 422 rather than a silent omission.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import ValidationError
from app.models.employee import Employee
from app.repositories.employee import EmployeeRepository
from app.schemas.audience import MAX_AUDIENCE, AudienceKind, AudienceRead, AudienceSpec


class AudienceResolver:
    def __init__(self, employees: EmployeeRepository) -> None:
        self._employees = employees

    async def resolve(self, spec: AudienceSpec) -> list[Employee]:
        """The active employees this audience means, ordered by name."""
        if spec.kind is AudienceKind.EVERYONE:
            people = list(await self._employees.list_all_active())
        elif spec.kind is AudienceKind.DEPARTMENTS:
            people = await self._in_departments(spec.stored_departments())
        else:
            people = await self._exactly(spec.employee_ids)

        if len(people) > MAX_AUDIENCE:
            raise ValidationError(
                f"That reaches {len(people)} people, more than the {MAX_AUDIENCE} limit."
            )
        return sorted(people, key=lambda p: p.full_name.lower())

    async def _in_departments(self, departments: Sequence[str]) -> list[Employee]:
        wanted = {d.casefold() for d in departments}
        everyone = await self._employees.list_all_active()
        found = [p for p in everyone if (p.department or "").strip().casefold() in wanted]
        if not found:
            # Loud rather than an empty send: a department renamed in HR is the
            # usual cause, and a silent zero-recipient send hides it until
            # someone asks why nobody replied.
            raise ValidationError("Nobody active is in the department(s) you picked.")
        return found

    async def _exactly(self, ids: Sequence[uuid.UUID]) -> list[Employee]:
        unique = list(dict.fromkeys(ids))
        found = await self._employees.get_many(unique)
        people = [found[i] for i in unique if i in found and found[i].is_active]
        if len(people) != len(unique):
            raise ValidationError("Every recipient must be a current employee.")
        return people

    async def describe(self, spec: AudienceSpec) -> AudienceRead:
        """The audience plus how many people it reaches right now. Never raises
        for an empty department - a saved audience that has gone empty should
        still render, as zero."""
        try:
            size = len(await self.resolve(spec))
        except ValidationError:
            size = 0
        return AudienceRead(
            kind=spec.kind,
            departments=spec.stored_departments(),
            employee_ids=spec.employee_ids if spec.kind is AudienceKind.PEOPLE else [],
            size=size,
        )

    @staticmethod
    def from_stored(
        kind: AudienceKind, departments: Sequence[str], employee_ids: Sequence[str]
    ) -> AudienceSpec:
        """Rebuild a spec from the JSON columns a row stores it in."""
        return AudienceSpec(
            kind=kind,
            departments=list(departments),
            employee_ids=[uuid.UUID(i) for i in employee_ids],
        )
