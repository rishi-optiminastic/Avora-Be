"""Sheets: who may see, edit and manage a spreadsheet, the save lock, the size
cap, and that planted script links never reach another person's browser."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.workbook_snapshot import MAX_SNAPSHOT_BYTES, is_safe_link, scrub_links
from app.models.employee import Employee, Role
from tests.conftest import _Seed, auth_headers

_URL = "/api/v1/sheets"


async def _create(
    client: AsyncClient, settings: Settings, owner: Employee, sharing: str = "private"
) -> dict[str, Any]:
    res = await client.post(
        _URL,
        headers=auth_headers(settings, owner),
        json={"title": "  Tech  tracker ", "sharing": sharing},
    )
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    return body


async def _save(
    client: AsyncClient,
    settings: Settings,
    who: Employee,
    sheet_id: str,
    body: dict[str, Any],
):
    return await client.put(
        f"{_URL}/{sheet_id}/content", headers=auth_headers(settings, who), json=body
    )


def _snapshot(value: str = "Done") -> dict[str, Any]:
    return {"id": "wb", "sheets": {"s1": {"cellData": {"0": {"0": {"v": value}}}}}}


# --- visibility ------------------------------------------------------------- #
async def test_a_private_sheet_is_the_owners_alone(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report)
    assert sheet["title"] == "Tech tracker"
    assert sheet["can_edit"] and sheet["can_manage"]

    hidden = await client.get(
        f"{_URL}/{sheet['id']}", headers=auth_headers(settings, seed.outsider)
    )
    listed = await client.get(_URL, headers=auth_headers(settings, seed.outsider))
    admin = await client.get(f"{_URL}/{sheet['id']}", headers=auth_headers(settings, seed.admin))

    assert hidden.status_code == 404  # existence is not revealed
    assert listed.json()["total"] == 0
    assert admin.status_code == 200


async def test_view_only_sharing_lets_others_read_but_not_save(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report, "org_view")

    read = await client.get(f"{_URL}/{sheet['id']}", headers=auth_headers(settings, seed.outsider))
    save = await _save(client, settings, seed.outsider, sheet["id"], {"version": 1, "snapshot": {}})

    assert read.status_code == 200
    assert read.json()["can_edit"] is False
    assert save.status_code == 403


async def test_edit_sharing_lets_others_save_but_not_manage(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report, "org_edit")
    headers = auth_headers(settings, seed.outsider)

    save = await _save(
        client, settings, seed.outsider, sheet["id"], {"version": 1, "snapshot": _snapshot()}
    )
    rename = await client.patch(f"{_URL}/{sheet['id']}", headers=headers, json={"title": "Mine"})
    delete = await client.delete(f"{_URL}/{sheet['id']}", headers=headers)

    assert save.status_code == 200, save.text
    assert save.json()["version"] == 2
    assert rename.status_code == 403
    assert delete.status_code == 403


async def test_a_viewer_never_edits_or_creates(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report, "org_edit")
    viewer = await db.get(Employee, seed.outsider.id)
    assert viewer is not None
    viewer.role = Role.VIEWER
    await db.commit()

    save = await _save(client, settings, viewer, sheet["id"], {"version": 1, "snapshot": {}})
    create = await client.post(_URL, headers=auth_headers(settings, viewer), json={"title": "x"})

    assert save.status_code == 403
    assert create.status_code == 403


# --- saving ----------------------------------------------------------------- #
async def test_a_stale_save_is_refused_unless_forced(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report, "org_edit")
    first = await _save(
        client, settings, seed.outsider, sheet["id"], {"version": 1, "snapshot": _snapshot("A")}
    )
    assert first.status_code == 200

    stale = await _save(
        client, settings, seed.report, sheet["id"], {"version": 1, "snapshot": _snapshot("B")}
    )
    forced = await _save(
        client,
        settings,
        seed.report,
        sheet["id"],
        {"version": 1, "snapshot": _snapshot("B"), "force": True},
    )
    after = await client.get(f"{_URL}/{sheet['id']}", headers=auth_headers(settings, seed.report))

    assert stale.status_code == 409
    assert seed.outsider.full_name in stale.json()["error"]["message"]
    assert forced.status_code == 200 and forced.json()["version"] == 3
    assert after.json()["snapshot"] == _snapshot("B")


async def test_an_oversized_workbook_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report)
    huge = {"version": 1, "snapshot": {"blob": "x" * (MAX_SNAPSHOT_BYTES + 4096)}}

    res = await _save(client, settings, seed.report, sheet["id"], huge)

    assert res.status_code == 413


async def test_a_malformed_save_is_a_422(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report)

    res = await _save(client, settings, seed.report, sheet["id"], {"version": 0, "snapshot": []})

    assert res.status_code == 422


async def test_script_links_are_blanked_before_storing(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report)
    links = {"s1": [{"id": "l1", "row": 0, "column": 0, "payload": "javascript:alert(1)"}]}
    snapshot = {
        "sheets": {
            "s1": {
                "cellData": {
                    "0": {
                        "0": {
                            "p": {
                                "body": {
                                    "customRanges": [
                                        {"properties": {"url": "JaVa\tScRiPt:alert(1)"}},
                                        {"properties": {"url": "https://avora.example"}},
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        },
        "resources": [{"name": "SHEET_HYPER_LINK_PLUGIN", "data": json.dumps(links)}],
    }

    res = await _save(
        client, settings, seed.report, sheet["id"], {"version": 1, "snapshot": snapshot}
    )
    stored = (
        await client.get(f"{_URL}/{sheet['id']}", headers=auth_headers(settings, seed.report))
    ).json()

    assert res.status_code == 200, res.text
    ranges = stored["snapshot"]["sheets"]["s1"]["cellData"]["0"]["0"]["p"]["body"]["customRanges"]
    assert ranges[0]["properties"]["url"] == ""
    assert ranges[1]["properties"]["url"] == "https://avora.example"
    plugin = json.loads(stored["snapshot"]["resources"][0]["data"])
    assert plugin["s1"][0]["payload"] == ""


# --- managing --------------------------------------------------------------- #
async def test_the_owner_renames_reshares_and_deletes(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    sheet = await _create(client, settings, seed.report)
    headers = auth_headers(settings, seed.report)

    patched = await client.patch(
        f"{_URL}/{sheet['id']}", headers=headers, json={"title": "Q4 plan", "sharing": "org_view"}
    )
    deleted = await client.delete(f"{_URL}/{sheet['id']}", headers=headers)
    gone = await client.get(f"{_URL}/{sheet['id']}", headers=headers)

    assert patched.json()["title"] == "Q4 plan" and patched.json()["sharing"] == "org_view"
    assert deleted.status_code == 204
    assert gone.status_code == 404


async def test_a_blank_title_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.post(
        _URL, headers=auth_headers(settings, seed.report), json={"title": "   "}
    )
    assert res.status_code == 422


# --- the link rule itself --------------------------------------------------- #
@pytest.mark.parametrize(
    "link",
    [
        "https://a.example",
        "http://a.example",
        "mailto:a@b.c",
        "tel:+911",
        "#gid=s1&range=A1",
        "docs/x",
    ],
)
def test_safe_links_pass(link: str) -> None:
    assert is_safe_link(link)


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        " javascript:x",
        "JAVASCRIPT:x",
        "java\nscript:x",
        "data:text/html,x",
        "vbscript:x",
    ],
)
def test_script_links_fail(link: str) -> None:
    assert not is_safe_link(link)


def test_scrub_leaves_ordinary_cells_alone() -> None:
    snapshot = _snapshot("javascript:looks-like-a-link-but-is-just-text")
    assert scrub_links(snapshot) == snapshot
