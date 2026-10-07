"""Fill in personal details Avora is missing from Circle: date of birth,
gender and profile photo.

Fill-only: a value Avora already has - entered by HR or by the person - is
never replaced. Run per employee by `worker/circle_documents_scheduler.py`;
the caller commits. Personal data travels only over the private Circle link
(never id-sync) and is audited.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import NotFoundError, StorageError
from app.core.logging import get_logger
from app.core.uploads import detect_media_type
from app.models.employee import Employee, Gender
from app.repositories.audit import AuditRepository
from app.repositories.circle_document_import import CircleDocumentImportRepository
from app.repositories.employee import EmployeeRepository
from app.services.circle_client import CircleClient
from app.services.circle_document_sync import SYNC_ACTOR, discard_stored
from app.services.employee_service import ALLOWED_AVATAR_TYPES, MAX_AVATAR_BYTES

logger = get_logger("app.circle_profile")

# Ledger key prefix: a photo is attempted once per Circle document, so a bad
# or oversized one is not downloaded again every run.
_AVATAR_LEDGER_PREFIX = "avatar:"

_GENDERS: dict[str, Gender] = {
    "male": Gender.MALE,
    "m": Gender.MALE,
    "man": Gender.MALE,
    "female": Gender.FEMALE,
    "f": Gender.FEMALE,
    "woman": Gender.FEMALE,
    "other": Gender.OTHER,
    "non-binary": Gender.OTHER,
    "nonbinary": Gender.OTHER,
}


def map_gender(raw: Any) -> Gender | None:
    """Circle's free text onto Avora's choices; anything unclear stays unset."""
    return _GENDERS.get(str(raw or "").strip().lower())


def parse_birth_date(raw: Any) -> date | None:
    try:
        born = date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None
    # A future or implausible date is a typo, not a birthday.
    if born is None or not date(1900, 1, 1) <= born <= date.today():
        return None
    return born


class CircleProfileSync:
    def __init__(
        self,
        employees: EmployeeRepository,
        imports: CircleDocumentImportRepository,
        audit: AuditRepository,
        settings: Settings,
        client: CircleClient,
    ) -> None:
        self._employees = employees
        self._imports = imports
        self._audit = audit
        self._settings = settings
        self._client = client

    async def fill(self, employee: Employee) -> list[str]:
        """Fill what is missing; returns the names of the fields filled."""
        if employee.date_of_birth and employee.gender and employee.has_avatar:
            return []  # nothing to ask Circle for
        try:
            profile = await self._client.profile(employee.work_email)
        except NotFoundError:
            return []
        filled = await self._fill_details(employee, profile)
        if not employee.has_avatar and await self._fill_avatar(employee, profile):
            filled.append("avatar")
        if filled:
            await self._audit.append(
                actor=SYNC_ACTOR,
                action="profile.circle_fill",
                target=f"employee:{employee.id}:{','.join(filled)}",
            )
        return filled

    async def _fill_details(self, employee: Employee, profile: dict[str, Any]) -> list[str]:
        fields: dict[str, object] = {}
        born = parse_birth_date(profile.get("date_of_birth"))
        if employee.date_of_birth is None and born is not None:
            fields["date_of_birth"] = born
        gender = map_gender(profile.get("gender"))
        if employee.gender is None and gender is not None:
            fields["gender"] = gender
        if fields:
            await self._employees.admin_update_profile(employee, fields)
        return sorted(fields)

    async def _fill_avatar(self, employee: Employee, profile: dict[str, Any]) -> bool:
        doc_id = str(profile.get("avatar_document_id") or "")
        if not doc_id:
            return False
        ledger_key = f"{_AVATAR_LEDGER_PREFIX}{doc_id}"[:64]
        if ledger_key in await self._imports.copied_ids(employee.id):
            return False
        try:
            file = await self._client.avatar_content(
                employee.work_email, max_bytes=MAX_AVATAR_BYTES
            )
        except NotFoundError:
            file = None
        media = detect_media_type(file.content) if file and file.content else None
        if file is None or file.too_large or media is None or media not in ALLOWED_AVATAR_TYPES:
            await self._imports.record(employee.id, ledger_key, None)  # tried; don't retry
            return False
        object_key = await self._store(employee, file.content, media)
        try:
            await self._employees.set_avatar(
                employee,
                object_key=object_key,
                content=None if object_key else file.content,
                content_type=media,
            )
            await self._imports.record(employee.id, ledger_key, None)
        except Exception:
            await discard_stored(object_key)
            raise
        return True

    async def _store(self, employee: Employee, data: bytes, media: str) -> str | None:
        """S3 when configured (key only in the DB), else the in-DB column - the
        same layout the profile-photo upload uses."""
        if not self._settings.s3_enabled:
            return None
        object_key = storage.avatar_object_key(str(employee.id), media)
        try:
            await storage.put_object(object_key, data, media)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("avatar_s3_put_failed", extra={"key": object_key})
            raise StorageError() from exc
        return object_key
