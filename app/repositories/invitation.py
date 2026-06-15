"""Invitation data access — the only place invitation queries are built."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Role
from app.models.invitation import Invitation, InvitationStatus


class InvitationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        email: str,
        role: Role,
        token_hash: str,
        invited_by: uuid.UUID,
        expires_at: datetime,
        department: str | None = None,
    ) -> Invitation:
        invitation = Invitation(
            email=email,
            role=role,
            department=department,
            token_hash=token_hash,
            invited_by=invited_by,
            expires_at=expires_at,
            status=InvitationStatus.PENDING,
        )
        self._session.add(invitation)
        await self._session.flush()
        return invitation

    async def get(self, invitation_id: uuid.UUID) -> Invitation | None:
        return await self._session.get(Invitation, invitation_id)

    async def get_by_token_hash(self, token_hash: str) -> Invitation | None:
        result = await self._session.execute(
            select(Invitation).where(Invitation.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def rotate_token(
        self, invitation: Invitation, *, token_hash: str, expires_at: datetime
    ) -> Invitation:
        """Issue a fresh token + expiry for a resend (old link stops working)."""
        invitation.token_hash = token_hash
        invitation.expires_at = expires_at
        await self._session.flush()
        return invitation

    async def mark_accepted(
        self, invitation: Invitation, *, employee_id: uuid.UUID, accepted_at: datetime
    ) -> Invitation:
        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_employee_id = employee_id
        invitation.accepted_at = accepted_at
        await self._session.flush()
        return invitation

    async def list_pending(self) -> Sequence[Invitation]:
        result = await self._session.execute(
            select(Invitation)
            .where(Invitation.status == InvitationStatus.PENDING)
            .order_by(Invitation.created_at.desc())
        )
        return result.scalars().all()
