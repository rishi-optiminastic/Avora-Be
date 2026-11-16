"""CPD assessments - writing a test, assigning it, sitting it, and the marks.

The thing most worth protecting here is the ANSWER KEY. A taker's view of a
question has no `correct` field at all, and several tests below exist only to
prove there is no route that leaks one.

Authorization (CLAUDE.md §9): HR and admins write, assign and read everyone's
results; everyone else sits their own test and reads only their own history.
A reporting manager deliberately cannot read a report's marks - a test result
sits closer to a payslip than to an attendance record.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/assessments"


def _paper() -> dict[str, object]:
    return {
        "title": "Data handling basics",
        "description": "Annual refresher.",
        "pass_mark": 60,
        "questions": [
            {
                "prompt": "Where do client files belong?",
                "kind": "single",
                "options": ["Desktop", "The workspace drive", "Email"],
                "correct": [1],
                "points": 1,
            },
            {
                "prompt": "Which count as personal data?",
                "kind": "multi",
                "options": ["Home address", "Office floor", "Phone number", "Job title"],
                "correct": [0, 2],
                "points": 2,
            },
        ],
    }


async def _add_hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-cpd",
        work_email="hr-cpd@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


async def _publish(client: AsyncClient, settings: Settings, who: Employee) -> str:
    made = await client.post(URL, json=_paper(), headers=auth_headers(settings, who))
    assert made.status_code == 201, made.text
    aid = str(made.json()["id"])
    live = await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, who))
    assert live.status_code == 200, live.text
    return aid


async def _assign_to(
    client: AsyncClient, settings: Settings, who: Employee, aid: str, to: Employee
) -> str:
    resp = await client.post(
        f"{URL}/{aid}/assign",
        json={"audience": {"kind": "people", "employee_ids": [str(to.id)]}},
        headers=auth_headers(settings, who),
    )
    assert resp.status_code == 201, resp.text
    mine = await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, to))
    assert mine.status_code == 200, mine.text
    return str(mine.json()[0]["id"])


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
async def test_the_attempts_paths_are_not_swallowed_by_the_id_route(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """`/attempts/me` must not be matched as assessment_id="attempts".

    FastAPI matches in declaration order; with `/{assessment_id}` above it
    this answered 422 from the UUID parser instead of listing attempts.
    """
    resp = await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# Who may do what
# --------------------------------------------------------------------------- #
async def test_every_endpoint_requires_authentication(client: AsyncClient, seed: _Seed) -> None:
    for method, path in (
        ("get", URL),
        ("post", URL),
        ("get", f"{URL}/attempts/me"),
        ("get", f"{URL}/{uuid.uuid4()}"),
        ("post", f"{URL}/{uuid.uuid4()}/publish"),
    ):
        resp = await getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 401, f"{method} {path}"


async def test_only_hr_and_admins_write_a_test(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(URL, json=_paper(), headers=auth_headers(settings, actor))
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"


async def test_only_hr_and_admins_assign(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    aid = await _publish(client, settings, seed.admin)
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.post(
            f"{URL}/{aid}/assign",
            json={"audience": {"kind": "everyone"}},
            headers=auth_headers(settings, actor),
        )
        assert resp.status_code == 403, f"{actor.full_name}: {resp.text}"


async def test_only_hr_and_admins_read_the_results(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    aid = await _publish(client, settings, seed.admin)
    for actor in (seed.report, seed.manager, seed.outsider):
        resp = await client.get(f"{URL}/{aid}/results", headers=auth_headers(settings, actor))
        assert resp.status_code == 404, f"{actor.full_name}: {resp.text}"


async def test_hr_can_do_all_of_it(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _add_hr(db)
    aid = await _publish(client, settings, hr)
    assert (
        await client.get(f"{URL}/{aid}/results", headers=auth_headers(settings, hr))
    ).status_code == 200


# --------------------------------------------------------------------------- #
# The answer key never reaches a taker
# --------------------------------------------------------------------------- #
async def test_the_answer_key_is_not_in_the_paper_a_taker_opens(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The one that matters. `QuestionRead` has no `correct` field, so there
    is no code path that can serialise it to the person sitting the test."""
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)

    opened = await client.get(
        f"{URL}/attempts/{attempt_id}", headers=auth_headers(settings, seed.report)
    )

    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert len(body["questions"]) == 2
    for question in body["questions"]:
        assert "correct" not in question, "the answer key leaked into the paper"
    assert body["answers"] == [], "no marked answers before submitting"


