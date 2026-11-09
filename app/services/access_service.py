"""Access business rules - the catalog, who holds what, and the reveal.

Visibility is decided in ONE place, `AccessViewer`, so every read applies the
same rule:

* Everyone sees the catalog (names, kinds, categories, owners) and their own
  grants - they need to know what exists to ask for it.
* Who holds an asset is shown within the caller's employee scope: HR and
  admins see everyone, a manager their reporting tree, everyone else only
  themselves (the same scope attendance and leave use).
* Cost, login id and billing details are shown to HR and admins, to holders,
  and to a manager whose team holds the asset.
* The secret is revealed only to a live holder or an admin, one asset at a
  time, and every reveal is audited.

HR and admins add, edit, grant and revoke. Only admins archive and rotate
secrets, and only admins can reveal a secret they do not hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.access_crypto import decrypt_secret, encrypt_secret
from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.models.access import AccessGrant, TechAsset
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind
from app.repositories.access import AccessRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.access import (
    GrantCreate,
    HolderRead,
    MyGrantRead,
    SecretRevealRead,
    TechAssetCreate,
    TechAssetRead,
    TechAssetUpdate,
)
from app.schemas.auth import CurrentUser
from app.services.notification_service import NotificationService

ACCESS_LINK = "/dashboard/workspace/access"


def is_admin(caller: CurrentUser) -> bool:
    # IT_ADMIN is normalized to ADMIN on the caller; both listed for clarity.
    return caller.role in (Role.ADMIN, Role.IT_ADMIN)


def can_grant(caller: CurrentUser) -> bool:
    return is_admin(caller) or caller.role is Role.HR


@dataclass(frozen=True)
class AccessViewer:
    """What one caller may see. `visible` is None for org-wide callers."""

    caller: CurrentUser
    visible: frozenset[uuid.UUID] | None
    team: frozenset[uuid.UUID]  # people in scope other than the caller

    @property
    def org_wide(self) -> bool:
        return self.visible is None

    def sees(self, employee_id: uuid.UUID) -> bool:
        return self.visible is None or employee_id in self.visible

    def sees_money(self, holders: Sequence[AccessGrant]) -> bool:
        if self.org_wide:
            return True
        return any(
            g.employee_id == self.caller.employee_id or g.employee_id in self.team for g in holders
        )


async def viewer_for(employees: EmployeeRepository, caller: CurrentUser) -> AccessViewer:
    if can_grant(caller):
        return AccessViewer(caller=caller, visible=None, team=frozenset())
    scope = frozenset(e.id for e in await employees.all_in_scope(caller)) | {caller.employee_id}
    return AccessViewer(caller=caller, visible=scope, team=scope - {caller.employee_id})


class AccessService:
    def __init__(
        self,
        access: AccessRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        notifications: NotificationService,
        settings: Settings,
    ) -> None:
        self._access = access
        self._employees = employees
        self._audit = audit
        self._notifications = notifications
        self._settings = settings

    # ---- reads ----------------------------------------------------------- #
    async def catalog(
        self, caller: CurrentUser, *, include_archived: bool = False
    ) -> list[TechAssetRead]:
        if include_archived and not is_admin(caller):
            include_archived = False
        assets = await self._access.list_assets(include_archived=include_archived)
        return await self._views(caller, assets)

    async def asset(self, caller: CurrentUser, asset_id: uuid.UUID) -> TechAssetRead:
        asset = await self._access.get_asset(asset_id)
        if asset is None or (asset.archived_at is not None and not is_admin(caller)):
            raise NotFoundError()
        return (await self._views(caller, [asset]))[0]

    async def my_grants(self, caller: CurrentUser) -> list[MyGrantRead]:
        grants = await self._access.live_grants_for_employee(caller.employee_id)
        assets = await self._access.assets_by_id([g.asset_id for g in grants])
        live = [a for a in assets.values() if a.archived_at is None]
        views = {v.id: v for v in await self._views(caller, live)}
        return [
            MyGrantRead(
                grant_id=g.id, level=g.level, granted_at=g.granted_at, asset=views[g.asset_id]
            )
            for g in grants
            if g.asset_id in views
        ]

    async def _views(self, caller: CurrentUser, assets: Sequence[TechAsset]) -> list[TechAssetRead]:
        """Build read shapes for many assets with a fixed number of queries."""
        viewer = await viewer_for(self._employees, caller)
        grants = await self._access.live_grants_for_assets([a.id for a in assets])
        parents = await self._access.assets_by_id(
            list({a.parent_id for a in assets if a.parent_id is not None})
        )
        people_ids = {g.employee_id for gs in grants.values() for g in gs}
        people_ids |= {a.owner_id for a in assets if a.owner_id is not None}
        people = await self._employees.get_many(list(people_ids))
        return [
            _view(
                a,
                grants.get(a.id, []),
                viewer,
                parents.get(a.parent_id) if a.parent_id is not None else None,
                people,
            )
            for a in assets
        ]

    # ---- HR / admin: assets ---------------------------------------------- #
    async def create(self, caller: CurrentUser, payload: TechAssetCreate) -> TechAssetRead:
        if not can_grant(caller):
            raise AuthorizationError()
        await self._check_links(None, payload.parent_id, payload.owner_id)
        fields = payload.model_dump(exclude={"secret"})
        asset = TechAsset(**fields)
        if payload.secret is not None:
            self._store_secret(asset, payload.secret, caller)
        self._access.add_asset(asset)
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id), action="access.asset.create", target=f"asset:{asset.id}"
        )
        return (await self._views(caller, [asset]))[0]

    async def update(
        self, caller: CurrentUser, asset_id: uuid.UUID, payload: TechAssetUpdate
    ) -> TechAssetRead:
        if not can_grant(caller):
            raise AuthorizationError()
        asset = await self._access.get_asset(asset_id)
        if asset is None:
            raise NotFoundError()
        changes = payload.model_dump(exclude_unset=True)
        archived = changes.pop("archived", None)
        if archived is not None and not is_admin(caller):
            raise AuthorizationError()  # archiving hides it from everyone: admins only
        if "parent_id" in changes or "owner_id" in changes:
            await self._check_links(asset.id, changes.get("parent_id"), changes.get("owner_id"))
        for key, value in changes.items():
            if key in ("name", "priority", "status", "details") and value is None:
                continue  # required columns: null means "leave it"
            setattr(asset, key, value)
        if archived is not None:
            asset.archived_at = datetime.now(UTC) if archived else None
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id), action="access.asset.update", target=f"asset:{asset.id}"
        )
        return (await self._views(caller, [asset]))[0]

    async def rotate_secret(
        self, caller: CurrentUser, asset_id: uuid.UUID, secret: str
    ) -> TechAssetRead:
        """Store a new password or key and tell every holder it changed."""
        if not is_admin(caller):
            raise AuthorizationError()
        asset = await self._access.get_asset(asset_id)
        if asset is None:
            raise NotFoundError()
        self._store_secret(asset, secret, caller)
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id), action="access.secret.rotate", target=f"asset:{asset.id}"
        )
        holders = (await self._access.live_grants_for_assets([asset.id])).get(asset.id, [])
        for grant in holders:
            await self._notifications.notify(
                recipient_id=grant.employee_id,
                kind=NotificationKind.SYSTEM,
                title=f"{asset.name} password changed",
                body="Reveal it again from My access before you next sign in.",
                link=ACCESS_LINK,
                entity_type="tech_asset",
                entity_id=asset.id,
                actor_id=caller.employee_id,
            )
        return (await self._views(caller, [asset]))[0]

    def _store_secret(self, asset: TechAsset, secret: str, caller: CurrentUser) -> None:
        asset.secret_ciphertext = encrypt_secret(self._settings, secret)
        asset.secret_rotated_at = datetime.now(UTC)
        asset.secret_rotated_by = caller.employee_id

    async def _check_links(
        self, asset_id: uuid.UUID | None, parent_id: uuid.UUID | None, owner_id: uuid.UUID | None
    ) -> None:
        if parent_id is not None:
            if parent_id == asset_id:
                raise ValidationError("An asset cannot sit under itself.")
            if await self._access.get_asset(parent_id) is None:
                raise ValidationError("That parent account does not exist.")
        if owner_id is not None and await self._employees.get(owner_id) is None:
            raise ValidationError("That owner is not an employee.")

    # ---- reveal ---------------------------------------------------------- #
    async def reveal(self, caller: CurrentUser, asset_id: uuid.UUID) -> SecretRevealRead:
        asset = await self._access.get_asset(asset_id)
        if asset is None or asset.archived_at is not None:
            raise NotFoundError()
        holds = await self._access.live_grant(asset.id, caller.employee_id) is not None
        if not (holds or is_admin(caller)):
            raise NotFoundError()  # do not confirm the asset has a secret
        if asset.secret_ciphertext is None:
            raise ValidationError("No password is stored for this yet.")
        secret = decrypt_secret(self._settings, asset.secret_ciphertext)
        await self._audit.append(
            actor=str(caller.employee_id), action="access.secret.reveal", target=f"asset:{asset.id}"
        )
        return SecretRevealRead(asset_id=asset.id, login_id=asset.login_id, secret=secret)

    # ---- HR / admin: grants ---------------------------------------------- #
    async def grant(self, caller: CurrentUser, payload: GrantCreate) -> AccessGrant:
        if not can_grant(caller):
            raise AuthorizationError()
        asset = await self._access.get_asset(payload.asset_id)
        if asset is None or asset.archived_at is not None:
            raise NotFoundError()
        person = await self._employees.get(payload.employee_id)
        if person is None or not person.is_active:
            raise ValidationError("That person is not an active employee.")
        grant = await self._access.grant(
            asset_id=asset.id,
            employee_id=person.id,
            level=payload.level,
            granted_by=caller.employee_id,
            note=payload.note,
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="access.grant",
            target=f"asset:{asset.id}:employee:{person.id}:{grant.level.value}",
        )
        await self._notifications.notify(
            recipient_id=person.id,
            kind=NotificationKind.SYSTEM,
            title=f"You now have access to {asset.name}",
            body=f"Level: {grant.level.value}",
            link=ACCESS_LINK,
            entity_type="tech_asset",
            entity_id=asset.id,
            actor_id=caller.employee_id,
        )
        return grant

    async def revoke(self, caller: CurrentUser, grant_id: uuid.UUID) -> None:
        if not can_grant(caller):
            raise AuthorizationError()
        grant = await self._access.get_grant(grant_id)
        if grant is None or grant.revoked_at is not None:
            raise NotFoundError()
        grant.revoked_at = datetime.now(UTC)
        grant.revoked_by = caller.employee_id
        await self._access.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="access.revoke",
            target=f"asset:{grant.asset_id}:employee:{grant.employee_id}",
        )


def _view(
    asset: TechAsset,
    grants: Sequence[AccessGrant],
    viewer: AccessViewer,
    parent: TechAsset | None,
    people: dict[uuid.UUID, Employee],
) -> TechAssetRead:
    mine = next((g for g in grants if g.employee_id == viewer.caller.employee_id), None)
    money = viewer.sees_money(grants)
    detail = money or mine is not None
    owner = people.get(asset.owner_id) if asset.owner_id is not None else None
    holders = [_holder(g, people.get(g.employee_id)) for g in grants if viewer.sees(g.employee_id)]
    return TechAssetRead(
        id=asset.id,
        kind=asset.kind,
        name=asset.name,
        category=asset.category,
        parent_id=asset.parent_id,
        parent_name=parent.name if parent is not None else None,
        owner_id=asset.owner_id,
        owner_name=owner.full_name if owner is not None else None,
        login_url=asset.login_url,
        priority=asset.priority,
        status=asset.status,
        two_factor_method=asset.two_factor_method,
        has_secret=asset.has_secret,
        secret_rotated_at=asset.secret_rotated_at,
        renews_on=asset.renews_on,
        archived=asset.archived_at is not None,
        holder_count=len(grants),
        holders=holders,
        my_level=mine.level if mine is not None else None,
        details_visible=detail,
        login_id=asset.login_id if detail else None,
        details=dict(asset.details or {}) if detail else None,
        notes=asset.notes if detail else None,
        cost_visible=money,
        cost_minor=asset.cost_minor if money else None,
        currency=asset.currency if money else None,
        billing_cycle=asset.billing_cycle if money else None,
    )


def _holder(grant: AccessGrant, person: Employee | None) -> HolderRead:
    return HolderRead(
        grant_id=grant.id,
        employee_id=grant.employee_id,
        name=person.full_name if person is not None else "Former employee",
        department=person.department if person is not None else None,
        level=grant.level,
        granted_at=grant.granted_at,
    )
