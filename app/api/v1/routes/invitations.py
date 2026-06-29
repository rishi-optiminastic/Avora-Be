"""Invitation endpoints.

- POST /invitations          → admin invites someone (creates + emails a link).
- GET  /invitations/{token}  → token-gated invite details for the accept page.
- POST /invitations/accept   → the invited person accepts (provisions employee).

Routes only parse input, call the service, and return a response schema.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from app.core.deps import (
    AdminDep,
    AdminOrHrDep,
    AuthIdentityDep,
    IdempotencyKeyHeader,
    IdempotencyServiceDep,
    InvitationServiceDep,
)
from app.schemas.employee import EmployeeRead
from app.schemas.invitation import (
    InvitationAccept,
    InvitationCreate,
    InvitationCreated,
    InvitationInfo,
    InvitationRead,
)
from app.services.invitation_service import ORG_NAME

router = APIRouter(prefix="/invitations", tags=["invitations"])


@router.post("", response_model=InvitationCreated, status_code=201)
async def create_invitation(
    payload: InvitationCreate,
    admin: AdminDep,
    service: InvitationServiceDep,
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    """Admin-only: invite a person at a role and email them the link (rule 5.5)."""

    async def _op() -> InvitationCreated:
        invitation, accept_url = await service.create(
            admin, email=payload.email, role=payload.role, department=payload.department
        )
        return InvitationCreated(
            id=invitation.id,
            email=invitation.email,
            role=invitation.role,
            department=invitation.department,
            accept_url=accept_url,
            expires_at=invitation.expires_at,
        )

    return await idem.run(
        principal_id=admin.employee_id,
        scope="invitations.create",
        key=idempotency_key,
        request=payload,
        operation=_op,
        success_status=201,
    )


@router.get("", response_model=list[InvitationRead])
async def list_invitations(admin: AdminDep, service: InvitationServiceDep) -> list[InvitationRead]:
    """Admin-only: pending invitations (for the team view)."""
    invitations = await service.list_pending(admin)
    return [InvitationRead.model_validate(i) for i in invitations]


@router.post("/{invitation_id}/resend", response_model=InvitationCreated)
async def resend_invitation(
    invitation_id: uuid.UUID,
    admin: AdminDep,
    service: InvitationServiceDep,
    idem: IdempotencyServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    """Admin-only: re-issue and re-email a pending invite (old link is revoked)."""

    async def _op() -> InvitationCreated:
        invitation, accept_url = await service.resend(admin, invitation_id=invitation_id)
        return InvitationCreated(
            id=invitation.id,
            email=invitation.email,
            role=invitation.role,
            department=invitation.department,
            accept_url=accept_url,
            expires_at=invitation.expires_at,
        )

    return await idem.run(
        principal_id=admin.employee_id,
        scope="invitations.resend",
        key=idempotency_key,
        request={"invitation_id": str(invitation_id)},
        operation=_op,
    )


@router.post("/{invitation_id}/revoke", response_model=InvitationRead)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    manager: AdminOrHrDep,
    service: InvitationServiceDep,
) -> InvitationRead:
    """Admin/HR: cancel a pending invitation so its link can no longer be used."""
    invitation = await service.revoke(manager, invitation_id=invitation_id)
    return InvitationRead.model_validate(invitation)


@router.get("/{token}", response_model=InvitationInfo)
async def get_invitation(token: str, service: InvitationServiceDep) -> InvitationInfo:
    """Token-gated: details for the acceptance page. The token is the secret."""
    invitation, inviter_name = await service.info(token)
    return InvitationInfo(
        email=invitation.email,
        role=invitation.role,
        department=invitation.department,
        org_name=ORG_NAME,
        inviter_name=inviter_name,
        expires_at=invitation.expires_at,
    )


@router.post("/accept", response_model=EmployeeRead)
async def accept_invitation(
    payload: InvitationAccept,
    identity: AuthIdentityDep,
    service: InvitationServiceDep,
) -> EmployeeRead:
    """The signed-in invitee accepts; we provision their employee record."""
    employee = await service.accept(
        identity_email=identity.email, identity_name=identity.name, raw_token=payload.token
    )
    return EmployeeRead.model_validate(employee)
