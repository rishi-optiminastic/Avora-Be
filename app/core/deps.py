"""Shared FastAPI dependencies — wiring + auth + scoping.

This is the only place FastAPI's `Depends`/`Request` meet the service layer.
Auth dependencies re-derive identity, role and scope from the DB; they NEVER
trust a client-supplied role, team, or id (Golden rules #1, #2).
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, AuthorizationError, RateLimitError
from app.core.ratelimit import FixedWindowRateLimiter
from app.core.security import (
    TokenError,
    decode_better_auth_jwt,
    hash_device_token,
    verify_hmac_sha256,
)
from app.db.session import get_session
from app.repositories.activity import ActivityRepository
from app.repositories.audit import AuditRepository
from app.repositories.device import DeviceRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.holiday import HolidayRepository
from app.repositories.invitation import InvitationRepository
from app.repositories.leave import LeaveRepository
from app.repositories.leave_comment import LeaveCommentRepository
from app.repositories.task import TaskRepository
from app.schemas.auth import AuthIdentity, CurrentDevice, CurrentUser
from app.services.activity_service import ActivityService
from app.services.email_service import EmailService
from app.services.employee_service import EmployeeService
from app.services.holiday_service import HolidayService
from app.services.hr_service import HRService
from app.services.invitation_service import InvitationService
from app.services.leave_service import LeaveService
from app.services.task_service import TaskService

# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in get_session():
        yield session


DbDep = Annotated[AsyncSession, Depends(get_db)]


# --------------------------------------------------------------------------- #
# Repository providers
# --------------------------------------------------------------------------- #
def get_employee_repo(db: DbDep) -> EmployeeRepository:
    return EmployeeRepository(db)


def get_device_repo(db: DbDep) -> DeviceRepository:
    return DeviceRepository(db)


def get_activity_repo(db: DbDep) -> ActivityRepository:
    return ActivityRepository(db)


def get_audit_repo(db: DbDep) -> AuditRepository:
    return AuditRepository(db)


def get_invitation_repo(db: DbDep) -> InvitationRepository:
    return InvitationRepository(db)


def get_task_repo(db: DbDep) -> TaskRepository:
    return TaskRepository(db)


def get_leave_repo(db: DbDep) -> LeaveRepository:
    return LeaveRepository(db)


def get_leave_comment_repo(db: DbDep) -> LeaveCommentRepository:
    return LeaveCommentRepository(db)


def get_holiday_repo(db: DbDep) -> HolidayRepository:
    return HolidayRepository(db)


# --------------------------------------------------------------------------- #
# Service providers
# --------------------------------------------------------------------------- #
def get_email_service(settings: SettingsDep) -> EmailService:
    return EmailService(settings)


def get_employee_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> EmployeeService:
    return EmployeeService(employees, audit)


def get_hr_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> HRService:
    return HRService(employees, audit)


def get_activity_service(
    settings: SettingsDep,
    devices: Annotated[DeviceRepository, Depends(get_device_repo)],
    activity: Annotated[ActivityRepository, Depends(get_activity_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> ActivityService:
    return ActivityService(settings, devices, activity, audit)


def get_invitation_service(
    settings: SettingsDep,
    invitations: Annotated[InvitationRepository, Depends(get_invitation_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
    email: Annotated[EmailService, Depends(get_email_service)],
) -> InvitationService:
    return InvitationService(settings, invitations, employees, audit, email)


def get_task_service(
    tasks: Annotated[TaskRepository, Depends(get_task_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> TaskService:
    return TaskService(tasks, employees, audit)


def get_leave_service(
    leaves: Annotated[LeaveRepository, Depends(get_leave_repo)],
    comments: Annotated[LeaveCommentRepository, Depends(get_leave_comment_repo)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> LeaveService:
    return LeaveService(leaves, comments, employees, audit)


def get_holiday_service(
    holidays: Annotated[HolidayRepository, Depends(get_holiday_repo)],
    audit: Annotated[AuditRepository, Depends(get_audit_repo)],
) -> HolidayService:
    return HolidayService(holidays, audit)


EmployeeServiceDep = Annotated[EmployeeService, Depends(get_employee_service)]
HRServiceDep = Annotated[HRService, Depends(get_hr_service)]
ActivityServiceDep = Annotated[ActivityService, Depends(get_activity_service)]
InvitationServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
LeaveServiceDep = Annotated[LeaveService, Depends(get_leave_service)]
HolidayServiceDep = Annotated[HolidayService, Depends(get_holiday_service)]


# --------------------------------------------------------------------------- #
# Human authentication — Better Auth JWT (Bearer) verified via JWKS.
#
# Better Auth (the Next.js app) authenticates the human and issues a short-lived
# asymmetric JWT. We verify its signature against the published JWKS, then
# re-derive role/scope from OUR employee record (HR is the source of truth) —
# authentication there, authorization here. The token is identity only; we
# never trust a client-supplied role/team/id (Golden rules #1, #2; rule 5.2).
# --------------------------------------------------------------------------- #
@lru_cache
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    """Cached JWKS client (keeps the fetched keys warm across requests)."""
    return jwt.PyJWKClient(jwks_url)


def get_jwks_client(settings: SettingsDep) -> jwt.PyJWKClient:
    return _jwks_client(settings.better_auth_jwks_url)


JwksClientDep = Annotated[jwt.PyJWKClient, Depends(get_jwks_client)]


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError()
    token = authorization[7:].strip()
    if not token:
        raise AuthenticationError()
    return token


def _verify_identity(
    settings: Settings, jwks_client: jwt.PyJWKClient, authorization: str | None
) -> AuthIdentity:
    """Verify a Better Auth Bearer JWT and return the identity it asserts.

    Authentication only — no DB, no employee lookup. Both the full user
    dependency and the invite-accept flow build on this.
    """
    token = _bearer_token(authorization)
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token).key
        claims = decode_better_auth_jwt(settings, token, key=signing_key)
    except (TokenError, jwt.PyJWKClientError, jwt.InvalidTokenError) as exc:
        raise AuthenticationError() from exc

    subject = claims.get("sub")
    email = claims.get("email")
    if not isinstance(subject, str) or not isinstance(email, str) or not email:
        raise AuthenticationError()
    return AuthIdentity(subject=subject, email=email)


async def get_auth_identity(
    settings: SettingsDep,
    jwks_client: JwksClientDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthIdentity:
    """Verified identity that need NOT correspond to an employee yet."""
    return _verify_identity(settings, jwks_client, authorization)


AuthIdentityDep = Annotated[AuthIdentity, Depends(get_auth_identity)]


async def get_current_user(
    settings: SettingsDep,
    employees: Annotated[EmployeeRepository, Depends(get_employee_repo)],
    jwks_client: JwksClientDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    identity = _verify_identity(settings, jwks_client, authorization)

    # Map the verified identity to OUR employee by work email, then re-derive
    # role/scope from the DB. A Better Auth user with no employee record (e.g. a
    # fresh self-signup) is authenticated but NOT authorized — employees are
    # provisioned by HR or by accepting an admin invite, never by self-signup.
    employee = await employees.get_by_work_email(identity.email)
    if employee is None or not employee.is_active:
        raise AuthenticationError()

    return CurrentUser(
        employee_id=employee.id,
        role=employee.role,
        manager_id=employee.manager_id,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_admin(user: CurrentUserDep) -> CurrentUser:
    if not user.is_admin:
        raise AuthorizationError()
    return user


AdminDep = Annotated[CurrentUser, Depends(require_admin)]


# Per-user write throttle for chatty endpoints (e.g. leave comments) so a copied
# request can't be replayed in a tight loop to spam. In-process; back with Redis
# for multi-instance (see ratelimit module).
_comment_limiter = FixedWindowRateLimiter(max_requests=15, window_seconds=60)


def verify_comment_rate_limit(user: CurrentUserDep) -> CurrentUser:
    if not _comment_limiter.allow(f"leave-comment:{user.employee_id}"):
        raise RateLimitError("Too many messages — please slow down.")
    return user


CommentRateLimitDep = Annotated[CurrentUser, Depends(verify_comment_rate_limit)]


# --------------------------------------------------------------------------- #
# Agent authentication — per-device bearer token + HMAC body signature.
# --------------------------------------------------------------------------- #
_ingest_limiter = FixedWindowRateLimiter(max_requests=10_000)  # reconfigured at startup


def configure_rate_limiter(settings: Settings) -> None:
    global _ingest_limiter
    _ingest_limiter = FixedWindowRateLimiter(max_requests=settings.agent_ingest_rate_per_minute)


async def get_current_device(
    request: Request,
    settings: SettingsDep,
    devices: Annotated[DeviceRepository, Depends(get_device_repo)],
    authorization: Annotated[str | None, Header()] = None,
    x_signature: Annotated[str | None, Header()] = None,
) -> CurrentDevice:
    """Authenticate an agent: bearer device token + HMAC over the raw body.

    Order matters: cheap auth checks first, then the HMAC over the exact bytes
    we will parse. Rate limiting is per device, after we know which device.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError()
    raw_token = authorization[7:].strip()
    if not raw_token:
        raise AuthenticationError()

    device = await devices.get_active_by_token_hash(hash_device_token(settings, raw_token))
    if device is None:
        raise AuthenticationError()

    # HMAC over the exact request body the route will validate (rule 5.4).
    body = await request.body()
    if not x_signature or not verify_hmac_sha256(raw_token, body, x_signature):
        raise AuthenticationError()

    # Per-device rate limit.
    if not _ingest_limiter.allow(str(device.id)):
        raise AuthorizationError("Rate limit exceeded.")

    return CurrentDevice(
        device_id=device.id,
        employee_id=device.employee_id,
        last_sequence=device.last_sequence,
    )


CurrentDeviceDep = Annotated[CurrentDevice, Depends(get_current_device)]


# --------------------------------------------------------------------------- #
# HR webhook authentication — HMAC shared secret + optional IP allowlist.
# --------------------------------------------------------------------------- #
def _ip_allowed(settings: Settings, client_ip: str | None) -> bool:
    allowlist = settings.hr_ip_allowlist
    if not allowlist:  # empty = allow all (dev only; warn in prod config check)
        return True
    if client_ip is None:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


async def verify_hr_webhook(
    request: Request,
    settings: SettingsDep,
    x_hr_signature: Annotated[str | None, Header()] = None,
) -> bytes:
    """Verify the HR webhook HMAC + IP allowlist; return the raw body bytes."""
    client_ip = request.client.host if request.client else None
    if not _ip_allowed(settings, client_ip):
        raise AuthorizationError("Source not allowed.")

    body = await request.body()
    if not x_hr_signature or not verify_hmac_sha256(
        settings.hr_webhook_secret, body, x_hr_signature
    ):
        raise AuthenticationError()
    return body
