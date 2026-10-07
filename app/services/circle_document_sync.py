"""Copy each employee's Circle documents into Avora, once, automatically.

Run by `worker/circle_documents_scheduler.py`. Copies are ordinary Avora
documents (same storage, same access rules: HR/Admin or the person). The
ledger (`circle_document_imports`) makes every Circle file copy exactly once:
deleting a copy in Avora does not bring it back, and nothing is ever deleted
or changed in Circle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core import storage
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.document import DocumentCategory
from app.repositories.audit import AuditRepository
from app.repositories.circle_document_import import CircleDocumentImportRepository
from app.repositories.document import DocumentRepository
from app.services.circle_client import CircleClient, is_valid_doc_id
from app.services.document_service import MAX_DOC_BYTES, store_document_bytes

logger = get_logger("app.circle_documents")

SYNC_ACTOR = "circle-sync"
_TITLE_MAX = 200

# Circle's free-text document categories (onboarding doc types) -> Avora's.
_CATEGORY_MAP: dict[str, DocumentCategory] = {
    "aadhaar card": DocumentCategory.IDENTITY,
    "pan card": DocumentCategory.IDENTITY,
    "passport photo": DocumentCategory.IDENTITY,
    "address proof": DocumentCategory.IDENTITY,
    "offer letter": DocumentCategory.CONTRACT,
    "appointment letter": DocumentCategory.CONTRACT,
    "signed offer letter": DocumentCategory.CONTRACT,
    "signed appointment letter": DocumentCategory.CONTRACT,
    "current offer letter": DocumentCategory.CONTRACT,
    "offer/appraisal letter": DocumentCategory.CONTRACT,
    "education certificates": DocumentCategory.CERTIFICATE,
    "experience letter": DocumentCategory.CERTIFICATE,
    "salary slips": DocumentCategory.PAYSLIP,
}


def map_category(circle_category: str | None) -> DocumentCategory:
    return _CATEGORY_MAP.get((circle_category or "").strip().lower(), DocumentCategory.OTHER)


def document_title(circle_category: str | None, file_name: str | None) -> str:
    """Circle's label when it has one ("Aadhaar card"), else the file name."""
    label = (circle_category or "").strip()
    if not label or label.lower() == "document":
        label = (file_name or "").strip() or "Document from Circle"
    return label[:_TITLE_MAX]


@dataclass(frozen=True)
class CopyOutcome:
    copied: bool
    object_key: str | None = None


class CircleDocumentSync:
    """One Circle document at a time, so the caller can commit each copy on its
    own: a single bad file then never holds back the rest."""

    def __init__(
        self,
        documents: DocumentRepository,
        imports: CircleDocumentImportRepository,
        audit: AuditRepository,
        settings: Settings,
        client: CircleClient,
    ) -> None:
        self._documents = documents
        self._imports = imports
        self._audit = audit
        self._settings = settings
        self._client = client

    async def pending(self, employee_id: uuid.UUID, work_email: str) -> list[dict[str, Any]]:
        """This person's Circle documents not yet copied (or skipped). Circle
        being unreachable raises; a person Circle does not know has none."""
        try:
            listing = await self._client.documents(work_email)
        except NotFoundError:
            return []
        done = await self._imports.copied_ids(employee_id)
        return [d for d in listing if str(d.get("id") or "") and str(d["id"]) not in done]

    async def copy_one(
        self, employee_id: uuid.UUID, work_email: str, entry: dict[str, Any]
    ) -> CopyOutcome:
        """Copy one document. A file that cannot be copied - gone from Circle,
        an unusable id, empty or too large - is recorded as skipped, so it is
        not fetched again every run. The caller commits; if that commit fails
        it must delete `outcome.object_key` (see `discard_stored`)."""
        circle_id = str(entry["id"])
        if not is_valid_doc_id(circle_id):
            await self._skip(employee_id, circle_id)
            return CopyOutcome(copied=False)
        try:
            file = await self._client.document_content(
                work_email, circle_id, max_bytes=MAX_DOC_BYTES
            )
        except NotFoundError:
            await self._skip(employee_id, circle_id)  # deleted in Circle since listing
            return CopyOutcome(copied=False)
        if not file.content or file.too_large:
            await self._skip(employee_id, circle_id)
            return CopyOutcome(copied=False)
        file_name = str(entry.get("file_name") or "") or None
        media_type, object_key, content = await store_document_bytes(
            self._settings, file.content, filename=file_name, content_type=file.content_type
        )
        try:
            await self._save(
                employee_id, circle_id, entry, file.content, (media_type, object_key, content)
            )
        except Exception:
            # The bytes reached storage but the record did not: remove them, or
            # every retry would leave another orphaned object behind.
            await discard_stored(object_key)
            raise
        return CopyOutcome(copied=True, object_key=object_key)

    async def _skip(self, employee_id: uuid.UUID, circle_id: str) -> None:
        await self._imports.record(employee_id, circle_id[:64], None)
        await self._audit.append(
            actor=SYNC_ACTOR,
            action="document.circle_skip",
            target=f"employee:{employee_id}",
        )

    async def _save(
        self,
        employee_id: uuid.UUID,
        circle_id: str,
        entry: dict[str, Any],
        data: bytes,
        stored: tuple[str, str | None, bytes | None],
    ) -> None:
        media_type, object_key, content = stored
        category = str(entry.get("category") or "") or None
        file_name = str(entry.get("file_name") or "") or None
        document = await self._documents.add_file(
            employee_id,
            title=document_title(category, file_name),
            category=map_category(category),
            content_type=media_type,
            byte_size=len(data),
            original_filename=file_name,
            object_key=object_key,
            content=content,
            uploaded_by=None,
        )
        await self._imports.record(employee_id, circle_id, document.id)
        await self._audit.append(
            actor=SYNC_ACTOR,
            action="document.circle_copy",
            target=f"employee:{employee_id}:document:{document.id}",
        )


async def discard_stored(object_key: str | None) -> None:
    """Best-effort removal of bytes whose database record was not saved."""
    if not object_key:
        return
    try:
        await storage.delete_objects([object_key])
    except (ClientError, BotoCoreError):
        logger.warning("circle_document_orphan_cleanup_failed", extra={"key": object_key})
