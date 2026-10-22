"""Mirror the personal and employment details Circle owns.

Circle wins for: employee code, work location, reporting manager, employment
type, phone, personal email, address, emergency contact and the masked PAN,
Aadhaar and UAN. Rules:

- A value Circle has replaces Avora's.
- A value Circle lacks never wipes Avora's: it stays, and the field NAME is
  logged (never the value).
- A manager Circle names that Avora cannot match (not here yet, left, shared
  email) is logged and the current reporting line kept - the same care the
  HR webhook takes, because the reporting edge is what grants a lead access
  to their team.
- ID numbers are stored masked only; they are masked again here, so a full
  number is never stored even if Circle ever sent one.

The first time Circle knows the person, they are marked Circle-linked: from
then on these fields, pay and bank are read-only in Avora. The caller
(`CircleProfileSync`) audits the changed field names and commits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.core.pii_crypto import decrypt_pii, encrypt_pii
from app.models.circle_record import SHORT_TEXT_LENGTH, EmployeeCircleProfile
from app.models.employee import Employee
from app.repositories.circle_record import CircleRecordRepository
from app.repositories.employee import EmployeeRepository
from app.services.circle_values import clipped, mask_id_number, text_or_none

logger = get_logger("app.circle_details")

_EMPLOYEE_NUMBER_LENGTH = 32
_LOCATION_LENGTH = 128
# Plaintext cap so the Fernet ciphertext (~4/3 of it plus overhead) always fits
# the 2048-character column.
_CONTACT_TEXT_LENGTH = 1024
# Contact details: Circle field -> encrypted column.
_CONTACT_FIELDS = (
    ("phone", "phone_encrypted"),
    ("personal_email", "personal_email_encrypted"),
    ("address", "address_encrypted"),
    ("emergency_contact", "emergency_contact_encrypted"),
)
_MASKED_IDS = (("pan_masked", False), ("aadhaar_masked", True), ("uan_masked", False))


class CircleDetailsSync:
    def __init__(
        self,
        employees: EmployeeRepository,
        records: CircleRecordRepository,
        settings: Settings,
    ) -> None:
        self._employees = employees
        self._records = records
        self._settings = settings

    async def apply(self, employee: Employee, profile: dict[str, Any]) -> list[str]:
        """Apply Circle's details; returns the names of the fields changed."""
        kept: list[str] = []
        changed = await self._apply_employee_fields(employee, profile, kept)
        changed += await self._apply_profile(employee, profile, kept)
        if kept:
            logger.info(
                "circle_details_kept", extra={"employee": str(employee.id), "fields": sorted(kept)}
            )
        return sorted(changed)

    async def _apply_employee_fields(
        self, employee: Employee, profile: dict[str, Any], kept: list[str]
    ) -> list[str]:
        incoming = {
            "employee_number": clipped(profile.get("employee_code"), _EMPLOYEE_NUMBER_LENGTH),
            "location": clipped(profile.get("work_location"), _LOCATION_LENGTH),
        }
        fields: dict[str, object] = {}
        for name, value in incoming.items():
            current = getattr(employee, name)
            if value is None:
                _note_kept(kept, name, current)
            elif value != current:
                fields[name] = value
        manager_id = await self._manager_id(employee, profile)
        if manager_id is not None and manager_id != employee.manager_id:
            fields["manager_id"] = manager_id
        names = sorted(fields)
        if employee.circle_linked_at is None:
            fields["circle_linked_at"] = datetime.now(UTC)
            names.append("circle_link")
        if fields:
            await self._employees.apply_circle_fields(employee, fields)
        return names

    async def _manager_id(self, employee: Employee, profile: dict[str, Any]) -> uuid.UUID | None:
        """The Avora id of the manager Circle names, or None to keep the
        current line (Circle names nobody, or someone Avora cannot match)."""
        email = (text_or_none(profile.get("manager_email")) or "").lower()
        named = email or text_or_none(profile.get("manager_code"))
        if not named:
            return None
        try:
            manager = await self._employees.find_by_work_email_insensitive(email) if email else None
        except ConflictError:
            manager = None  # two people share it: don't guess
        if manager is None or not manager.is_active or manager.id == employee.id:
            logger.warning("circle_manager_unmatched", extra={"employee": str(employee.id)})
            return None
        return manager.id

    async def _apply_profile(
        self, employee: Employee, profile: dict[str, Any], kept: list[str]
    ) -> list[str]:
        record = await self._records.get_profile(employee.id)
        fields: dict[str, str | None] = {}
        for source, column in _CONTACT_FIELDS:
            value = clipped(profile.get(source), _CONTACT_TEXT_LENGTH)
            current = self._decrypted(record, column)
            if value is None:
                _note_kept(kept, source, current)
            elif value != current:
                fields[column] = encrypt_pii(self._settings, value)
        plain = {"employment_type": clipped(profile.get("employment_type"), SHORT_TEXT_LENGTH)}
        for name, grouped in _MASKED_IDS:
            plain[name] = mask_id_number(profile.get(name), grouped=grouped)
        for name, value in plain.items():
            current = getattr(record, name) if record else None
            if value is None:
                _note_kept(kept, name, current)
            elif value != current:
                fields[name] = value
        await self._records.save_profile(employee.id, fields, synced_at=datetime.now(UTC))
        return sorted(_public_name(column) for column in fields)

    def _decrypted(self, record: EmployeeCircleProfile | None, column: str) -> str | None:
        ciphertext = getattr(record, column) if record else None
        return decrypt_pii(self._settings, ciphertext) if ciphertext else None


def _note_kept(kept: list[str], name: str, current: object) -> None:
    """Circle had nothing for `name`; record it when Avora keeps a value."""
    if current:
        kept.append(name)


def _public_name(column: str) -> str:
    return column.removesuffix("_encrypted")
