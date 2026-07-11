"""Personal Access Token business rules.

Mint / list / revoke are performed by an authenticated human (a JWT-backed
`CurrentUser`) for THEMSELVES - a PAT can never mint another PAT, and a caller
only ever sees or revokes their own tokens. `resolve_token` is the one method the
MCP server calls to authenticate an inbound token to an employee id.

No FastAPI objects here (Layering §4). Only a peppered hash is ever stored; the
raw token is returned once at creation and never again (Security rule 5.2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.security import generate_device_token, hash_device_token
from app.repositories.audit import AuditRepository
from app.repositories.personal_access_token import PersonalAccessTokenRepository
from app.schemas.auth import CurrentUser
from app.schemas.pat import PatCreate, PatCreated, PatRead


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PatService:
    def __init__(
        self,
        repo: PersonalAccessTokenRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._settings = settings

    async def issue_token(self, caller: CurrentUser, payload: PatCreate) -> PatCreated:
        raw = generate_device_token()
        token = await self._repo.create_token(
            employee_id=caller.employee_id,
            label=payload.label,
            token_hash=hash_device_token(self._settings, raw),
            expires_at=payload.expires_at,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="pat.token_created",
            target=str(token.id),
        )
        return PatCreated(
            id=token.id,
            label=token.label,
            created_at=token.created_at,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            is_revoked=token.is_revoked,
            token=raw,
        )

    async def list_tokens(self, caller: CurrentUser) -> list[PatRead]:
        tokens = await self._repo.list_tokens(caller.employee_id)
        return [
            PatRead(
                id=t.id,
                label=t.label,
                created_at=t.created_at,
                expires_at=t.expires_at,
                last_used_at=t.last_used_at,
                is_revoked=t.is_revoked,
            )
            for t in tokens
        ]

    async def revoke_token(self, caller: CurrentUser, token_id: uuid.UUID) -> None:
        token = await self._repo.get_token(token_id, caller.employee_id)
        if token is None:
            raise NotFoundError("Token not found.")
        if not token.is_revoked:
            await self._repo.revoke_token(token)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="pat.token_revoked",
            target=str(token.id),
        )

    async def resolve_token(self, raw_token: str) -> uuid.UUID | None:
        """Authenticate a Personal Access Token -> employee id (or None). Bumps
        last_used_at on success. Role/scope are re-derived by the caller from the
        employee record, never carried on the token (Golden rule #2)."""
        token = await self._repo.get_active_by_token_hash(
            hash_device_token(self._settings, raw_token)
        )
        if token is None:
            return None
        await self._repo.touch_token(token, _utcnow())
        return token.employee_id
