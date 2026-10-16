"""Forms: builder, library, sending and filling in.

Two things matter beyond "it stores answers". Scope: who must fill in what, and
what they said, is sensitive, so anyone out of scope gets 404 and never data
(CLAUDE.md §9, one test per endpoint). And meaning: a published form never
changes, so every answer stays tied to the exact question that was asked.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.form import AssignmentStatus, Form, FormAssignment, FormStatus, FormTrigger
from tests.conftest import _Seed, auth_headers

_LABELS = ["Poor", "Below Average", "Average", "Good", "Excellent"]


def _form(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "New joiner feedback",
        "intro": "We'd value your candid feedback.",
        "rating_labels": _LABELS,
        "visibility": "everyone",
        "fields": [
            {"type": "section", "label": "Section A. Overall"},
            {"type": "rating", "label": "How was onboarding?", "with_comment": True},
            {"type": "paragraph", "label": "What should we keep doing?", "required": False},
            {
                "type": "choice",
                "label": "Would you be a buddy?",
                "options": ["Definitely", "Probably", "Unsure"],
            },
        ],
    }
    body.update(overrides)
    return body


async def _create(client: AsyncClient, settings: Settings, admin: Employee, **kw: Any) -> dict:
    res = await client.post(
        "/api/v1/forms", json=_form(**kw), headers=auth_headers(settings, admin)
    )
    assert res.status_code == 201, res.text
    return res.json()


async def _publish(client: AsyncClient, settings: Settings, admin: Employee, form_id: str) -> dict:
    res = await client.post(
        f"/api/v1/forms/{form_id}/publish", headers=auth_headers(settings, admin)
    )
    assert res.status_code == 200, res.text
    return res.json()


async def _send(
    client: AsyncClient, settings: Settings, admin: Employee, form_id: str, to: Employee
) -> str:
    res = await client.post(
        f"/api/v1/forms/{form_id}/assignments",
        json={"assignee_ids": [str(to.id)]},
        headers=auth_headers(settings, admin),
    )
    assert res.status_code == 201, res.text
    return str(res.json()[0]["id"])


def _answers(form: dict, *, rating: int = 4, choice: str = "Probably") -> list[dict]:
    fields = {f["type"]: f["id"] for f in form["fields"]}
    return [
        {"field_id": fields["rating"], "rating": rating, "text": "Smooth overall."},
        {"field_id": fields["choice"], "choice": choice},
    ]


# --- building ---------------------------------------------------------------- #
async def test_only_hr_and_admins_build_forms(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.post(
        "/api/v1/forms", json=_form(), headers=auth_headers(settings, seed.report)
    )
    assert res.status_code == 404
    form = await _create(client, settings, seed.admin)
    for method, path in (
        ("put", f"/api/v1/forms/{form['id']}"),
        ("delete", f"/api/v1/forms/{form['id']}"),
        ("post", f"/api/v1/forms/{form['id']}/publish"),
        ("post", f"/api/v1/forms/{form['id']}/versions"),
        ("post", f"/api/v1/forms/{form['id']}/archive"),
        ("get", f"/api/v1/forms/{form['id']}/results"),
        ("get", f"/api/v1/forms/{form['id']}/assignments"),
    ):
        kwargs: dict[str, Any] = {"headers": auth_headers(settings, seed.report)}
        if method == "put":
            kwargs["json"] = _form()
        res = await client.request(method, path, **kwargs)
        assert res.status_code == 404, (method, path)


async def test_rating_questions_need_five_labels(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.post(
        "/api/v1/forms",
        json=_form(rating_labels=["Bad", "Good"]),
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 422


async def test_triggered_form_needs_a_day_count(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.post(
        "/api/v1/forms",
        json=_form(trigger="new_joiner"),
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 422


async def test_published_form_cannot_change_but_can_be_versioned(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    admin = auth_headers(settings, seed.admin)
    form = await _create(client, settings, seed.admin)
    await _publish(client, settings, seed.admin, form["id"])

    res = await client.put(f"/api/v1/forms/{form['id']}", json=_form(title="Edited"), headers=admin)
    assert res.status_code == 409
    assert (await client.delete(f"/api/v1/forms/{form['id']}", headers=admin)).status_code == 409

    draft = (await client.post(f"/api/v1/forms/{form['id']}/versions", headers=admin)).json()
    assert draft["version"] == 2 and draft["status"] == "draft"
    assert len(draft["fields"]) == len(form["fields"])
    # Only one draft at a time per form.
    assert (
        await client.post(f"/api/v1/forms/{form['id']}/versions", headers=admin)
    ).status_code == 409

    await _publish(client, settings, seed.admin, draft["id"])
    old = (await client.get(f"/api/v1/forms/{form['id']}", headers=admin)).json()
    assert old["status"] == "archived"


async def test_only_one_live_form_per_trigger(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    first = await _create(client, settings, seed.admin, trigger="new_joiner", trigger_days=15)
    published = await _publish(client, settings, seed.admin, first["id"])
    assert published["auto_assign_since"] is not None  # Option A: joiners from today on
    second = await _create(client, settings, seed.admin, trigger="new_joiner", trigger_days=15)
    res = await client.post(
        f"/api/v1/forms/{second['id']}/publish", headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 409


# --- library ----------------------------------------------------------------- #
async def test_library_hides_drafts_and_restricted_forms(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    draft = await _create(client, settings, seed.admin)
    open_form = await _create(client, settings, seed.admin, title="Open")
    await _publish(client, settings, seed.admin, open_form["id"])
    restricted = await _create(
        client,
        settings,
        seed.admin,
        title="Managers only",
        visibility="restricted",
        visible_employee_ids=[str(seed.manager.id)],
    )
    await _publish(client, settings, seed.admin, restricted["id"])

    listed = await client.get("/api/v1/forms", headers=auth_headers(settings, seed.report))
    assert [f["title"] for f in listed.json()] == ["Open"]
    for form_id in (draft["id"], restricted["id"]):
        res = await client.get(
            f"/api/v1/forms/{form_id}", headers=auth_headers(settings, seed.report)
        )
        assert res.status_code == 404
    res = await client.get(
        f"/api/v1/forms/{restricted['id']}", headers=auth_headers(settings, seed.manager)
    )
    assert res.status_code == 200
    # Counts are HR's business only.
    assert listed.json()[0]["response_count"] == 0


# --- sending and filling in ----------------------------------------------------- #
async def test_draft_cannot_be_sent(client: AsyncClient, settings: Settings, seed: _Seed) -> None:
    form = await _create(client, settings, seed.admin)
    res = await client.post(
        f"/api/v1/forms/{form['id']}/assignments",
        json={"assignee_ids": [str(seed.report.id)]},
        headers=auth_headers(settings, seed.admin),
    )
    assert res.status_code == 409


async def test_employee_cannot_send_forms(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(client, settings, seed.admin)
    await _publish(client, settings, seed.admin, form["id"])
    res = await client.post(
        f"/api/v1/forms/{form['id']}/assignments",
        json={"assignee_ids": [str(seed.outsider.id)]},
        headers=auth_headers(settings, seed.report),
    )
    assert res.status_code == 404


async def test_people_see_and_open_only_their_own_forms(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(client, settings, seed.admin)
    await _publish(client, settings, seed.admin, form["id"])
    assignment_id = await _send(client, settings, seed.admin, form["id"], seed.report)

    mine = await client.get(
        "/api/v1/forms/assignments/mine", headers=auth_headers(settings, seed.report)
    )
    assert [a["id"] for a in mine.json()] == [assignment_id]
    theirs = await client.get(
        "/api/v1/forms/assignments/mine", headers=auth_headers(settings, seed.outsider)
    )
    assert theirs.json() == []

    path = f"/api/v1/forms/assignments/{assignment_id}"
    assert (
        await client.get(path, headers=auth_headers(settings, seed.outsider))
    ).status_code == 404
    # Their manager has no business reading their feedback either.
    assert (await client.get(path, headers=auth_headers(settings, seed.manager))).status_code == 404
    assert (await client.get(path, headers=auth_headers(settings, seed.admin))).status_code == 200


async def test_submit_checks_answers_and_only_once(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(client, settings, seed.admin)
    form = await _publish(client, settings, seed.admin, form["id"])
    assignment_id = await _send(client, settings, seed.admin, form["id"], seed.report)
    path = f"/api/v1/forms/assignments/{assignment_id}/submit"
    me = auth_headers(settings, seed.report)

    # Someone else can't answer for them; HR can read but not answer either.
    for other in (seed.outsider, seed.admin):
        body = {"answers": _answers(form)}
        res = await client.post(path, json=body, headers=auth_headers(settings, other))
        assert res.status_code == 404

    missing = {"answers": _answers(form)[1:]}  # no rating for a required question
    assert (await client.post(path, json=missing, headers=me)).status_code == 422
    bad_choice = {"answers": _answers(form, choice="Maybe later")}
    assert (await client.post(path, json=bad_choice, headers=me)).status_code == 422
    out_of_range = {"answers": _answers(form, rating=9)}
    assert (await client.post(path, json=out_of_range, headers=me)).status_code == 422

    done = await client.post(path, json={"answers": _answers(form)}, headers=me)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "submitted"
    assert (
        await client.post(path, json={"answers": _answers(form)}, headers=me)
    ).status_code == 409


async def test_results_average_the_ratings(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(client, settings, seed.admin)
    form = await _publish(client, settings, seed.admin, form["id"])
    for person, rating in ((seed.report, 5), (seed.outsider, 2)):
        assignment_id = await _send(client, settings, seed.admin, form["id"], person)
        res = await client.post(
            f"/api/v1/forms/assignments/{assignment_id}/submit",
            json={"answers": _answers(form, rating=rating)},
            headers=auth_headers(settings, person),
        )
        assert res.status_code == 200

    results = (
        await client.get(
            f"/api/v1/forms/{form['id']}/results", headers=auth_headers(settings, seed.admin)
        )
    ).json()
    rating = next(f for f in results["fields"] if f["type"] == "rating")
    assert rating["average"] == 3.5 and rating["counts"] == {"5": 1, "2": 1}
    assert len(results["responses"]) == 2


async def test_needs_buddy_list_is_hr_only(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    path = "/api/v1/forms/assignments/needs-buddy"
    assert (await client.get(path, headers=auth_headers(settings, seed.report))).status_code == 404
    assert (await client.get(path, headers=auth_headers(settings, seed.admin))).status_code == 200


async def test_hr_joiner_waits_for_their_own_form_like_anyone(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Being HR/admin lets you read other people's forms, not open your own early."""
    form = await _create(client, settings, seed.admin)
    form = await _publish(client, settings, seed.admin, form["id"])
    assignment_id = await _send(client, settings, seed.admin, form["id"], seed.admin)
    row = await db.get(FormAssignment, uuid.UUID(assignment_id))
    assert row is not None
    row.available_on = date.today() + timedelta(days=5)
    await db.commit()
    me = auth_headers(settings, seed.admin)
    path = f"/api/v1/forms/assignments/{assignment_id}"
    assert (await client.get(path, headers=me)).status_code == 404
    body = {"answers": _answers(form)}
    assert (await client.post(f"{path}/submit", json=body, headers=me)).status_code == 404


