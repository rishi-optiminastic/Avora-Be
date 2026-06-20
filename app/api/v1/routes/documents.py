"""Employee document endpoints — HR/Admin manage; the person reads their own.

Authorization lives in `DocumentService` (HR/Admin or self for reads; HR/Admin
only for writes). Out-of-scope reads return 403.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import CurrentUserDep, DocumentServiceDep
from app.schemas.document import DocumentCreate, DocumentRead

router = APIRouter(prefix="/employees", tags=["documents"])


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