async def test_a_taker_cannot_read_the_test_itself(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """`GET /{id}` returns the key, so it is HR-only - 404 for everyone else
    rather than 403, since the test's existence is not their business."""
    aid = await _publish(client, settings, seed.admin)
    await _assign_to(client, settings, seed.admin, aid, seed.report)

    resp = await client.get(f"{URL}/{aid}", headers=auth_headers(settings, seed.report))
    assert resp.status_code == 404


async def test_hr_opening_somebody_elses_attempt_does_not_see_the_paper(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """HR may see that a test is outstanding, but not read it on somebody's
    behalf - and especially not with the key beside it."""
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)

    opened = await client.get(
        f"{URL}/attempts/{attempt_id}", headers=auth_headers(settings, seed.admin)
    )

    assert opened.status_code == 200
    assert opened.json()["questions"] == []


async def test_an_unrelated_person_cannot_open_an_attempt(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)

    resp = await client.get(
        f"{URL}/attempts/{attempt_id}", headers=auth_headers(settings, seed.outsider)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Sitting it
# --------------------------------------------------------------------------- #
async def test_submitting_marks_the_paper_and_shows_the_key(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The key appears HERE and nowhere earlier: a mark with no explanation
    teaches nobody anything, and by now knowing it changes nothing."""
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)
    opened = (
        await client.get(
            f"{URL}/attempts/{attempt_id}", headers=auth_headers(settings, seed.report)
        )
    ).json()
    qs = {q["prompt"]: q["id"] for q in opened["questions"]}

    resp = await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={
            "answers": [
                {"question_id": qs["Where do client files belong?"], "chosen": [1]},
                {"question_id": qs["Which count as personal data?"], "chosen": [0, 2]},
            ]
        },
        headers=auth_headers(settings, seed.report),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["score"] == 3
    assert body["max_score"] == 3
    assert body["percentage"] == 100
    assert body["passed"] is True
    assert all(a["correct"] for a in body["answers"]), "the key is shown after marking"


async def test_a_wrong_multi_answer_scores_nothing_for_that_question(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Two of three right is not partial credit - it is a wrong answer to
    "which of these apply"."""
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)
    opened = (
        await client.get(
            f"{URL}/attempts/{attempt_id}", headers=auth_headers(settings, seed.report)
        )
    ).json()
    qs = {q["prompt"]: q["id"] for q in opened["questions"]}

    resp = await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={
            "answers": [
                {"question_id": qs["Where do client files belong?"], "chosen": [1]},
                {"question_id": qs["Which count as personal data?"], "chosen": [0]},
            ]
        },
        headers=auth_headers(settings, seed.report),
    )

    body = resp.json()
    assert body["score"] == 1
    assert body["percentage"] == 33.33
    assert body["passed"] is False


async def test_you_cannot_submit_twice(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)
    first = await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={"answers": []},
        headers=auth_headers(settings, seed.report),
    )
    assert first.status_code == 200

    again = await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={"answers": []},
        headers=auth_headers(settings, seed.report),
    )
    assert again.status_code == 409, again.text


