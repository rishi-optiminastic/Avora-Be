"""Access requests - someone asks for a tool, one approval grants it.

* **Anyone may ask** for any live asset in the catalog, at a level, with a
  reason. Asking for what you already hold at that level or stronger is
  refused; asking for a stronger level is an upgrade.
* **One approval is enough**, from the requester's manager (anyone above them
  in the reporting line), HR or an admin. Never your own.
* **Approval grants at once.** The grant is written in the same transaction as
  the decision. If no password is stored yet, the asset's owner is told to
  share the login, since Avora cannot hand it over itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.access import (
    AccessLevel,
    AccessRequest,
    AccessRequestStatus,
    TechAsset,
)
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind, NotificationLevel
from app.repositories.access import AccessRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.access import AccessRequestCreate, AccessRequestRead, AccessRequestReview
from app.schemas.auth import CurrentUser
from app.services.access_service import ACCESS_LINK, AccessViewer, viewer_for
from app.services.notification_service import NotificationService

_REQUESTS_LINK = f"{ACCESS_LINK}/requests"
_LIST_LIMIT = 200
# Strongest first; a request is only new if it asks for more than is held.
_RANK = {AccessLevel.OWNER: 4, AccessLevel.ADMIN: 3, AccessLevel.EDITOR: 2, AccessLevel.VIEWER: 1}


class AccessRequestService:
    def __init__(
        self,
        access: AccessRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        notifications: NotificationService,
    ) -> None:
        self._access = access
        self._employees = employees
        self._audit = audit
        self._notifications = notifications

    async def request(self, caller: CurrentUser, payload: AccessRequestCreate) -> AccessRequestRead:
        asset = await self._access.get_asset(payload.asset_id)
        if asset is None or asset.archived_at is not None:
            raise NotFoundError()
        held = await self._access.live_grant(asset.id, caller.employee_id)
        if held is not None and _RANK[held.level] >= _RANK[payload.level]:
            raise ValidationError(f"You already have {held.level.value} access to {asset.name}.")
        if await self._access.pending_request(asset.id, caller.employee_id) is not None:
            raise ValidationError(f"You already have a request waiting for {asset.name}.")
        entry = AccessRequest(
            asset_id=asset.id,
            employee_id=caller.employee_id,
            level=payload.level,
            reason=payload.reason.strip(),
        )
        self._access.add_request(entry)
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="access.request",
            target=f"access_request:{entry.id}:asset:{asset.id}",
        )
        await self._tell_reviewers(caller, entry, asset)
        return (await self._reads(caller, [entry]))[0]

    async def list_for_caller(self, caller: CurrentUser) -> list[AccessRequestRead]:
        viewer = await viewer_for(self._employees, caller)
        pending_from = None if viewer.org_wide else sorted(viewer.team)
        rows = await self._access.requests_for(
            mine=caller.employee_id, pending_from=pending_from, limit=_LIST_LIMIT
        )
        return await self._reads(caller, rows, viewer)

    async def cancel(self, caller: CurrentUser, request_id: uuid.UUID) -> AccessRequestRead:
        entry = await self._access.get_request(request_id)
        if entry is None or entry.employee_id != caller.employee_id:
            raise NotFoundError()
        if entry.status is not AccessRequestStatus.PENDING:
            raise ValidationError("That request was already decided.")
        entry.status = AccessRequestStatus.CANCELLED
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="access.request.cancelled",
            target=f"access_request:{entry.id}",
        )
        return (await self._reads(caller, [entry]))[0]

    async def review(
        self, caller: CurrentUser, request_id: uuid.UUID, payload: AccessRequestReview
    ) -> AccessRequestRead:
        entry = await self._access.get_request(request_id)
        viewer = await viewer_for(self._employees, caller)
        if entry is None or not viewer.sees(entry.employee_id):
            raise NotFoundError()
        if not _may_decide(viewer, entry):
            raise AuthorizationError()
        if entry.status is not AccessRequestStatus.PENDING:
            raise ValidationError("That request was already decided.")
        asset = await self._access.get_asset(entry.asset_id)
        if asset is None:
            raise NotFoundError()

        entry.status = (
            AccessRequestStatus.APPROVED if payload.approve else AccessRequestStatus.REJECTED
        )
        entry.reviewed_by_id = caller.employee_id
        entry.review_note = payload.review_note.strip() if payload.review_note else None
        entry.decided_at = datetime.now(UTC)
        if payload.approve:
            await self._access.grant(
                asset_id=asset.id,
                employee_id=entry.employee_id,
                level=entry.level,
                granted_by=caller.employee_id,
                request_id=entry.id,
            )
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action=f"access.request.{entry.status.value}",
            target=f"access_request:{entry.id}:asset:{asset.id}",
        )
        await self._notify_decision(caller, entry, asset)
        return (await self._reads(caller, [entry], viewer))[0]

    # ---- notifications --------------------------------------------------- #
    async def _tell_reviewers(
        self, caller: CurrentUser, entry: AccessRequest, asset: TechAsset
    ) -> None:
        requester = await self._employees.get(caller.employee_id)
        name = requester.full_name if requester is not None else "Someone"
        for reviewer in await self._reviewers_for(caller):
            await self._notifications.notify(
                recipient_id=reviewer.id,
                kind=NotificationKind.SYSTEM,
                title=f"{name} asked for {asset.name}",
                body=f"{entry.level.value} access · {entry.reason}"[:200],
                link=_REQUESTS_LINK,
                entity_type="access_request",
                entity_id=entry.id,
                actor_id=caller.employee_id,
            )

    async def _reviewers_for(self, caller: CurrentUser) -> list[Employee]:
        """The direct manager, or HR plus admins when there isn't an active one,
        so a request never sits with nobody told about it."""
        if caller.manager_id is not None:
            manager = await self._employees.get(caller.manager_id)
            if manager is not None and manager.is_active:
                return [manager]
        fallback = [
            *await self._employees.list_by_role(Role.HR),
            *await self._employees.list_admins(),
        ]
        unique = {e.id: e for e in fallback if e.id != caller.employee_id}
        return list(unique.values())

    async def _notify_decision(
        self, caller: CurrentUser, entry: AccessRequest, asset: TechAsset
    ) -> None:
        approved = entry.status is AccessRequestStatus.APPROVED
        await self._notifications.notify(
            recipient_id=entry.employee_id,
            kind=NotificationKind.SYSTEM,
            level=NotificationLevel.INFO if approved else NotificationLevel.WARNING,
            title=f"{asset.name} request {'approved' if approved else 'rejected'}",
            body=(entry.review_note or f"{entry.level.value} access")[:200],
            link=ACCESS_LINK,
            entity_type="access_request",
            entity_id=entry.id,
            actor_id=caller.employee_id,
        )
        if not approved or asset.has_secret or asset.owner_id is None:
            return
        requester = await self._employees.get(entry.employee_id)
        await self._notifications.notify(
            recipient_id=asset.owner_id,
            kind=NotificationKind.SYSTEM,
            title=f"Share the {asset.name} login",
            body=f"{requester.full_name if requester else 'A teammate'} was approved for "
            f"{entry.level.value} access and no password is stored in Avora.",
            link=ACCESS_LINK,
            entity_type="tech_asset",
            entity_id=asset.id,
            actor_id=caller.employee_id,
        )

    # ---- read shapes ----------------------------------------------------- #
    async def _reads(
        self,
        caller: CurrentUser,
        rows: Sequence[AccessRequest],
        viewer: AccessViewer | None = None,
    ) -> list[AccessRequestRead]:
        viewer = viewer or await viewer_for(self._employees, caller)
        assets = await self._access.assets_by_id(list({r.asset_id for r in rows}))
        people_ids = {r.employee_id for r in rows}
        people_ids |= {r.reviewed_by_id for r in rows if r.reviewed_by_id is not None}
        people = await self._employees.get_many(list(people_ids))
        out: list[AccessRequestRead] = []
        for r in rows:
            asset = assets.get(r.asset_id)
            if asset is None:
                continue
            person = people.get(r.employee_id)
            reviewer = people.get(r.reviewed_by_id) if r.reviewed_by_id is not None else None
            out.append(
                AccessRequestRead(
                    id=r.id,
                    asset_id=r.asset_id,
                    asset_name=asset.name,
                    asset_kind=asset.kind,
                    employee_id=r.employee_id,
                    employee_name=person.full_name if person is not None else "Former employee",
                    level=r.level,
                    reason=r.reason,
                    status=r.status,
                    reviewed_by_id=r.reviewed_by_id,
                    reviewer_name=reviewer.full_name if reviewer is not None else None,
                    decided_at=r.decided_at,
                    review_note=r.review_note,
                    created_at=r.created_at,
                    can_decide=r.status is AccessRequestStatus.PENDING and _may_decide(viewer, r),
                )
            )
        return out


def _may_decide(viewer: AccessViewer, entry: AccessRequest) -> bool:
    """HR, admins, or anyone with the requester in their reporting scope.
    Never your own: approving yourself is handing yourself the keys."""
    if entry.employee_id == viewer.caller.employee_id:
        return False
    return viewer.org_wide or entry.employee_id in viewer.team
