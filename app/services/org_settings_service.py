"""Org-settings business rules — the workspace identity / white-label config.

Everyone may READ it (the dashboard chrome renders the workspace name, product
wordmark and logo for all users); only Admin may change the text or upload a
logo. The first read materialises a default row so the chrome always has values.
"""

from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError, StorageError, ValidationError
from app.models.employee import Role
from app.models.org_settings import OrgSettings
from app.repositories.audit import AuditRepository
from app.repositories.org_settings import OrgSettingsRepository
from app.schemas.auth import CurrentUser
from app.schemas.org_settings import OrgSettingsUpdate

logger = logging.getLogger(__name__)

ALLOWED_LOGO_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/svg+xml"})
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB — matches the UI hint.


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role == Role.ADMIN


class OrgSettingsService:
    def __init__(
        self, orgs: OrgSettingsRepository, audit: AuditRepository, settings: Settings
    ) -> None:
        self._orgs = orgs
        self._audit = audit
        self._settings = settings

    async def get_or_create(self) -> OrgSettings:
        return await self._orgs.get() or await self._orgs.create_default()

    async def update(self, caller: CurrentUser, payload: OrgSettingsUpdate) -> OrgSettings:
        if not _can_manage(caller):
            raise AuthorizationError()
        org = await self.get_or_create()
        fields = payload.model_dump(exclude_unset=True)
        for key, value in fields.items():
            if isinstance(value, str):
                value = value.strip()
            setattr(org, key, value)
        org.updated_by = caller.employee_id
        await self._orgs.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="org_settings.update",
            target=f"org:{org.id}",
        )
        return org

    async def set_logo(self, caller: CurrentUser, data: bytes, content_type: str) -> OrgSettings:
        """Admin uploads the workspace logo (image, ≤2 MB). S3-backed when
        configured, otherwise the bytes are kept in-row (mirrors the avatar)."""
        if not _can_manage(caller):
            raise AuthorizationError()
        media = (content_type or "").split(";")[0].strip().lower()
        if media not in ALLOWED_LOGO_TYPES:
            raise ValidationError("Logo must be a PNG, JPG, WebP, or SVG image.")
        if not data:
            raise ValidationError("Logo is empty.")
        if len(data) > MAX_LOGO_BYTES:
            raise ValidationError("Logo is too large (max 2 MB).")

        org = await self.get_or_create()
        object_key: str | None = None
        stored: bytes | None = data
        if self._settings.s3_enabled:
            object_key = storage.logo_object_key(media)
            try:
                await storage.put_object(object_key, data, media)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("logo_s3_put_failed", extra={"key": object_key})
                raise StorageError() from exc
            stored = None

        org.logo_object_key = object_key
        org.logo_content = stored
        org.logo_content_type = media
        org.updated_by = caller.employee_id
        await self._orgs.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="org_settings.logo",
            target=f"org:{org.id}",
        )
        return org

    async def get_logo(self) -> OrgSettings:
        """The workspace logo for any signed-in user (404 when none is set)."""
        org = await self._orgs.get()
        if org is None or not org.has_logo:
            raise NotFoundError()
        return org