async def test_nobody_can_submit_on_somebody_elses_behalf(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Including HR. The whole point of an auto-marked test is that nobody
    marks it, and a result somebody else entered means nothing."""
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)

    for actor in (seed.admin, seed.manager, seed.outsider):
        resp = await client.post(
            f"{URL}/attempts/{attempt_id}/submit",
            json={"answers": []},
            headers=auth_headers(settings, actor),
        )
        assert resp.status_code == 404, f"{actor.full_name}: {resp.text}"


async def test_an_unanswered_paper_scores_zero_rather_than_failing(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)

    resp = await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={"answers": []},
        headers=auth_headers(settings, seed.report),
    )

    body = resp.json()
    assert body["score"] == 0
    assert body["max_score"] == 3, "unanswered questions stay in the denominator"
    assert body["passed"] is False


# --------------------------------------------------------------------------- #
# Authoring rules
# --------------------------------------------------------------------------- #
async def test_a_test_with_no_questions_cannot_be_published(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """An empty paper marks everybody zero and passes nobody."""
    made = await client.post(
        URL,
        json={"title": "Empty", "questions": []},
        headers=auth_headers(settings, seed.admin),
    )
    aid = made.json()["id"]

    resp = await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 409, resp.text
    assert "question" in resp.json()["error"]["message"].lower()


async def test_a_published_tests_questions_are_frozen(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Editing the paper people have already sat makes two marks mean
    different things."""
    aid = await _publish(client, settings, seed.admin)

    resp = await client.put(
        f"{URL}/{aid}", json=_paper(), headers=auth_headers(settings, seed.admin)
    )

    assert resp.status_code == 409, resp.text
    assert "published" in resp.json()["error"]["message"].lower()


async def test_a_published_test_can_still_be_retitled(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Only the QUESTIONS are frozen. A typo in the title is not a reason to
    make HR republish the whole thing."""
    aid = await _publish(client, settings, seed.admin)

    resp = await client.put(
        f"{URL}/{aid}",
        json={"title": "Data handling basics 2027", "pass_mark": 70, "questions": []},
        headers=auth_headers(settings, seed.admin),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Data handling basics 2027"
    assert len(resp.json()["questions"]) == 2, "and the paper is untouched"


async def test_a_draft_cannot_be_assigned(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    made = await client.post(URL, json=_paper(), headers=auth_headers(settings, seed.admin))
    aid = made.json()["id"]

    resp = await client.post(
        f"{URL}/{aid}/assign",
        json={"audience": {"kind": "everyone"}},
        headers=auth_headers(settings, seed.admin),
    )
    assert resp.status_code == 409, resp.text


async def test_a_single_answer_question_needs_exactly_one_right_option(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Caught at write time, not at marking time: discovering an unanswerable
    question after thirty people have sat it is too late to fix fairly."""
    paper = _paper()
    paper["questions"] = [
        {
            "prompt": "Pick one",
            "kind": "single",
            "options": ["a", "b", "c"],
            "correct": [0, 1],
            "points": 1,
        }
    ]
    resp = await client.post(URL, json=paper, headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 422, resp.text


async def test_a_key_pointing_outside_the_options_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    paper = _paper()
    paper["questions"] = [
        {"prompt": "?", "kind": "single", "options": ["a", "b"], "correct": [7], "points": 1}
    ]
    resp = await client.post(URL, json=paper, headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 422, resp.text


async def test_every_option_being_correct_is_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A question everybody gets right teaches nothing and measures nothing."""
    paper = _paper()
    paper["questions"] = [
        {"prompt": "?", "kind": "multi", "options": ["a", "b"], "correct": [0, 1], "points": 1}
    ]
    resp = await client.post(URL, json=paper, headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 422, resp.text


async def test_duplicate_options_are_refused(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    paper = _paper()
    paper["questions"] = [
        {"prompt": "?", "kind": "single", "options": ["a", "A"], "correct": [0], "points": 1}
    ]
    resp = await client.post(URL, json=paper, headers=auth_headers(settings, seed.admin))
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# Assigning
# --------------------------------------------------------------------------- #
async def test_reassigning_tops_up_rather_than_duplicating(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """One attempt per person per test. Re-assigning after a new joiner
    arrives is a normal thing to do."""
    aid = await _publish(client, settings, seed.admin)
    first = await client.post(
        f"{URL}/{aid}/assign",
        json={"audience": {"kind": "people", "employee_ids": [str(seed.report.id)]}},
        headers=auth_headers(settings, seed.admin),
    )
    assert first.json() == {"assigned": 1, "already_held": 0}

    again = await client.post(
        f"{URL}/{aid}/assign",
        json={
            "audience": {
                "kind": "people",
                "employee_ids": [str(seed.report.id), str(seed.outsider.id)],
            }
        },
        headers=auth_headers(settings, seed.admin),
    )

    assert again.json() == {"assigned": 1, "already_held": 1}
    mine = await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
    assert len(mine.json()) == 1, "not a second copy"


async def test_assigning_to_a_department_reaches_that_department(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    """The same audience shape as events and form sends."""
    seed.report.department = "Engineering"
    await db.commit()
    aid = await _publish(client, settings, seed.admin)

    resp = await client.post(
        f"{URL}/{aid}/assign",
        json={"audience": {"kind": "departments", "departments": ["engineering"]}},
        headers=auth_headers(settings, seed.admin),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["assigned"] >= 1
    assert (
        len(
            (
                await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
            ).json()
        )
        == 1
    )


# --------------------------------------------------------------------------- #
# History, for the profile
# --------------------------------------------------------------------------- #
async def test_your_history_carries_the_mark_and_the_pass_mark(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The profile shows "62% (pass mark 60)", so both travel with the row -
    and the pass mark is the one it was judged against."""
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)
    await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={"answers": []},
        headers=auth_headers(settings, seed.report),
    )

    rows = (
        await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
    ).json()

    assert len(rows) == 1
    assert rows[0]["assessment_title"] == "Data handling basics"
    assert rows[0]["pass_mark"] == 60
    assert rows[0]["percentage"] == 0
    assert rows[0]["status"] == "submitted"


async def test_nobody_reads_somebody_elses_history(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Not even their reporting manager: a test result sits closer to a
    payslip than to an attendance record."""
    aid = await _publish(client, settings, seed.admin)
    await _assign_to(client, settings, seed.admin, aid, seed.report)

    for actor in (seed.manager, seed.outsider):
        resp = await client.get(
            f"{URL}/attempts/employee/{seed.report.id}", headers=auth_headers(settings, actor)
        )
        assert resp.status_code == 404, f"{actor.full_name}: {resp.text}"


async def test_hr_reads_anyones_history(
    client: AsyncClient, settings: Settings, seed: _Seed, db: AsyncSession
) -> None:
    hr = await _add_hr(db)
    aid = await _publish(client, settings, seed.admin)
    await _assign_to(client, settings, seed.admin, aid, seed.report)

    resp = await client.get(
        f"{URL}/attempts/employee/{seed.report.id}", headers=auth_headers(settings, hr)
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_results_name_the_people_who_sat_it(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)
    await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={"answers": []},
        headers=auth_headers(settings, seed.report),
    )

    rows = (
        await client.get(f"{URL}/{aid}/results", headers=auth_headers(settings, seed.admin))
    ).json()

    assert len(rows) == 1
    assert rows[0]["employee_name"] == seed.report.full_name
    assert rows[0]["percentage"] == 0


async def test_the_list_counts_who_holds_it_and_who_has_sat_it(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    aid = await _publish(client, settings, seed.admin)
    await _assign_to(client, settings, seed.admin, aid, seed.report)
    await _assign_to(client, settings, seed.admin, aid, seed.outsider)
    mine = (
        await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
    ).json()
    await client.post(
        f"{URL}/attempts/{mine[0]['id']}/submit",
        json={"answers": []},
        headers=auth_headers(settings, seed.report),
    )

    rows = (await client.get(URL, headers=auth_headers(settings, seed.admin))).json()
    paper = next(r for r in rows if r["id"] == aid)

    assert paper["assigned_count"] == 2
    assert paper["submitted_count"] == 1
    assert paper["question_count"] == 2
    assert paper["total_points"] == 3


async def test_drafts_are_hidden_from_everyone_but_hr(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A draft is HR's working copy, and a half-written test on everyone's
    list is noise at best."""
    await client.post(URL, json=_paper(), headers=auth_headers(settings, seed.admin))

    theirs = (await client.get(URL, headers=auth_headers(settings, seed.report))).json()
    hrs = (await client.get(URL, headers=auth_headers(settings, seed.admin))).json()

    assert theirs == []
    assert len(hrs) == 1


# --------------------------------------------------------------------------- #
# Snapshots and leaks
# --------------------------------------------------------------------------- #
async def test_changing_the_pass_mark_does_not_rewrite_an_old_result(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """A history row has to agree with itself.

    Reading the assessment's CURRENT pass mark made an old row contradict
    its own verdict the moment HR moved the bar: "0%, pass mark 0%, Not
    passed". The mark it was judged against is snapshotted with the verdict.
    """
    aid = await _publish(client, settings, seed.admin)
    attempt_id = await _assign_to(client, settings, seed.admin, aid, seed.report)
    await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={"answers": []},
        headers=auth_headers(settings, seed.report),
    )

    # HR drops the bar to zero afterwards.
    lowered = await client.put(
        f"{URL}/{aid}",
        json={"title": "Data handling basics", "pass_mark": 0, "questions": []},
        headers=auth_headers(settings, seed.admin),
    )
    assert lowered.status_code == 200, lowered.text

    rows = (
        await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
    ).json()

    assert rows[0]["pass_mark"] == 60, "the bar it was actually judged against"
    assert rows[0]["passed"] is False, "and the verdict still agrees with it"


async def test_an_unsat_attempt_shows_the_current_pass_mark(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """Before it is sat the live mark is a preview of the bar, not a claim
    about a result - so it should move when HR moves it."""
    aid = await _publish(client, settings, seed.admin)
    await _assign_to(client, settings, seed.admin, aid, seed.report)
    await client.put(
        f"{URL}/{aid}",
        json={"title": "Data handling basics", "pass_mark": 80, "questions": []},
        headers=auth_headers(settings, seed.admin),
    )

    rows = (
        await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
    ).json()

    assert rows[0]["pass_mark"] == 80


async def test_the_assessment_list_never_carries_questions(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """`AssessmentRead` has no `questions` field, so the list cannot leak a
    key even to HR by accident - and HR is the one caller who gets a shape
    that does carry one."""
    await _publish(client, settings, seed.admin)

    rows = (await client.get(URL, headers=auth_headers(settings, seed.admin))).json()

    assert len(rows) == 1
    assert "questions" not in rows[0]


async def test_a_refused_question_edit_leaves_the_title_alone(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    """The guard runs before anything is written, so a 409 is not relying on
    a rollback to undo a half-applied update."""
    aid = await _publish(client, settings, seed.admin)
    changed = _paper()
    changed["title"] = "Renamed by a refused edit"

    refused = await client.put(
        f"{URL}/{aid}", json=changed, headers=auth_headers(settings, seed.admin)
    )
    assert refused.status_code == 409

    after = (await client.get(f"{URL}/{aid}", headers=auth_headers(settings, seed.admin))).json()
    assert after["title"] == "Data handling basics"


async def test_editing_a_drafts_questions_replaces_them(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    made = await client.post(URL, json=_paper(), headers=auth_headers(settings, seed.admin))
    aid = made.json()["id"]
    shorter = _paper()
    shorter["questions"] = [shorter["questions"][0]]  # type: ignore[index]

    resp = await client.put(
        f"{URL}/{aid}", json=shorter, headers=auth_headers(settings, seed.admin)
    )

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["questions"]) == 1, "replaced, not appended"
