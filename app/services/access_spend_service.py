"""Access spend - what the company pays for its tools, for finance.

Org-wide money, so it is for the people who run the books: HR, admins and
anyone holding the payroll-manager grant (the finance team). Everyone else
sees cost only on the catalog cards they are already allowed to see.

Each costed asset is charged to a team: the `team` written on the asset if
finance set one, else its owner's department, else "Unassigned".
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.access_spend import (
    SpendInput,
    SpendLine,
    SpendTotals,
    group_totals,
    renewals_within,
    to_line,
    totals,
)
from app.core.access_spend_export import build_spend_xlsx
from app.core.exceptions import AuthorizationError
from app.models.access import TechAsset, TechAssetKind
from app.models.employee import Employee
from app.repositories.access import AccessRepository
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.access import (
    SpendGroupRead,
    SpendLineRead,
    SpendSummaryRead,
    SpendTotalRead,
)
from app.schemas.auth import CurrentUser

_OFFICE_TZ = ZoneInfo("Asia/Kolkata")
_RENEWAL_WINDOW_DAYS = 90


def can_see_spend(caller: CurrentUser) -> bool:
    return caller.can_manage_payroll


class AccessSpendService:
    def __init__(
        self, access: AccessRepository, employees: EmployeeRepository, audit: AuditRepository
    ) -> None:
        self._access = access
        self._employees = employees
        self._audit = audit

    async def summary(self, caller: CurrentUser) -> SpendSummaryRead:
        lines, today = await self._lines(caller)
        await self._audit.append(actor=str(caller.employee_id), action="access.spend.read")
        return SpendSummaryRead(
            generated_on=today,
            count=totals(lines).count,
            totals=_totals(totals(lines)),
            by_team=_groups(lines, "team"),
            by_category=_groups(lines, "category"),
            by_kind=_groups(lines, "kind"),
            renewals=[_line(x) for x in renewals_within(lines, today, _RENEWAL_WINDOW_DAYS)],
            lines=[_line(x) for x in lines],
        )

    async def export(self, caller: CurrentUser) -> tuple[bytes, str]:
        lines, today = await self._lines(caller)
        renewals = renewals_within(lines, today, _RENEWAL_WINDOW_DAYS)
        await self._audit.append(actor=str(caller.employee_id), action="access.spend.export")
        return build_spend_xlsx(
            lines, renewals, generated=today
        ), f"tech-spend-{today.isoformat()}.xlsx"

    async def _lines(self, caller: CurrentUser) -> tuple[list[SpendLine], date]:
        if not can_see_spend(caller):
            raise AuthorizationError()
        assets = [a for a in await self._access.list_assets() if _costed(a)]
        parents = await self._access.assets_by_id(
            list({a.parent_id for a in assets if a.parent_id is not None})
        )
        owners = await self._employees.get_many(
            list({a.owner_id for a in assets if a.owner_id is not None})
        )
        lines = [to_line(_input(a, parents, owners)) for a in assets]
        lines.sort(key=lambda x: (-x.monthly_minor, x.item.name.lower()))
        return lines, datetime.now(UTC).astimezone(_OFFICE_TZ).date()


def _costed(asset: TechAsset) -> bool:
    return asset.cost_minor is not None and asset.cost_minor > 0 and bool(asset.currency)


def _detail(asset: TechAsset, key: str) -> str | None:
    value = (asset.details or {}).get(key)
    return str(value).strip() or None if value is not None else None


def _input(
    asset: TechAsset,
    parents: dict[uuid.UUID, TechAsset],
    owners: dict[uuid.UUID, Employee],
) -> SpendInput:
    owner = owners.get(asset.owner_id) if asset.owner_id is not None else None
    parent = parents.get(asset.parent_id) if asset.parent_id is not None else None
    return SpendInput(
        asset_id=str(asset.id),
        name=asset.name,
        kind=asset.kind.value,
        category=asset.category,
        team=_detail(asset, "team") or (owner.department if owner is not None else None),
        owner_name=owner.full_name if owner is not None else None,
        billed_to=parent.name if parent is not None else None,
        status=asset.status.value,
        cost_minor=asset.cost_minor or 0,
        currency=asset.currency or "INR",
        billing_cycle=asset.billing_cycle,
        renews_on=asset.renews_on,
        payment_method=_detail(asset, "payment_method"),
        card_last4=_detail(asset, "card_last4"),
        auto_renew=_detail(asset, "auto_renew"),
    )


def _totals(t: SpendTotals) -> list[SpendTotalRead]:
    currencies = sorted(t.monthly, key=lambda c: (c != "INR", c))
    return [
        SpendTotalRead(currency=c, monthly_minor=t.monthly[c], annual_minor=t.annual.get(c, 0))
        for c in currencies
    ]


def _groups(lines: list[SpendLine], key: str) -> list[SpendGroupRead]:
    return [
        SpendGroupRead(label=label, count=t.count, totals=_totals(t))
        for label, t in group_totals(lines, key)
    ]


def _line(line: SpendLine) -> SpendLineRead:
    item = line.item
    return SpendLineRead(
        asset_id=uuid.UUID(item.asset_id),
        name=item.name,
        kind=TechAssetKind(item.kind),
        category=line.category,
        team=line.team,
        owner_name=item.owner_name,
        billed_to=item.billed_to,
        status=item.status,
        billing_cycle=item.billing_cycle,
        currency=item.currency,
        cost_minor=item.cost_minor,
        monthly_minor=line.monthly_minor,
        annual_minor=line.annual_minor,
        renews_on=item.renews_on,
        payment_method=item.payment_method,
        card_last4=item.card_last4,
        auto_renew=item.auto_renew,
        recurring=line.recurring,
        estimated=line.estimated,
        counted=line.counted,
    )
