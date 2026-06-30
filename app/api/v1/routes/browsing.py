"""Browsing endpoints — per-employee domain breakdown + productivity category
split, derived from raw activity and scoped to the caller.

Also hosts the personal "hidden domains" management endpoints. These are gated to
a single configured owner inside the service; everyone else gets a 404, so the
capability stays invisible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import BrowsingPrivacyServiceDep, CurrentUserDep, MonitoringServiceDep
from app.schemas.browsing_privacy import HiddenDomainCreate, HiddenDomainRead
from app.schemas.monitoring import BrowsingRead

router = APIRouter(prefix="/browsing", tags=["browsing"])


@router.get("", response_model=list[BrowsingRead])
async def list_browsing(
    caller: CurrentUserDep,
    service: MonitoringServiceDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> list[BrowsingRead]:
    """Per-employee browsing breakdown for a day (defaults to today)."""
    when = datetime.combine(day, time.min, tzinfo=UTC) if day else datetime.now(UTC)
    return await service.browsing(caller, when)


@router.get("/hidden-domains", response_model=list[HiddenDomainRead])
async def list_hidden_domains(
    caller: CurrentUserDep,
    service: BrowsingPrivacyServiceDep,
) -> list[HiddenDomainRead]:
    """The owner's hidden-domain list (404 for anyone else)."""
    return [HiddenDomainRead.model_validate(d) for d in await service.list(caller)]


@router.post(
    "/hidden-domains",
    response_model=HiddenDomainRead,
    status_code=status.HTTP_201_CREATED,
)
async def hide_domain(
    payload: HiddenDomainCreate,
    caller: CurrentUserDep,
    service: BrowsingPrivacyServiceDep,
) -> HiddenDomainRead:
    """Hide a domain from the Browsing tab (owner only)."""
    return HiddenDomainRead.model_validate(await service.add(caller, payload.domain))


@router.delete("/hidden-domains/{hidden_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unhide_domain(
    hidden_id: uuid.UUID,
    caller: CurrentUserDep,
    service: BrowsingPrivacyServiceDep,
) -> None:
    """Stop hiding a domain (owner only)."""
    await service.remove(caller, hidden_id)
