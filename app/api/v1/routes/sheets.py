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
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, PageParams
from app.schemas.workbook import (
    WorkbookContentSave,
    WorkbookCreate,
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
) -> Page[WorkbookSummary]:
    return await service.list(caller, PageParams(page=page, size=size))


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
