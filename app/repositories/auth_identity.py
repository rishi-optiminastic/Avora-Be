"""Access to the Better Auth identity row for an employee.

Scoped to exactly one job: keeping the sign-in address in step with the
roster's work email. Nothing here reads or writes credentials - `auth_account`
holds the password hash and is keyed on the user id, not the email, so moving
an address never touches it and never forces a reset.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_identity import auth_user


class AuthIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_id_by_email(self, email: str) -> str | None:
        """The Better Auth user id for this address, matched case-insensitively.

        Returns None when nobody has signed up under it - an invited employee
        who never created an account has no `auth_user` row at all, which is a
        normal state and not an error.
        """
        result = await self._session.execute(
            select(auth_user.c.id).where(func.lower(auth_user.c.email) == email.lower())
        )
        return result.scalar_one_or_none()

    async def set_email(self, *, user_id: str, new_email: str) -> None:
        """Move this identity to `new_email`.

        Callers reach here only for an id `find_id_by_email` just returned in
        the same transaction, so there is no "no such row" case to report back.
        Whether an identity exists at all is the caller's question, answered by
        that lookup, not by a row count here.
        """
        await self._session.execute(
            update(auth_user)
            .where(auth_user.c.id == user_id)
            .values(email=new_email, updatedAt=datetime.now(UTC).replace(tzinfo=None))
        )
        await self._session.flush()
