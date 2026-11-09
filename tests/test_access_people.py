"""Access by person - what each person can reach, including through accounts.

Holding devops@ means you can sign in to the tools under it, so those tools must
list you (as "through devops@") and your People entry must include them. Scope
is the same as the rest of Access, and only HR or an admin can revoke.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access_reach import ancestors, reach_for_asset
from app.core.config import Settings
from app.models.access import AccessGrant, AccessLevel
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

BASE = "/api/v1/access"


def _grant(asset: uuid.UUID, person: uuid.UUID) -> AccessGrant:
    return AccessGrant(
        id=uuid.uuid4(),
        asset_id=asset,
        employee_id=person,
        level=AccessLevel.EDITOR,
        granted_at=datetime.now(UTC),
    )


def test_access_flows_down_and_direct_grants_win() -> None:
    account, tool, alice, bob = (uuid.uuid4() for _ in range(4))
    parent_of = {tool: account, account: None}
    grants = {account: [_grant(account, alice), _grant(account, bob)], tool: [_grant(tool, bob)]}
    reach = reach_for_asset(tool, parent_of, grants)
    by_person = {r.employee_id: r.via_asset_id for r in reach}
    assert by_person == {bob: None, alice: account}


def test_a_loop_in_the_chain_is_cut() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert ancestors(a, {a: b, b: a}) == [b]


async def _hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-hr",
        work_email="hr@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _setup(client: AsyncClient, settings: Settings, seed: _Seed) -> tuple[str, str]:
    admin = auth_headers(settings, seed.admin)
    account = await client.post(
        f"{BASE}/assets", json={"kind": "account", "name": "devops@corp.test"}, headers=admin
    )
    tool = await client.post(
        f"{BASE}/assets",
        json={"kind": "tool", "name": "Vercel", "parent_id": account.json()["id"]},
        headers=admin,
    )
    grant = await client.post(
        f"{BASE}/grants",
        json={"asset_id": account.json()["id"], "employee_id": str(seed.report.id)},
        headers=admin,
    )
    assert grant.status_code == 201
    return account.json()["id"], tool.json()["id"]


async def test_a_tool_lists_who_reaches_it_through_its_account(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    _, tool = await _setup(client, settings, seed)
    view = (
        await client.get(f"{BASE}/assets/{tool}", headers=auth_headers(settings, seed.manager))
    ).json()
    assert view["holders"] == []
    assert [(h["employee_id"], h["via_name"]) for h in view["inherited_holders"]] == [
        (str(seed.report.id), "devops@corp.test")
    ]
    assert view["reach_count"] == 1
    mine = (
        await client.get(f"{BASE}/assets/{tool}", headers=auth_headers(settings, seed.report))
    ).json()
    assert mine["my_via"] == "devops@corp.test" and mine["my_level"] == "viewer"
    outsider = (
        await client.get(f"{BASE}/assets/{tool}", headers=auth_headers(settings, seed.outsider))
    ).json()
    assert outsider["inherited_holders"] == [] and outsider["reach_count"] == 1


async def test_people_lists_everything_a_person_reaches_within_scope(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    await _setup(client, settings, seed)
    people = (
        await client.get(f"{BASE}/people", headers=auth_headers(settings, seed.manager))
    ).json()
    report = next(p for p in people if p["employee_id"] == str(seed.report.id))
    assert report["direct_count"] == 1 and report["reach_count"] == 2
    assert [(i["name"], i["via_name"]) for i in report["items"]] == [
        ("devops@corp.test", None),
        ("Vercel", "devops@corp.test"),
    ]
    assert str(seed.outsider.id) not in {p["employee_id"] for p in people}

    own = (await client.get(f"{BASE}/people", headers=auth_headers(settings, seed.outsider))).json()
    assert [p["employee_id"] for p in own] == [str(seed.outsider.id)]
    hidden = await client.get(
        f"{BASE}/people/{seed.report.id}", headers=auth_headers(settings, seed.outsider)
    )
    assert hidden.status_code == 404


async def test_only_hr_or_admins_revoke_all_and_it_ends_inherited_access(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    await _setup(client, settings, seed)
    url = f"{BASE}/people/{seed.report.id}/revoke-all"
    assert (await client.post(url, headers=auth_headers(settings, seed.manager))).status_code == 403
    hr = await _hr(db)
    done = await client.post(url, headers=auth_headers(settings, hr))
    assert done.status_code == 200 and done.json() == {"revoked": 1}
    after = (
        await client.get(f"{BASE}/people/{seed.report.id}", headers=auth_headers(settings, hr))
    ).json()
    assert after["reach_count"] == 0
