"""Sheets endpoints - spreadsheets kept in Avora.

Every rule about who may view, edit or manage a sheet lives in WorkbookService;
these routes only parse and shape. The save body is read with a byte cap before
it is parsed, so an oversized workbook is refused without being buffered whole.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import pydantic
from fastapi import APIRouter, Query, Request, status
from fastapi.exceptions import RequestValidationError

from app.core.deps import CurrentUserDep, WorkbookServiceDep
from app.core.http import read_capped_body
from app.core.workbook_snapshot import MAX_SNAPSHOT_BYTES
from app.repositories.workbook import SheetScope
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, PageParams
from app.schemas.workbook import (
    PEOPLE_QUERY_MAX,
    TITLE_MAX,
    WorkbookContentSave,
    WorkbookCreate,
    WorkbookMemberRead,
    WorkbookMemberSet,
    WorkbookPerson,
    WorkbookRead,
    WorkbookSaved,
    WorkbookSummary,
    WorkbookUpdate,
)

router = APIRouter(prefix="/sheets", tags=["sheets"])

# Room for the save envelope (`version`, `force`) around the snapshot itself.
_SAVE_ENVELOPE_BYTES = 1024


@router.get("", response_model=Page[WorkbookSummary])
async def list_sheets(
    caller: CurrentUserDep,
    service: WorkbookServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    scope: SheetScope = SheetScope.ALL,
    q: Annotated[str | None, Query(max_length=TITLE_MAX)] = None,
) -> Page[WorkbookSummary]:
    query = q.strip() if q else None
    return await service.list_sheets(caller, PageParams(page=page, size=size), scope, query or None)


# Declared before "/{sheet_id}" so "people" is never read as a sheet id.
@router.get("/people", response_model=list[WorkbookPerson])
async def search_people(
    caller: CurrentUserDep,
    service: WorkbookServiceDep,
    q: Annotated[str, Query(min_length=1, max_length=PEOPLE_QUERY_MAX)],
) -> list[WorkbookPerson]:
    return await service.people(caller, q)


@router.post("", response_model=WorkbookRead, status_code=status.HTTP_201_CREATED)
async def create_sheet(
    payload: WorkbookCreate, caller: CurrentUserDep, service: WorkbookServiceDep
) -> WorkbookRead:
    return await service.create(caller, payload)


@router.get("/{sheet_id}", response_model=WorkbookRead)
async def get_sheet(
    sheet_id: uuid.UUID, caller: CurrentUserDep, service: WorkbookServiceDep
) -> WorkbookRead:
    return await service.get(caller, sheet_id)


@router.patch("/{sheet_id}", response_model=WorkbookRead)
async def update_sheet(
    sheet_id: uuid.UUID,
    payload: WorkbookUpdate,
    caller: CurrentUserDep,
    service: WorkbookServiceDep,
) -> WorkbookRead:
    return await service.update(caller, sheet_id, payload)


@router.put("/{sheet_id}/content", response_model=WorkbookSaved)
async def save_sheet(
    sheet_id: uuid.UUID,
    request: Request,
    caller: CurrentUserDep,
    service: WorkbookServiceDep,
) -> WorkbookSaved:
    body = await read_capped_body(request, MAX_SNAPSHOT_BYTES + _SAVE_ENVELOPE_BYTES)
    try:
        payload = WorkbookContentSave.model_validate_json(body)
    except pydantic.ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    return await service.save(caller, sheet_id, payload)


@router.delete("/{sheet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sheet(
    sheet_id: uuid.UUID, caller: CurrentUserDep, service: WorkbookServiceDep
) -> None:
    await service.delete(caller, sheet_id)


@router.get("/{sheet_id}/members", response_model=list[WorkbookMemberRead])
async def list_members(
    sheet_id: uuid.UUID, caller: CurrentUserDep, service: WorkbookServiceDep
) -> list[WorkbookMemberRead]:
    return await service.members(caller, sheet_id)


@router.put("/{sheet_id}/members/{employee_id}", response_model=list[WorkbookMemberRead])
async def set_member(
    sheet_id: uuid.UUID,
    employee_id: uuid.UUID,
    payload: WorkbookMemberSet,
    caller: CurrentUserDep,
    service: WorkbookServiceDep,
) -> list[WorkbookMemberRead]:
    """Share with someone, or change their access. Returns everyone with access."""
    return await service.set_member(caller, sheet_id, employee_id, payload.access)


@router.delete("/{sheet_id}/members/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    sheet_id: uuid.UUID,
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: WorkbookServiceDep,
) -> None:
    await service.remove_member(caller, sheet_id, employee_id)
