"""Access by person - who has what, including what comes through an account.

The catalog answers "who holds Vercel"; this answers "what can Insha get
into". Both read the same reach rule (`core/access_reach.py`), so a person's
list and a tool's holders never disagree.

Scope is the same as everywhere in Access: HR and admins see everyone, a
manager their reporting tree, everyone else only themselves. Only HR and admins
may revoke, and "revoke all" removes a person's direct grants - which, for an
account, also ends everything inherited through it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.access_reach import Reach, reach_by_person
from app.core.exceptions import AuthorizationError, NotFoundError
from app.models.access import AccessGrant, AssetPriority, TechAsset
from app.models.employee import Employee
from app.repositories.access import AccessRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.access import PersonAccessItemRead, PersonAccessRead, RevokeAllResult
from app.schemas.auth import CurrentUser
from app.services.access_service import can_grant, viewer_for

# Only what a logo needs; never billing, recovery or card details.
_LOGO_KEYS = ("website", "brand", "carrier", "manage_url")


class AccessPeopleService:
    def __init__(
        self, access: AccessRepository, employees: EmployeeRepository, audit: AuditRepository
    ) -> None:
        self._access = access
        self._employees = employees
        self._audit = audit

    async def people(self, caller: CurrentUser) -> list[PersonAccessRead]:
        viewer = await viewer_for(self._employees, caller)
        if viewer.org_wide:
            staff = list(await self._employees.all_in_scope(caller))
        else:
            visible = await self._employees.get_many(sorted(viewer.visible or {caller.employee_id}))
            staff = [e for e in visible.values() if e.is_active]
        reach, assets = await self._reach()
        rows = [_person(e, reach.get(e.id, []), assets) for e in staff]
        return sorted(rows, key=lambda r: (-r.reach_count, r.name.lower()))

    async def person(self, caller: CurrentUser, employee_id: uuid.UUID) -> PersonAccessRead:
        viewer = await viewer_for(self._employees, caller)
        person = await self._employees.get(employee_id)
        if person is None or not viewer.sees(employee_id):
            raise NotFoundError()
        reach, assets = await self._reach()
        return _person(person, reach.get(person.id, []), assets)

    async def revoke_all(self, caller: CurrentUser, employee_id: uuid.UUID) -> RevokeAllResult:
        """Offboarding in one step: end every direct grant this person holds."""
        if not can_grant(caller):
            raise AuthorizationError()
        if await self._employees.get(employee_id) is None:
            raise NotFoundError()
        grants = await self._access.live_grants_for_employee(employee_id)
        now = datetime.now(UTC)
        for grant in grants:
            grant.revoked_at = now
            grant.revoked_by = caller.employee_id
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="access.revoke_all",
            target=f"employee:{employee_id}:{len(grants)}",
        )
        return RevokeAllResult(revoked=len(grants))

    async def _reach(self) -> tuple[dict[uuid.UUID, list[Reach]], dict[uuid.UUID, TechAsset]]:
        everything = await self._access.list_assets(include_archived=True)
        assets = {a.id: a for a in everything}
        live = [a.id for a in everything if a.archived_at is None]
        grants: dict[uuid.UUID, list[AccessGrant]] = await self._access.live_grants_for_assets(
            list(assets)
        )
        parent_of = {a.id: a.parent_id for a in everything}
        return reach_by_person(live, parent_of, grants), assets


def _person(
    person: Employee, reach: list[Reach], assets: dict[uuid.UUID, TechAsset]
) -> PersonAccessRead:
    items = [_item(r, assets) for r in reach if r.asset_id in assets]
    items.sort(key=lambda i: (i.via_name is not None, i.via_name or "", i.name.lower()))
    return PersonAccessRead(
        employee_id=person.id,
        name=person.full_name,
        department=person.department,
        job_title=person.job_title,
        direct_count=sum(1 for i in items if i.via_asset_id is None),
        reach_count=len(items),
        critical_count=sum(1 for r in reach if _critical(assets.get(r.asset_id))),
        items=items,
    )


def _critical(asset: TechAsset | None) -> bool:
    return asset is not None and asset.priority is AssetPriority.CRITICAL


def _item(reach: Reach, assets: dict[uuid.UUID, TechAsset]) -> PersonAccessItemRead:
    asset = assets[reach.asset_id]
    via = assets.get(reach.via_asset_id) if reach.via_asset_id is not None else None
    details = asset.details or {}
    return PersonAccessItemRead(
        asset_id=asset.id,
        name=asset.name,
        kind=asset.kind,
        category=asset.category,
        priority=asset.priority,
        status=asset.status,
        login_url=asset.login_url,
        logo_details={k: details[k] for k in _LOGO_KEYS if details.get(k) not in (None, "")},
        level=reach.grant.level,
        grant_id=reach.grant.id,
        granted_at=reach.grant.granted_at,
        via_asset_id=reach.via_asset_id,
        via_name=via.name if via is not None else None,
    )
