"""Invitation business rules.

Creating an invite is an admin-only action; accepting one provisions an
employee with the invited role. No FastAPI objects here (Layering §4); the
route enforces admin/HR auth via `AdminOrHrDep` and we re-check the role in-service.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError
from app.core.names import clean_display_name, placeholder_name_from_email
from app.core.security import generate_invite_token, hash_invite_token
from app.models.employee import Employee, Role
from app.models.invitation import Invitation, InvitationStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.invitation import InvitationRepository
from app.schemas.auth import CurrentUser
from app.services.email_service import EmailService

ORG_NAME = "the team"


def _role_label(role: Role) -> str:
    return role.value.replace("_", " ").title()


class InvitationService:
    def __init__(
        self,
        settings: Settings,
        invitations: InvitationRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        email: EmailService,
    ) -> None:
        self._settings = settings
        self._invitations = invitations
        self._employees = employees
        self._audit = audit
        self._email = email

    async def create(
        self, caller: CurrentUser, *, email: str, role: Role, department: str | None = None
    ) -> tuple[Invitation, str]:
        # Privilege-granting action — admins only (rule 5.5). The route already
        # enforces this; we re-check so the rule can't be bypassed by reuse.
        if caller.role not in (Role.ADMIN, Role.HR):
            raise AuthorizationError()

        normalized = email.strip().lower()
        dept = department.strip() if department and department.strip() else None
        raw_token = generate_invite_token()
        expires_at = datetime.now(UTC) + timedelta(hours=self._settings.invite_ttl_hours)

        invitation = await self._invitations.create(
            email=normalized,
            role=role,
            department=dept,
            token_hash=hash_invite_token(self._settings, raw_token),
            invited_by=caller.employee_id,
            expires_at=expires_at,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="invitation.create",
            target=f"invite:{normalized}:{role.value}",
        )
        accept_url = await self._deliver(invitation, raw_token)
        return invitation, accept_url

    async def resend(
        self, caller: CurrentUser, *, invitation_id: uuid.UUID
    ) -> tuple[Invitation, str]:
        """Admin-only: re-issue a fresh link for a pending invite (old one dies)."""
        if caller.role not in (Role.ADMIN, Role.HR):
            raise AuthorizationError()
        invitation = await self._invitations.get(invitation_id)
        if invitation is None:
            raise NotFoundError()
        if invitation.status is not InvitationStatus.PENDING:
            raise ConflictError("This invitation can no longer be resent.")

        raw_token = generate_invite_token()
        expires_at = datetime.now(UTC) + timedelta(hours=self._settings.invite_ttl_hours)
        await self._invitations.rotate_token(
            invitation,
            token_hash=hash_invite_token(self._settings, raw_token),
            expires_at=expires_at,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="invitation.resend",
            target=f"invite:{invitation.id}",
        )
        accept_url = await self._deliver(invitation, raw_token)
        return invitation, accept_url

    async def list_pending(self, caller: CurrentUser) -> Sequence[Invitation]:
        if caller.role not in (Role.ADMIN, Role.HR):
            raise AuthorizationError()
        return await self._invitations.list_pending()

    async def revoke(self, caller: CurrentUser, *, invitation_id: uuid.UUID) -> Invitation:
        """Admin/HR: cancel a pending invitation so its link can no longer be used."""
        if caller.role not in (Role.ADMIN, Role.HR):
            raise AuthorizationError()
        invitation = await self._invitations.get(invitation_id)
        if invitation is None:
            raise NotFoundError()
        if invitation.status is not InvitationStatus.PENDING:
            raise ConflictError("Only a pending invitation can be revoked.")
        await self._invitations.mark_revoked(invitation)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="invitation.revoke",
            target=f"invite:{invitation.id}",
        )
        return invitation

    async def _deliver(self, invitation: Invitation, raw_token: str) -> str:
        """Build the accept URL and email it. Raises on send failure so the
        surrounding request transaction rolls back — we never keep an invite
        whose link couldn't be delivered."""
        accept_url = f"{self._settings.app_base_url.rstrip('/')}/invite/{raw_token}"
        inviter = await self._employees.get(invitation.invited_by)
        await self._email.send_invite(
            to=invitation.email,
            inviter_name=inviter.full_name if inviter else "An administrator",
            role_label=_role_label(invitation.role),
            org_name=ORG_NAME,
            accept_url=accept_url,
            expires_label=f"on {invitation.expires_at:%d %b %Y}",
        )
        return accept_url

    async def info(self, raw_token: str) -> tuple[Invitation, str | None]:
        """Validated invite + the inviter's name, for the acceptance page."""
        invitation = await self.get_valid(raw_token)
        inviter = await self._employees.get(invitation.invited_by)
        return invitation, inviter.full_name if inviter else None

    async def get_valid(self, raw_token: str) -> Invitation:
        invitation = await self._invitations.get_by_token_hash(
            hash_invite_token(self._settings, raw_token)
        )
        if invitation is None:
            raise NotFoundError()
        if invitation.status is not InvitationStatus.PENDING:
            raise ConflictError("This invitation has already been used.")
        # Some drivers (e.g. SQLite in tests) return naive datetimes; treat a
        # naive value as UTC so the comparison is always offset-aware.
        expires_at = invitation.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise ConflictError("This invitation has expired.")
        return invitation

    async def accept(
        self, *, identity_email: str, identity_name: str | None, raw_token: str
    ) -> Employee:
        invitation = await self.get_valid(raw_token)

        # The signed-in account must be the person who was invited.
        if invitation.email != identity_email.strip().lower():
            raise AuthorizationError()

        # Prefer the real name from the signed-in identity (Google profile /
        # sign-up); only fall back to an email-derived placeholder when absent.
        provider_name = clean_display_name(identity_name)

        employee = await self._employees.get_by_work_email(invitation.email)
        if employee is None:
            employee = await self._employees.create_from_invite(
                work_email=invitation.email,
                full_name=provider_name or placeholder_name_from_email(invitation.email),
                role=invitation.role,
                department=invitation.department,
            )
        else:
            await self._employees.refresh_display_name(employee, identity_name)

        await self._invitations.mark_accepted(
            invitation, employee_id=employee.id, accepted_at=datetime.now(UTC)
        )
        await self._audit.append(
            actor=str(employee.id),
            action="invitation.accept",
            target=f"invite:{invitation.id}",
        )
        return employee
