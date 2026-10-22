"""Sheets request/response schemas (Golden rule #5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.workbook import WorkbookAccess, WorkbookSharing

TITLE_MAX = 120


def _clean_title(value: str) -> str:
    title = " ".join(value.split())
    if not title:
        raise ValueError("Give the sheet a name.")
    return title


class WorkbookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    sharing: WorkbookSharing = WorkbookSharing.PRIVATE

    _title = field_validator("title")(_clean_title)


class WorkbookUpdate(BaseModel):
    """Rename or reshare. Owner or admin only."""

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    sharing: WorkbookSharing | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        return None if value is None else _clean_title(value)


class WorkbookContentSave(BaseModel):
    """A save from the editor. `version` is the one the editor loaded or last
    saved; a stale one is refused unless `force` is set after the person chose
    to overwrite."""

    version: int = Field(ge=1)
    snapshot: dict[str, Any]
    force: bool = False


# What the caller may do with a sheet, as one word for the UI.
MyAccess = Literal["owner", "edit", "view"]


class WorkbookSummary(BaseModel):
    id: uuid.UUID
    title: str
    sharing: WorkbookSharing
    owner_id: uuid.UUID
    owner_name: str | None
    version: int
    updated_at: datetime
    updated_by_name: str | None
    my_access: MyAccess
    can_edit: bool
    # Add, change and remove named people (owner, admins and editors).
    can_share: bool
    # Rename, change general access and delete (owner and admins).
    can_manage: bool
    # Names of the people it is shared with by name, oldest first.
    shared_with: list[str]


class WorkbookRead(WorkbookSummary):
    snapshot: dict[str, Any]


class WorkbookSaved(BaseModel):
    version: int
    updated_at: datetime
    updated_by_name: str | None


class WorkbookMemberSet(BaseModel):
    access: WorkbookAccess


class WorkbookMemberRead(BaseModel):
    employee_id: uuid.UUID
    name: str
    job_title: str | None
    access: WorkbookAccess


class WorkbookPerson(BaseModel):
    """Someone the share picker can offer."""

    id: uuid.UUID
    name: str
    job_title: str | None
    department: str | None


PEOPLE_QUERY_MAX = 64
