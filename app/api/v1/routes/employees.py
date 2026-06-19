"""Employee endpoints — every one is scoped to the authenticated caller.

Routes only parse input, call a service, and return a response schema. No
business logic, no raw DB access (Layering §4). `response_model` is always set
and we return the schema, never the ORM object (Golden rule #5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import AdminDep, CurrentUserDep, EmployeeServiceDep
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from app.schemas.employee import EmployeeRead, EmployeeRoleUpdate, TrackingModeUpdate

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
