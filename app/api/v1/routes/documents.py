"""Employee document endpoints — HR/Admin manage; the person reads their own.

Authorization lives in `DocumentService` (HR/Admin or self for reads; HR/Admin
only for writes). Out-of-scope reads return 403.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.core import storage
from app.core.deps import (
    CurrentUserDep,
    DocumentServiceDep,
    DownloadRateLimitDep,
    UploadRateLimitDep,
)
from app.core.exceptions import NotFoundError
from app.core.http import read_capped_body
from app.models.document import DocumentCategory
from app.schemas.document import DocumentCreate, DocumentFileMeta, DocumentRead
from app.services.document_service import MAX_DOC_BYTES

router = APIRouter(prefix="/employees", tags=["documents"])

_UNSAFE = re.compile(r'[\r\n"]')


def _safe_filename(name: str | None) -> str:
    cleaned = _UNSAFE.sub("", name or "").strip()
    return cleaned[:200] or "download"


@router.get("/{employee_id}/documents", response_model=list[DocumentRead])
async def list_documents(
    employee_id: uuid.UUID,
    caller: CurrentUserDep,
    service: DocumentServiceDep,
) -> list[DocumentRead]:
    documents = await service.list(caller, employee_id)
    return [DocumentRead.model_validate(d) for d in documents]


@router.post(
    "/{employee_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_document(
    employee_id: uuid.UUID,
    payload: DocumentCreate,
    caller: CurrentUserDep,
    service: DocumentServiceDep,
) -> DocumentRead:
    return DocumentRead.model_validate(await service.add(caller, employee_id, payload))


@router.post(
    "/{employee_id}/documents/upload",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    employee_id: uuid.UUID,
    request: Request,
    caller: UploadRateLimitDep,
    service: DocumentServiceDep,
    title: Annotated[str, Query(min_length=1, max_length=200)],
    category: Annotated[DocumentCategory, Query()] = DocumentCategory.OTHER,
    filename: Annotated[str | None, Query(max_length=255)] = None,
    content_type: Annotated[str, Header()] = "application/octet-stream",
) -> DocumentRead:
    """Upload a document file (HR/Admin). Bytes ride in the body; metadata in the
    query + Content-Type header — same shape as the workspace-file upload."""
    data = await read_capped_body(request, MAX_DOC_BYTES)
    meta = DocumentFileMeta(title=title, category=category)
    document = await service.add_file(
        caller, employee_id, meta, data, filename=filename, content_type=content_type
    )
    return DocumentRead.model_validate(document)


@router.get("/{employee_id}/documents/{document_id}/download")
async def download_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: DownloadRateLimitDep,
    service: DocumentServiceDep,
) -> Response:
    """An uploaded document's bytes, scoped to HR/Admin or the person themselves
    (404 otherwise). Always served as a neutral-type attachment."""
    document = await service.get_for_download(caller, employee_id, document_id)
    safe = _safe_filename(document.original_filename or document.title)
    if document.object_key:
        url = storage.presigned_get_url(document.object_key, download_filename=safe)
        return RedirectResponse(url, status_code=307)
    if document.content is not None:
        return Response(
            content=document.content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )
    raise NotFoundError()


@router.delete(
    "/{employee_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: CurrentUserDep,
    service: DocumentServiceDep,
) -> None:
    await service.delete(caller, employee_id, document_id)
