"""Workspace file endpoints — a shared team drive.

Uploads stream the raw bytes in the request body (metadata rides in the query +
`Content-Type`/`X-Filename` headers), exactly like the screenshot endpoint, so
the API needs no multipart parser. Every call is authenticated as a human; the
service authorizes (org-wide read; uploader/HR/Admin delete) and audits.
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
    DownloadRateLimitDep,
    UploadRateLimitDep,
    WorkspaceFileServiceDep,
)
from app.core.exceptions import NotFoundError
from app.core.http import read_capped_body
from app.models.workspace_file import WorkspaceFileCategory
from app.schemas.workspace_file import (
    WorkspaceFileMeta,
    WorkspaceFileRead,
    WorkspaceFileStats,
)
from app.services.workspace_file_service import MAX_FILE_BYTES

router = APIRouter(prefix="/workspace/files", tags=["workspace-files"])

_UNSAFE = re.compile(r'[\r\n"]')


def _safe_filename(name: str | None) -> str:
    """A header-safe download filename (strip CR/LF/quotes; cap length)."""
    cleaned = _UNSAFE.sub("", name or "").strip()
    return cleaned[:200] or "download"


@router.post("", response_model=WorkspaceFileRead, status_code=status.HTTP_201_CREATED)
async def upload_file(
    request: Request,
    caller: UploadRateLimitDep,
    service: WorkspaceFileServiceDep,
    name: Annotated[str, Query(min_length=1, max_length=200)],
    category: Annotated[WorkspaceFileCategory, Query()] = WorkspaceFileCategory.OTHER,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    description: Annotated[str | None, Query(max_length=1000)] = None,
    filename: Annotated[str | None, Query(max_length=255)] = None,
    content_type: Annotated[str, Header()] = "application/octet-stream",
) -> WorkspaceFileRead:
    # Bytes ride in the body; metadata in the query + Content-Type header. The
    # filename is a query param (not a header) so the browser upload needs no
    # custom CORS-allowlisted request header. The body is read with a hard cap so
    # an oversized upload is refused before it can buffer into memory (rule 5.6).
    data = await read_capped_body(request, MAX_FILE_BYTES)
    meta = WorkspaceFileMeta(
        name=name, description=description, category=category, project_id=project_id
    )
    return await service.upload(caller, meta, data, filename=filename, content_type=content_type)


@router.get("", response_model=list[WorkspaceFileRead])
async def list_files(
    caller: CurrentUserDep,
    service: WorkspaceFileServiceDep,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    category: Annotated[WorkspaceFileCategory | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[WorkspaceFileRead]:
    return await service.list(
        caller, project_id=project_id, category=category, search=q, limit=limit
    )


@router.get("/stats", response_model=WorkspaceFileStats)
async def file_stats(
    caller: CurrentUserDep,
    service: WorkspaceFileServiceDep,
) -> WorkspaceFileStats:
    return await service.stats(caller)


@router.get("/{file_id}/download")
async def download_file(
    file_id: uuid.UUID,
    caller: DownloadRateLimitDep,
    service: WorkspaceFileServiceDep,
) -> Response:
    """The file bytes, scoped to any signed-in caller (404 if it doesn't exist).

    Always served as an attachment with a neutral content type — so a user-uploaded
    `*.html`/`svg` can never run script inline. S3-backed files redirect to a
    short-lived presigned URL that forces the same disposition; DB-fallback files
    stream their bytes with it set directly.
    """
    file = await service.get_for_download(caller, file_id)
    safe = _safe_filename(file.original_filename or file.name)
    if file.object_key:
        url = storage.presigned_get_url(file.object_key, download_filename=safe)
        return RedirectResponse(url, status_code=307)
    if file.content is not None:
        return Response(
            content=file.content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )
    raise NotFoundError()


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: uuid.UUID,
    caller: CurrentUserDep,
    service: WorkspaceFileServiceDep,
) -> None:
    await service.delete(caller, file_id)
