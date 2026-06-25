"""Env Sync data access. The only place Env Sync queries are built.

Access is **membership-scoped**: a caller sees a project only if they have an
`EnvProjectMember` row for it (Security rule 5.3). The push path locks the
project row so two concurrent pushes can't both believe they're based on head.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.env_access_token import EnvAccessToken
from app.models.env_project import EnvMemberRole, EnvProject, EnvProjectMember, EnvVersion


class EnvSyncRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- projects & membership -------------------------------------------- #
    async def get_project(self, project_id: uuid.UUID) -> EnvProject | None:
        return await self._session.get(EnvProject, project_id)

    async def member_for(
        self, project_id: uuid.UUID, employee_id: uuid.UUID
    ) -> EnvProjectMember | None:
        result = await self._session.execute(
            select(EnvProjectMember).where(
                EnvProjectMember.project_id == project_id,
                EnvProjectMember.employee_id == employee_id,
            )
        )
        return result.scalar_one_or_none()

    async def projects_for_employee(
        self, employee_id: uuid.UUID
    ) -> Sequence[tuple[EnvProject, EnvMemberRole]]:
        result = await self._session.execute(
            select(EnvProject, EnvProjectMember.role)
            .join(EnvProjectMember, EnvProjectMember.project_id == EnvProject.id)
            .where(EnvProjectMember.employee_id == employee_id)
            .order_by(EnvProject.name)
        )
        return [(p, role) for p, role in result.all()]

    async def list_members(
        self, project_id: uuid.UUID
    ) -> Sequence[tuple[EnvProjectMember, Employee]]:
        result = await self._session.execute(
            select(EnvProjectMember, Employee)
            .join(Employee, Employee.id == EnvProjectMember.employee_id)
            .where(EnvProjectMember.project_id == project_id)
            .order_by(Employee.full_name)
        )
        return [(m, e) for m, e in result.all()]

    async def create_project(
        self, *, name: str, owner_id: uuid.UUID, department: str | None
    ) -> EnvProject:
        project = EnvProject(name=name, owner_id=owner_id, department=department)
        self._session.add(project)
        await self._session.flush()
        self._session.add(
            EnvProjectMember(
                project_id=project.id, employee_id=owner_id, role=EnvMemberRole.OWNER
            )
        )
        await self._session.flush()
        return project

    async def add_member(
        self, *, project_id: uuid.UUID, employee_id: uuid.UUID, role: EnvMemberRole
    ) -> EnvProjectMember:
        existing = await self.member_for(project_id, employee_id)
        if existing is not None:
            existing.role = role
            await self._session.flush()
            return existing
        member = EnvProjectMember(project_id=project_id, employee_id=employee_id, role=role)
        self._session.add(member)
        await self._session.flush()
        return member

    async def remove_member(self, *, project_id: uuid.UUID, employee_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(EnvProjectMember).where(
                EnvProjectMember.project_id == project_id,
                EnvProjectMember.employee_id == employee_id,
            )
        )
        await self._session.flush()

    # --- versions --------------------------------------------------------- #
    async def lock_project(self, project_id: uuid.UUID) -> None:
        """Take a row lock on the project so the head check + insert in a push are
        atomic against a concurrent push (equivalent to Django select_for_update)."""
        await self._session.execute(
            select(EnvProject.id).where(EnvProject.id == project_id).with_for_update()
        )

    async def head(self, project_id: uuid.UUID, environment: str) -> EnvVersion | None:
        result = await self._session.execute(
            select(EnvVersion)
            .where(EnvVersion.project_id == project_id, EnvVersion.environment == environment)
            .order_by(EnvVersion.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def environment_names(self, project_id: uuid.UUID) -> Sequence[str]:
        result = await self._session.execute(
            select(EnvVersion.environment)
            .where(EnvVersion.project_id == project_id)
            .distinct()
            .order_by(EnvVersion.environment)
        )
        return list(result.scalars().all())

    async def history(self, project_id: uuid.UUID, environment: str) -> Sequence[EnvVersion]:
        result = await self._session.execute(
            select(EnvVersion)
            .where(EnvVersion.project_id == project_id, EnvVersion.environment == environment)
            .order_by(EnvVersion.created_at.desc())
        )
        return list(result.scalars().all())

    async def create_version(
        self,
        *,
        project_id: uuid.UUID,
        environment: str,
        content_encrypted: str,
        content_hash: str,
        parent_version_id: uuid.UUID | None,
        created_by_id: uuid.UUID,
    ) -> EnvVersion:
        version = EnvVersion(
            project_id=project_id,
            environment=environment,
            content_encrypted=content_encrypted,
            content_hash=content_hash,
            parent_version_id=parent_version_id,
            created_by_id=created_by_id,
        )
        self._session.add(version)
        await self._session.flush()
        return version

    # --- personal access tokens ------------------------------------------- #
    async def create_token(
        self, *, employee_id: uuid.UUID, label: str, token_hash: str
    ) -> EnvAccessToken:
        token = EnvAccessToken(employee_id=employee_id, label=label, token_hash=token_hash)
        self._session.add(token)
        await self._session.flush()
        return token

    async def list_tokens(self, employee_id: uuid.UUID) -> Sequence[EnvAccessToken]:
        result = await self._session.execute(
            select(EnvAccessToken)
            .where(EnvAccessToken.employee_id == employee_id)
            .order_by(EnvAccessToken.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_token(
        self, token_id: uuid.UUID, employee_id: uuid.UUID
    ) -> EnvAccessToken | None:
        result = await self._session.execute(
            select(EnvAccessToken).where(
                EnvAccessToken.id == token_id,
                EnvAccessToken.employee_id == employee_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_active_by_token_hash(self, token_hash: str) -> EnvAccessToken | None:
        result = await self._session.execute(
            select(EnvAccessToken).where(
                EnvAccessToken.token_hash == token_hash,
                EnvAccessToken.is_revoked.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def revoke_token(self, token: EnvAccessToken) -> EnvAccessToken:
        token.is_revoked = True
        await self._session.flush()
        return token

    async def touch_token(self, token: EnvAccessToken, used_at: datetime) -> None:
        token.last_used_at = used_at
        await self._session.flush()
