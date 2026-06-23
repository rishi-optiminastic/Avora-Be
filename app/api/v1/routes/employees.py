"""Employee endpoints — every one is scoped to the authenticated caller.

Routes only parse input, call a service, and return a response schema. No
business logic, no raw DB access (Layering §4). `response_model` is always set
and we return the schema, never the ORM object (Golden rule #5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.core import storage
from app.core.deps import (
    AdminDep,
    AdminOrHrDep,
    CurrentUserDep,
    EmployeeServiceDep,
    UploadRateLimitDep,
)
from app.core.exceptions import NotFoundError
from app.core.http import read_capped_body
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.employee import (
    AdminProfileUpdate,
    EmployeeRead,
    EmployeeRoleUpdate,
    EmployeeStatusUpdate,
    SelfProfileUpdate,
    TrackingModeUpdate,
)
from app.services.employee_service import MAX_AVATAR_BYTES

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("", response_model=Page[EmployeeRead])
async def list_employees(
    caller: CurrentUserDep,
    service: EmployeeServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> Page[EmployeeRead]:
    offset = (page - 1) * size
    items, total = await service.list_for_caller(caller, offset=offset, limit=size)
    return Page[EmployeeRead](
        items=[EmployeeRead.model_validate(e) for e in items],
        page=page,
        size=size,
        total=total,
    )


@router.get("/me", response_model=EmployeeRead)
async def get_me(caller: CurrentUserDep, service: EmployeeServiceDep) -> EmployeeRead:
    """The caller's own record — lets the client tailor UI to their role/scope."""
    return EmployeeRead.model_validate(await service.get_self(caller))


@router.patch("/me/tracking-mode", response_model=EmployeeRead)
async def set_my_tracking_mode(
    payload: TrackingModeUpdate,
    caller: CurrentUserDep,
    service: EmployeeServiceDep,
) -> EmployeeRead:
    """The employee pauses/resumes their own capture (work ↔ personal mode)."""
    return EmployeeRead.model_validate(await service.set_tracking_mode(caller, payload.mode))


@router.patch("/me/profile", response_model=EmployeeRead)
async def update_my_profile(
    payload: SelfProfileUpdate,
    caller: CurrentUserDep,
    service: EmployeeServiceDep,
) -> EmployeeRead:
    """The employee edits their own display fields (name, job title, timezone)."""
    return EmployeeRead.model_validate(
        await service.update_self_profile(
            caller,
            full_name=payload.full_name,
            job_title=payload.job_title,
            timezone=payload.timezone,
        )
    )


@router.post("/me/avatar", response_model=EmployeeRead, status_code=status.HTTP_201_CREATED)
async def upload_my_avatar(
    request: Request,
    caller: UploadRateLimitDep,
    service: EmployeeServiceDep,
    content_type: Annotated[str, Header()] = "application/octet-stream",
) -> EmployeeRead:
    """Upload the caller's own profile photo (image bytes in the body, ≤2 MB)."""
    data = await read_capped_body(request, MAX_AVATAR_BYTES)
    return EmployeeRead.model_validate(await service.set_my_avatar(caller, data, content_type))


@router.get("/{employee_id}/avatar")
async def get_employee_avatar(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: EmployeeServiceDep,
) -> Response:
    """An employee's profile photo, for any signed-in colleague (404 if none).

    Served inline (it renders in an <img>): S3-backed photos redirect to a
    short-lived presigned URL; DB-fallback photos stream their bytes.
    """
    employee = await service.get_avatar(caller, employee_id)
    media = employee.avatar_content_type or "image/jpeg"
    if employee.avatar_object_key:
        return RedirectResponse(storage.presigned_get_url(employee.avatar_object_key), 307)
    if employee.avatar_content is not None:
        return Response(content=employee.avatar_content, media_type=media)
    raise NotFoundError()


@router.get("/{employee_id}", response_model=EmployeeRead)
async def get_employee(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: EmployeeServiceDep,
) -> EmployeeRead:
    employee = await service.get_for_caller(caller, employee_id)
    return EmployeeRead.model_validate(employee)


@router.put("/{employee_id}/role", response_model=EmployeeRead)
async def set_employee_role(
    employee_id: uuid.UUID,
    payload: EmployeeRoleUpdate,
    admin: AdminDep,
    service: EmployeeServiceDep,
) -> EmployeeRead:
    """Admin-only privilege change — the sole path that sets a role (rule 5.5)."""
    employee = await service.set_role(admin, employee_id, payload.role)
    return EmployeeRead.model_validate(employee)


@router.patch("/{employee_id}", response_model=EmployeeRead)
async def admin_update_employee(
    employee_id: uuid.UUID,
    payload: AdminProfileUpdate,
    manager: AdminOrHrDep,
    service: EmployeeServiceDep,
) -> EmployeeRead:
    """Admin/HR edit of another employee's profile (name, department, title,
    manager, timezone, hire date, biometric id). Only the fields present in the
    body are written (PATCH). Role is NOT settable here — use the role endpoint;
    email/hr_external_id are identity keys and never editable."""
    employee = await service.admin_update_profile(manager, employee_id, payload)
    return EmployeeRead.model_validate(employee)


@router.patch("/{employee_id}/status", response_model=EmployeeRead)
async def set_employee_status(
    employee_id: uuid.UUID,
    payload: EmployeeStatusUpdate,
    manager: AdminOrHrDep,
    service: EmployeeServiceDep,
) -> EmployeeRead:
    """Admin/HR activate or deactivate (soft-delete / offboard) an employee."""
    employee = await service.set_active(manager, employee_id, payload.is_active)
    return EmployeeRead.model_validate(employee)


@router.patch("/{employee_id}/tracking-mode", response_model=EmployeeRead)
async def set_employee_tracking_mode(
    employee_id: uuid.UUID,
    payload: TrackingModeUpdate,
    manager: AdminOrHrDep,
    service: EmployeeServiceDep,
) -> EmployeeRead:
    """Admin/HR set another employee's capture mode (work/personal)."""
    employee = await service.set_tracking_mode_for(manager, employee_id, payload.mode)
    return EmployeeRead.model_validate(employee)


@router.post(
    "/{employee_id}/avatar",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
)
async def admin_upload_employee_avatar(
    employee_id: uuid.UUID,
    request: Request,
    manager: UploadRateLimitDep,
    service: EmployeeServiceDep,
    content_type: Annotated[str, Header()] = "application/octet-stream",
) -> EmployeeRead:
    """Admin/HR upload another employee's profile photo (image bytes in body, ≤2 MB).
    The service re-checks admin/HR authorization; the rate-limit dep caps abuse."""
    data = await read_capped_body(request, MAX_AVATAR_BYTES)
    return EmployeeRead.model_validate(
        await service.set_avatar_for(manager, employee_id, data, content_type)
    )
