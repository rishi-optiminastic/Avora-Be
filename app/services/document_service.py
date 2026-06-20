"""Employee document business rules — same visibility tier as compensation.

Read: HR/Admin or the person themselves. Write/delete: HR/Admin only. Every
action is audited (Security rule 5.7).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.document import EmployeeDocument
from app.models.employee import Role
from app.repositories.audit import AuditRepository
from app.repositories.document import DocumentRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.document import DocumentCreate


def _can_manage(caller: CurrentUser) -> bool:
    return caller.role in (Role.ADMIN, Role.HR)


class DocumentService:
    def __init__(
        self,
        documents: DocumentRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._documents = documents
        self._employees = employees
        self._audit = audit

    def _assert_can_view(self, caller: CurrentUser, employee_id: uuid.UUID) -> None:
        if not _can_manage(caller) and caller.employee_id != employee_id:
            raise AuthorizationError()

    async def list(self, caller: CurrentUser, employee_id: uuid.UUID) -> Sequence[EmployeeDocument]:
        self._assert_can_view(caller, employee_id)
        documents = await self._documents.list_for_employee(employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.list",
            target=f"employee:{employee_id}",
        )
        return documents

    async def add(
        self, caller: CurrentUser, employee_id: uuid.UUID, data: DocumentCreate
    ) -> EmployeeDocument:
        if not _can_manage(caller):
            raise AuthorizationError()
        employee = await self._employees.get(employee_id)
        if employee is None or not employee.is_active:
            raise NotFoundError()
        document = await self._documents.add(employee_id, data, uploaded_by=caller.employee_id)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.add",
            target=f"employee:{employee_id}",
        )
        return document

    async def delete(
        self, caller: CurrentUser, employee_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        if not _can_manage(caller):
            raise AuthorizationError()
        document = await self._documents.get(document_id)
        if document is None or document.employee_id != employee_id:
            raise NotFoundError()
        await self._documents.delete(document)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="document.delete",
            target=f"employee:{employee_id}",
        )
