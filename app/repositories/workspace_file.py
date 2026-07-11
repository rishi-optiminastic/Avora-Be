"""Workspace file data access — the only place these queries are built.

List/get queries `defer` the (large) `content` column so metadata never drags
file bytes over the wire; the bytes are loaded only by `get` for download. List
rows are joined to `work_entities` and `employees` so the API can show the
project and uploader names without an N+1.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy import Row, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models.employee import Employee
from app.models.work_entity import WorkEntity
from app.models.workspace_file import WorkspaceFile, WorkspaceFileCategory, WorkspaceVisibility


class FileStats(NamedTuple):
    total_files: int
    total_bytes: int
    project_count: int
    by_category: dict[str, int]


# A workspace file plus its joined-in project + uploader names. The names are
# typed `str` by the ORM but are NULL at runtime when the outer join misses
# (no project / a soft-deleted uploader) — callers treat them as optional.
type FileRow = Row[tuple[WorkspaceFile, str, str]]


class WorkspaceFileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _named(self) -> Select[tuple[WorkspaceFile, str, str]]:
        return (
            select(WorkspaceFile, WorkEntity.name, Employee.full_name)
            .outerjoin(WorkEntity, WorkspaceFile.project_id == WorkEntity.id)
            .outerjoin(Employee, WorkspaceFile.uploaded_by == Employee.id)
            .options(defer(WorkspaceFile.content))
        )

    async def add(
        self,
        *,
        name: str,
        description: str | None,
        category: WorkspaceFileCategory,
        content_type: str,
        byte_size: int,
        original_filename: str | None,
        url: str | None,
        visibility: WorkspaceVisibility,
        visible_departments: list[str],
        visible_employee_ids: list[str],
        project_id: uuid.UUID | None,
        uploaded_by: uuid.UUID,
        object_key: str | None,
        content: bytes | None,
    ) -> WorkspaceFile:
        file = WorkspaceFile(
            name=name,
            description=description,
            category=category,
            content_type=content_type,
            byte_size=byte_size,
            original_filename=original_filename,
            url=url,
            visibility=visibility,
            visible_departments=visible_departments,
            visible_employee_ids=visible_employee_ids,
            project_id=project_id,
            uploaded_by=uploaded_by,
            object_key=object_key,
            content=content,
        )
        self._session.add(file)
        await self._session.flush()
        return file

    async def get(self, file_id: uuid.UUID) -> WorkspaceFile | None:
        """Single file *with* its bytes — used by the download endpoint only."""
        return await self._session.get(WorkspaceFile, file_id)

    async def get_named(self, file_id: uuid.UUID) -> FileRow | None:
        result = await self._session.execute(self._named().where(WorkspaceFile.id == file_id))
        return result.first()

    async def list(
        self,
        *,
        project_id: uuid.UUID | None,
        category: WorkspaceFileCategory | None,
        search: str | None,
        limit: int,
    ) -> Sequence[FileRow]:
        stmt = self._named()
        if project_id is not None:
            stmt = stmt.where(WorkspaceFile.project_id == project_id)
        if category is not None:
            stmt = stmt.where(WorkspaceFile.category == category)
        if search:
            stmt = stmt.where(WorkspaceFile.name.ilike(f"%{search.strip()}%"))
        stmt = stmt.order_by(WorkspaceFile.created_at.desc()).limit(limit)
        result = await self._session.execute(stmt)
        return result.all()

    async def stats(self) -> FileStats:
        totals = await self._session.execute(
            select(
                func.count(WorkspaceFile.id),
                func.coalesce(func.sum(WorkspaceFile.byte_size), 0),
                func.count(func.distinct(WorkspaceFile.project_id)),
            )
        )
        total_files, total_bytes, project_count = totals.one()
        cat_rows = await self._session.execute(
            select(WorkspaceFile.category, func.count(WorkspaceFile.id)).group_by(
                WorkspaceFile.category
            )
        )
        by_category = {cat.value: count for cat, count in cat_rows.all()}
        return FileStats(
            total_files=int(total_files),
            total_bytes=int(total_bytes),
            project_count=int(project_count),
            by_category=by_category,
        )

    async def delete(self, file: WorkspaceFile) -> None:
        await self._session.delete(file)
        await self._session.flush()
