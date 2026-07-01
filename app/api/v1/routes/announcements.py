"""Announcement endpoints.

`GET` is open to every authenticated employee (the dashboard bar). `POST` and
`DELETE` are HR/Admin only — authorization lives in `AnnouncementService`, so an
out-of-scope caller gets 403.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import AnnouncementServiceDep, CurrentUserDep
from app.schemas.announcement import AnnouncementCreate, AnnouncementRead

router = APIRouter(prefix="/announcements", tags=["announcements"])


@router.get("", response_model=list[AnnouncementRead])
async def list_announcements(
    caller: CurrentUserDep, service: AnnouncementServiceDep
) -> list[AnnouncementRead]:
    """The current announcement bar — HR posts plus derived holiday notices."""
    return await service.list_current(caller)


@router.post("", response_model=AnnouncementRead, status_code=status.HTTP_201_CREATED)
async def create_announcement(
    payload: AnnouncementCreate, caller: CurrentUserDep, service: AnnouncementServiceDep
) -> AnnouncementRead:
    return await service.create(caller, payload)


@router.delete("/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_announcement(
    announcement_id: uuid.UUID, caller: CurrentUserDep, service: AnnouncementServiceDep
) -> None:
    await service.deactivate(caller, announcement_id)
