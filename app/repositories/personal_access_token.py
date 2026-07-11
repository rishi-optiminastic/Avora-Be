"""Personal Access Token persistence.

All queries live here (Layering §4). A token authenticates only while it is not
revoked AND not past its optional `expires_at`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.personal_access_token import PersonalAccessToken


class PersonalAccessTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_token(
        self,
        *,
        employee_id: uuid.UUID,
        label: str,
        token_hash: str,
        expires_at: datetime | None,
    ) -> PersonalAccessToken:
        token = PersonalAccessToken(
            employee_id=employee_id,
            label=label,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def list_tokens(self, employee_id: uuid.UUID) -> Sequence[PersonalAccessToken]:
        result = await self._session.execute(
            select(PersonalAccessToken)
            .where(PersonalAccessToken.employee_id == employee_id)
            .order_by(PersonalAccessToken.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_token(
        self, token_id: uuid.UUID, employee_id: uuid.UUID
    ) -> PersonalAccessToken | None:
        result = await self._session.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.id == token_id,
                PersonalAccessToken.employee_id == employee_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_active_by_token_hash(self, token_hash: str) -> PersonalAccessToken | None:
        """A token usable right now: matching hash, not revoked, not expired."""
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.token_hash == token_hash,
                PersonalAccessToken.is_revoked.is_(False),
                or_(
                    PersonalAccessToken.expires_at.is_(None),
                    PersonalAccessToken.expires_at > now,
                ),
            )
        )
        return result.scalar_one_or_none()

    async def revoke_token(self, token: PersonalAccessToken) -> PersonalAccessToken:
        token.is_revoked = True
        await self._session.flush()
        return token

    async def touch_token(self, token: PersonalAccessToken, used_at: datetime) -> None:
        token.last_used_at = used_at
        await self._session.flush()
