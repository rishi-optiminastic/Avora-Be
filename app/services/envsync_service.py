"""Env Sync business rules.

Reads/writes are authorized here against project membership (re-derived from the
DB, never the client). Secrets are encrypted before they reach the repository and
decrypted only when serving an authorized member. Every push is audited. No
FastAPI objects here (Layering §4)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.envsync_crypto import content_hash, decrypt, encrypt
from app.core.exceptions import (
    AuthorizationError,
    EnvConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import generate_device_token, hash_device_token
from app.models.env_project import EnvMemberRole, EnvProjectMember, EnvVersion
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.envsync import EnvSyncRepository
from app.schemas.auth import EnvPrincipal
from app.schemas.envsync import (
    CollaboratorAdd,
    CollaboratorRead,
    EnvHistoryRead,
    EnvironmentRead,
    EnvProjectCreate,
    EnvProjectRead,
    EnvPush,
    EnvVersionRead,
    TokenCreate,
    TokenCreated,
    TokenRead,
)
from app.services.envsync_realtime import hub

logger = get_logger("app.envsync")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EnvSyncService:
    def __init__(
        self,
        repo: EnvSyncRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._employees = employees
        self._audit = audit
        self._settings = settings

    # --- access helpers --------------------------------------------------- #
    async def _require_member(
        self, caller: EnvPrincipal, project_id: uuid.UUID
    ) -> EnvProjectMember:
        """Return the caller's membership, or 404 if the project is missing OR the
        caller isn't a member (never leak existence to a non-member — rule 5.3)."""
        member = await self._repo.member_for(project_id, caller.employee_id)
        if member is None:
            raise NotFoundError("Project not found.")
        return member

    async def _require_writer(
        self, caller: EnvPrincipal, project_id: uuid.UUID
    ) -> EnvProjectMember:
        member = await self._require_member(caller, project_id)
        if not member.can_write:
            raise AuthorizationError("You have read-only access to this project.")
        return member

    async def _require_owner(self, caller: EnvPrincipal, project_id: uuid.UUID) -> EnvProjectMember:
        member = await self._require_member(caller, project_id)
        if member.role is not EnvMemberRole.OWNER:
            raise AuthorizationError("Only an owner can manage collaborators.")
        return member

    async def _version_read(self, version: EnvVersion) -> EnvVersionRead:
        name = await self._creator_name(version.created_by_id)
        return EnvVersionRead(
            version_id=version.id,
            hash=version.content_hash,
            content=decrypt(self._settings, version.content_encrypted),
            updated_by=name,
            updated_at=version.created_at,
        )

    async def _creator_name(self, employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "unknown"
        employee = await self._employees.get(employee_id)
        return employee.full_name if employee is not None else "unknown"

    # --- projects --------------------------------------------------------- #
    async def list_projects(self, caller: EnvPrincipal) -> list[EnvProjectRead]:
        rows = await self._repo.projects_for_employee(caller.employee_id)
        return [
            EnvProjectRead(id=p.id, name=p.name, role=role, department=p.department)
            for p, role in rows
        ]

    async def create_project(
        self, caller: EnvPrincipal, payload: EnvProjectCreate
    ) -> EnvProjectRead:
        project = await self._repo.create_project(
            name=payload.name, owner_id=caller.employee_id, department=payload.department
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="envsync.project_created",
            target=str(project.id),
        )
        return EnvProjectRead(
            id=project.id,
            name=project.name,
            role=EnvMemberRole.OWNER,
            department=project.department,
        )

    # --- collaborators ---------------------------------------------------- #
    async def list_collaborators(
        self, caller: EnvPrincipal, project_id: uuid.UUID
    ) -> list[CollaboratorRead]:
        await self._require_member(caller, project_id)
        members = await self._repo.list_members(project_id)
        return [
            CollaboratorRead(
                id=employee.id, name=employee.full_name, email=employee.work_email, role=member.role
            )
            for member, employee in members
        ]

    async def add_collaborator(
        self, caller: EnvPrincipal, project_id: uuid.UUID, payload: CollaboratorAdd
    ) -> CollaboratorRead:
        await self._require_owner(caller, project_id)
        employee = await self._employees.get_by_work_email(payload.email)
        if employee is None or not employee.is_active:
            raise ValidationError("No active employee with that work email.")
        await self._repo.add_member(
            project_id=project_id, employee_id=employee.id, role=payload.role
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="envsync.collaborator_added",
            target=f"{project_id}:{employee.id}",
        )
        return CollaboratorRead(
            id=employee.id, name=employee.full_name, email=employee.work_email, role=payload.role
        )

    async def remove_collaborator(
        self, caller: EnvPrincipal, project_id: uuid.UUID, employee_id: uuid.UUID
    ) -> None:
        await self._require_owner(caller, project_id)
        target = await self._repo.member_for(project_id, employee_id)
        if target is None:
            raise NotFoundError("That person is not a collaborator on this project.")
        if target.role is EnvMemberRole.OWNER:
            owners = [
                m
                for m, _ in await self._repo.list_members(project_id)
                if m.role is EnvMemberRole.OWNER
            ]
            if len(owners) <= 1:
                raise ValidationError("Cannot remove the only owner of a project.")
        await self._repo.remove_member(project_id=project_id, employee_id=employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="envsync.collaborator_removed",
            target=f"{project_id}:{employee_id}",
        )

    # --- environments & versions ------------------------------------------ #
    async def list_environments(
        self, caller: EnvPrincipal, project_id: uuid.UUID
    ) -> list[EnvironmentRead]:
        await self._require_member(caller, project_id)
        result: list[EnvironmentRead] = []
        for name in await self._repo.environment_names(project_id):
            head = await self._repo.head(project_id, name)
            if head is None:
                continue
            result.append(
                EnvironmentRead(
                    environment=name,
                    version_id=head.id,
                    hash=head.content_hash,
                    updated_by=await self._creator_name(head.created_by_id),
                    updated_at=head.created_at,
                )
            )
        return result

    async def get_env(
        self, caller: EnvPrincipal, project_id: uuid.UUID, environment: str
    ) -> EnvVersionRead:
        await self._require_member(caller, project_id)
        head = await self._repo.head(project_id, environment)
        if head is None:
            raise NotFoundError("No env has been pushed for this project yet.")
        await self._audit.append(
            actor=str(caller.employee_id),
            action="envsync.pull",
            target=f"{project_id}:{environment}:{head.id}",
        )
        return await self._version_read(head)

    async def get_history(
        self, caller: EnvPrincipal, project_id: uuid.UUID, environment: str
    ) -> list[EnvHistoryRead]:
        await self._require_member(caller, project_id)
        versions = await self._repo.history(project_id, environment)
        names = await self._names_for(versions)
        return [
            EnvHistoryRead(
                version_id=v.id,
                hash=v.content_hash,
                updated_by=names.get(v.created_by_id, "unknown"),
                updated_at=v.created_at,
            )
            for v in versions
        ]

    async def _names_for(self, versions: Sequence[EnvVersion]) -> dict[uuid.UUID | None, str]:
        ids = {v.created_by_id for v in versions if v.created_by_id is not None}
        employees = await self._employees.get_many(list(ids))
        return {eid: emp.full_name for eid, emp in employees.items()}

    async def push_env(
        self, caller: EnvPrincipal, project_id: uuid.UUID, payload: EnvPush
    ) -> tuple[EnvVersionRead, bool]:
        """Push a new version. Returns (version, created). `created=False` is a
        no-op (identical content). Raises EnvConflictError if the remote head
        moved past the client's base version."""
        await self._require_writer(caller, project_id)

        # Lock the project row so the head check + insert are atomic against a
        # concurrent push (rule: last-write-wins, but nothing is silently lost).
        await self._repo.lock_project(project_id)
        head = await self._repo.head(project_id, payload.environment)
        head_id = head.id if head is not None else None

        if payload.base_version != head_id:
            head_read = await self._version_read(head) if head is not None else None
            raise EnvConflictError(
                head=head_read.model_dump(mode="json") if head_read is not None else None
            )

        new_hash = content_hash(payload.content)
        if head is not None and head.content_hash == new_hash:
            return await self._version_read(head), False  # no-op → 200

        version = await self._repo.create_version(
            project_id=project_id,
            environment=payload.environment,
            content_encrypted=encrypt(self._settings, payload.content),
            content_hash=new_hash,
            parent_version_id=head_id,
            created_by_id=caller.employee_id,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="envsync.push",
            target=f"{project_id}:{payload.environment}:{version.id}",
        )
        read = await self._version_read(version)
        await hub.broadcast(
            project_id,
            {
                "type": "env.updated",
                "project_id": str(project_id),
                "environment": payload.environment,
                "version_id": str(version.id),
                "hash": read.hash,
                "updated_by": read.updated_by,
                "updated_at": read.updated_at.isoformat(),
            },
        )
        return read, True

    # --- personal access tokens ------------------------------------------- #
    async def issue_token(self, caller: EnvPrincipal, payload: TokenCreate) -> TokenCreated:
        raw = generate_device_token()
        token = await self._repo.create_token(
            employee_id=caller.employee_id,
            label=payload.label,
            token_hash=hash_device_token(self._settings, raw),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="envsync.token_created",
            target=str(token.id),
        )
        return TokenCreated(
            id=token.id,
            label=token.label,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
            is_revoked=token.is_revoked,
            token=raw,
        )

    async def list_tokens(self, caller: EnvPrincipal) -> list[TokenRead]:
        tokens = await self._repo.list_tokens(caller.employee_id)
        return [
            TokenRead(
                id=t.id,
                label=t.label,
                created_at=t.created_at,
                last_used_at=t.last_used_at,
                is_revoked=t.is_revoked,
            )
            for t in tokens
        ]

    async def revoke_token(self, caller: EnvPrincipal, token_id: uuid.UUID) -> None:
        token = await self._repo.get_token(token_id, caller.employee_id)
        if token is None:
            raise NotFoundError("Token not found.")
        if not token.is_revoked:
            await self._repo.revoke_token(token)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="envsync.token_revoked",
            target=str(token.id),
        )

    async def resolve_token(self, raw_token: str) -> uuid.UUID | None:
        """Authenticate a Personal Access Token → employee id (or None). Used by
        the auth dependency; bumps last_used_at on success."""
        token = await self._repo.get_active_by_token_hash(
            hash_device_token(self._settings, raw_token)
        )
        if token is None:
            return None
        await self._repo.touch_token(token, _utcnow())
        return token.employee_id