async def test_hr_reading_someones_answers_is_audited(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form = await _create(client, settings, seed.admin)
    form = await _publish(client, settings, seed.admin, form["id"])
    assignment_id = await _send(client, settings, seed.admin, form["id"], seed.report)
    res = await client.get(
        f"/api/v1/forms/assignments/{assignment_id}", headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 200
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert "form.answers.view" in actions


async def test_withdrawing_a_form_cancels_what_was_not_sent(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form = await _create(client, settings, seed.admin)
    form = await _publish(client, settings, seed.admin, form["id"])
    assignment_id = await _send(client, settings, seed.admin, form["id"], seed.report)
    res = await client.post(
        f"/api/v1/forms/{form['id']}/archive", headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 200
    row = await db.scalar(
        select(FormAssignment)
        .where(FormAssignment.id == uuid.UUID(assignment_id))
        .execution_options(populate_existing=True)
    )
    assert row is not None and row.status is AssignmentStatus.CANCELLED


async def test_republishing_after_a_gap_starts_from_today(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """Joiners from the gap while a form was withdrawn are not mailed a backlog."""
    admin = auth_headers(settings, seed.admin)
    form = await _create(client, settings, seed.admin, trigger="new_joiner", trigger_days=15)
    await _publish(client, settings, seed.admin, form["id"])
    old = await db.get(Form, uuid.UUID(form["id"]))
    assert old is not None
    old.auto_assign_since = date.today() - timedelta(days=200)
    await db.commit()
    await client.post(f"/api/v1/forms/{form['id']}/archive", headers=admin)
    draft = (await client.post(f"/api/v1/forms/{form['id']}/versions", headers=admin)).json()
    republished = await _publish(client, settings, seed.admin, draft["id"])
    assert republished["auto_assign_since"] == date.today().isoformat()


async def test_library_hides_who_a_form_is_shared_with(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(
        client,
        settings,
        seed.admin,
        visibility="restricted",
        visible_departments=["Sales"],
        visible_employee_ids=[str(seed.report.id), str(seed.outsider.id)],
    )
    await _publish(client, settings, seed.admin, form["id"])
    seen = (await client.get("/api/v1/forms", headers=auth_headers(settings, seed.report))).json()
    assert seen[0]["visible_employee_ids"] == [] and seen[0]["visible_departments"] == []


# --- input hygiene and the review fixes ---------------------------------------- #
async def test_control_characters_are_refused_or_dropped(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    admin = auth_headers(settings, seed.admin)
    # A NUL would otherwise be a 500 on Postgres; CR/LF in a title breaks emails.
    for title in ("Bad\x00title", "Two\r\nlines"):
        res = await client.post("/api/v1/forms", json=_form(title=title), headers=admin)
        assert res.status_code == 422
    form = await _create(client, settings, seed.admin, intro="Para one\x00.\r\n\r\nPara two")
    assert form["intro"] == "Para one.\n\nPara two"


async def test_choice_options_are_trimmed_and_must_differ(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    admin = auth_headers(settings, seed.admin)
    fields = _form()["fields"]
    fields[3]["options"] = ["  Yes ", " ", "No "]
    form = await _create(client, settings, seed.admin, fields=fields)
    choice = next(f for f in form["fields"] if f["type"] == "choice")
    assert choice["options"] == ["Yes", "No"]
    for options in (["Yes", "yes"], ["Yes", "x" * 201]):
        fields[3]["options"] = options
        res = await client.post("/api/v1/forms", json=_form(fields=fields), headers=admin)
        assert res.status_code == 422, options


async def test_rating_labels_must_be_short_and_different(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    admin = auth_headers(settings, seed.admin)
    for labels in (["Bad", "Bad", "Ok", "Good", "Great"], ["a", "b", "c", "d", "e" * 81]):
        res = await client.post("/api/v1/forms", json=_form(rating_labels=labels), headers=admin)
        assert res.status_code == 422, labels


async def test_backend_rule_messages_reach_the_user(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    res = await client.post(
        "/api/v1/forms", json=_form(rating_labels=["a"]), headers=auth_headers(settings, seed.admin)
    )
    assert res.status_code == 422
    assert "label for each of 1 to 5" in res.json()["error"]["message"]


async def test_one_answer_per_question(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    form = await _create(client, settings, seed.admin)
    form = await _publish(client, settings, seed.admin, form["id"])
    assignment_id = await _send(client, settings, seed.admin, form["id"], seed.report)
    answers = _answers(form)
    twice = {"answers": [*answers, {**answers[0], "rating": 1}]}
    res = await client.post(
        f"/api/v1/forms/assignments/{assignment_id}/submit",
        json=twice,
        headers=auth_headers(settings, seed.report),
    )
    assert res.status_code == 422


async def test_manual_send_checks_due_date_and_subject(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    form = await _create(client, settings, seed.admin)
    await _publish(client, settings, seed.admin, form["id"])
    path = f"/api/v1/forms/{form['id']}/assignments"
    admin = auth_headers(settings, seed.admin)
    for due in (date.today() - timedelta(days=1), date.today() + timedelta(days=400)):
        body = {"assignee_ids": [str(seed.report.id)], "due_on": due.isoformat()}
        assert (await client.post(path, json=body, headers=admin)).status_code == 422
    seed.outsider.is_active = False
    await db.commit()
    body = {"assignee_ids": [str(seed.report.id)], "subject_id": str(seed.outsider.id)}
    assert (await client.post(path, json=body, headers=admin)).status_code == 422


async def test_changing_the_trigger_starts_the_go_live_date_again(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """A joiner form turned into a buddy form must not reach the buddies of
    everyone who joined since the joiner form first went live."""
    admin = auth_headers(settings, seed.admin)
    form = await _create(client, settings, seed.admin, trigger="new_joiner", trigger_days=15)
    await _publish(client, settings, seed.admin, form["id"])
    first = await db.get(Form, uuid.UUID(form["id"]))
    assert first is not None
    first.auto_assign_since = date.today() - timedelta(days=365)
    await db.commit()
    draft = (await client.post(f"/api/v1/forms/{form['id']}/versions", headers=admin)).json()
    res = await client.put(
        f"/api/v1/forms/{draft['id']}",
        json=_form(trigger="buddy", trigger_days=30),
        headers=admin,
    )
    assert res.status_code == 200
    republished = await _publish(client, settings, seed.admin, draft["id"])
    assert republished["auto_assign_since"] == date.today().isoformat()


async def test_withdrawing_cancels_what_older_versions_hold(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    admin = auth_headers(settings, seed.admin)
    form = await _create(client, settings, seed.admin, trigger="buddy", trigger_days=30)
    await _publish(client, settings, seed.admin, form["id"])
    waiting = FormAssignment(
        form_id=uuid.UUID(form["id"]),
        subject_id=seed.report.id,
        trigger=FormTrigger.BUDDY,
        status=AssignmentStatus.NEEDS_BUDDY,
        available_on=date.today(),
    )
    db.add(waiting)
    await db.commit()
    draft = (await client.post(f"/api/v1/forms/{form['id']}/versions", headers=admin)).json()
    await _publish(client, settings, seed.admin, draft["id"])  # v1 archived, still waiting
    await client.post(f"/api/v1/forms/{draft['id']}/archive", headers=admin)  # withdrawn
    row = await db.scalar(
        select(FormAssignment)
        .where(FormAssignment.id == waiting.id)
        .execution_options(populate_existing=True)
    )
    assert row is not None and row.status is AssignmentStatus.CANCELLED


async def test_database_allows_one_live_form_per_trigger(db: AsyncSession) -> None:
    """The backstop for two publishes racing past the service's own check."""
    for _ in range(2):
        db.add(
            Form(
                family_id=uuid.uuid4(),
                title="Joiner",
                status=FormStatus.PUBLISHED,
                trigger=FormTrigger.NEW_JOINER,
                trigger_days=15,
            )
        )
    with pytest.raises(IntegrityError):
        await db.commit()
