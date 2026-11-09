"""Access data access - assets, grants and requests. The only place their
queries are built.

No row-scope clause lives here: the service decides which holders and requests
a caller may see (it already holds the caller's visible employee set) and asks
for exactly those rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access import (
    AccessGrant,
    AccessLevel,
    AccessRequest,
    AccessRequestStatus,
    TechAsset,
    TechAssetKind,
)


class AccessRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- assets ---------------------------------------------------------- #
    def add_asset(self, asset: TechAsset) -> None:
        self._session.add(asset)

    async def get_asset(self, asset_id: uuid.UUID) -> TechAsset | None:
        return await self._session.get(TechAsset, asset_id)

    async def list_assets(
        self, *, kind: TechAssetKind | None = None, include_archived: bool = False
    ) -> Sequence[TechAsset]:
        stmt = select(TechAsset)
        if kind is not None:
            stmt = stmt.where(TechAsset.kind == kind)
        if not include_archived:
            stmt = stmt.where(TechAsset.archived_at.is_(None))
        rows = await self._session.execute(stmt.order_by(TechAsset.name))
        return rows.scalars().all()

    async def assets_by_id(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, TechAsset]:
        if not ids:
            return {}
        rows = await self._session.execute(select(TechAsset).where(TechAsset.id.in_(ids)))
        return {a.id: a for a in rows.scalars().all()}

    # ---- grants ---------------------------------------------------------- #
    async def live_grants_for_assets(
        self, asset_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[AccessGrant]]:
        """Every live grant on these assets, grouped by asset, oldest first."""
        if not asset_ids:
            return {}
        rows = await self._session.execute(
            select(AccessGrant)
            .where(AccessGrant.asset_id.in_(asset_ids), AccessGrant.revoked_at.is_(None))
            .order_by(AccessGrant.granted_at)
        )
        out: dict[uuid.UUID, list[AccessGrant]] = {}
        for grant in rows.scalars().all():
            out.setdefault(grant.asset_id, []).append(grant)
        return out

    async def live_grants_for_employee(self, employee_id: uuid.UUID) -> Sequence[AccessGrant]:
        rows = await self._session.execute(
            select(AccessGrant)
            .where(AccessGrant.employee_id == employee_id, AccessGrant.revoked_at.is_(None))
            .order_by(AccessGrant.granted_at.desc())
        )
        return rows.scalars().all()

    async def live_grant(self, asset_id: uuid.UUID, employee_id: uuid.UUID) -> AccessGrant | None:
        grant: AccessGrant | None = await self._session.scalar(
            select(AccessGrant).where(
                AccessGrant.asset_id == asset_id,
                AccessGrant.employee_id == employee_id,
                AccessGrant.revoked_at.is_(None),
            )
        )
        return grant

    async def get_grant(self, grant_id: uuid.UUID) -> AccessGrant | None:
        return await self._session.get(AccessGrant, grant_id)

    async def grant(
        self,
        *,
        asset_id: uuid.UUID,
        employee_id: uuid.UUID,
        level: AccessLevel,
        granted_by: uuid.UUID | None,
        request_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> AccessGrant:
        """Create a live grant, or move an existing live one to `level`. One live
        row per (asset, person) - the partial unique index enforces it too."""
        existing = await self.live_grant(asset_id, employee_id)
        if existing is not None:
            existing.level = level
            existing.granted_by = granted_by
            existing.request_id = request_id or existing.request_id
            await self._session.flush()
            return existing
        grant = AccessGrant(
            asset_id=asset_id,
            employee_id=employee_id,
            level=level,
            granted_by=granted_by,
            granted_at=datetime.now(UTC),
            request_id=request_id,
            note=note,
        )
        self._session.add(grant)
        await self._session.flush()
        return grant

    # ---- requests -------------------------------------------------------- #
    def add_request(self, request: AccessRequest) -> None:
        self._session.add(request)

    async def get_request(self, request_id: uuid.UUID) -> AccessRequest | None:
        return await self._session.get(AccessRequest, request_id)

    async def pending_request(
        self, asset_id: uuid.UUID, employee_id: uuid.UUID
    ) -> AccessRequest | None:
        found: AccessRequest | None = await self._session.scalar(
            select(AccessRequest).where(
                AccessRequest.asset_id == asset_id,
                AccessRequest.employee_id == employee_id,
                AccessRequest.status == AccessRequestStatus.PENDING,
            )
        )
        return found

    async def requests_for(
        self,
        *,
        mine: uuid.UUID,
        pending_from: Sequence[uuid.UUID] | None,
        limit: int,
    ) -> Sequence[AccessRequest]:
        """The caller's own requests, plus pending ones from `pending_from`
        (None = pending from anyone, for HR and admins), newest first."""
        pending = AccessRequest.status == AccessRequestStatus.PENDING
        if pending_from is None:
            others = pending
        elif pending_from:
            others = pending & AccessRequest.employee_id.in_(pending_from)
        else:
            others = None
        clause = (
            AccessRequest.employee_id == mine
            if others is None
            else or_(AccessRequest.employee_id == mine, others)
        )
        rows = await self._session.execute(
            select(AccessRequest)
            .where(clause)
            .order_by(AccessRequest.created_at.desc())
            .limit(limit)
        )
        return rows.scalars().all()

    async def flush(self) -> None:
        await self._session.flush()
