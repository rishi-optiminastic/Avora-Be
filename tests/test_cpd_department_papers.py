"""Department CPD papers: the department label, competencies, and an unkeyed draft.

Three things are worth pinning here.

**A draft may be unkeyed, a published test may not.** The Marketing paper
arrived from HR's document with no answer key at all, so the write schema has
to accept one. The safety net is `publish`, which refuses - because
`core.marking.is_right` cannot satisfy an empty key, so publishing an unkeyed
question would quietly mark every taker wrong.

**The competency reaches the taker; the key never does.** A competency is the
section heading somebody is already reading. The answer key is not, and the
existing suite proves that separately.

**The bundled papers are real data.** They are validated here against the
same schema the API uses, so a bad edit to a JSON file fails in CI rather
than at the moment HR tries to import it.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.cpd_papers import COMPETENCIES, QUESTIONS_PER_PAPER, load_papers, paper_paths
from app.models.employee import Employee, EmployeeStatus, Role
from tests.conftest import _Seed, auth_headers

URL = "/api/v1/assessments"


async def _add_hr(db: AsyncSession) -> Employee:
    hr = Employee(
        hr_external_id="hr-cpd-dept",
        work_email="hr-cpd-dept@corp.test",
        full_name="Holly HR",
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
    )
    db.add(hr)
    await db.commit()
    return hr


def _question(correct: list[int], competency: str | None = "Technical Knowledge") -> dict:
    return {
        "prompt": "Which sequence is right?",
        "kind": "single",
        "options": ["First then second", "Second then first", "Neither"],
        "correct": correct,
        "points": 1,
        "competency": competency,
    }


def _paper(*, department: str | None = "Tech", correct: list[int] | None = None) -> dict:
    return {
        "title": "CPD Assessment - Tech",
        "description": "20 questions.",
        "department": department,
        "pass_mark": 60,
        "questions": [_question([1] if correct is None else correct)],
    }


# --- the department label ------------------------------------------------- #
async def test_a_paper_remembers_the_department_it_was_written_for(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(), headers=auth_headers(settings, hr))
    assert made.status_code == 201, made.text
    assert made.json()["department"] == "Tech"

    listed = await client.get(URL, headers=auth_headers(settings, hr))
    assert listed.status_code == 200, listed.text
    rows = [r for r in listed.json() if r["id"] == made.json()["id"]]
    assert rows and rows[0]["department"] == "Tech", "the list groups on this"


async def test_a_paper_for_everybody_has_no_department(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """Nullable on purpose: not every test belongs to one department."""
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(department=None), headers=auth_headers(settings, hr))
    assert made.status_code == 201, made.text
    assert made.json()["department"] is None


async def test_the_department_can_be_corrected_on_a_published_paper(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """A label, not a scoping rule - so fixing a typo must not need an archive.

    The questions of a published test stay frozen; `update` is called with an
    empty question list, which is what means "leave the paper alone".
    """
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(), headers=auth_headers(settings, hr))
    aid = made.json()["id"]
    assert (
        await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, hr))
    ).status_code == 200

    renamed = {**_paper(department="Engineering"), "questions": []}
    out = await client.put(f"{URL}/{aid}", json=renamed, headers=auth_headers(settings, hr))
    assert out.status_code == 200, out.text
    assert out.json()["department"] == "Engineering"


# --- an unkeyed draft ------------------------------------------------------ #
async def test_a_draft_may_hold_a_question_with_no_answer_key(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """Marketing's paper arrived without one, so this has to be expressible."""
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(correct=[]), headers=auth_headers(settings, hr))
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["unkeyed_count"] == 1, "HR's list says what is still missing"
    assert body["questions"][0]["correct"] == []


async def test_publishing_an_unkeyed_paper_is_refused(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """The whole reason an empty key is allowed only in a draft: marking one
    scores everybody wrong, so it must never reach a taker."""
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(correct=[]), headers=auth_headers(settings, hr))
    aid = made.json()["id"]

    out = await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, hr))
    assert out.status_code == 409, out.text
    assert "answer key" in out.text.lower()
    assert "1" in out.text, "it names the question still to be keyed"


async def test_keying_the_question_then_publishes(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """The fix HR applies in the composer, end to end."""
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(correct=[]), headers=auth_headers(settings, hr))
    aid = made.json()["id"]

    fixed = await client.put(f"{URL}/{aid}", json=_paper(), headers=auth_headers(settings, hr))
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["unkeyed_count"] == 0

    live = await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, hr))
    assert live.status_code == 200, live.text
    assert live.json()["status"] == "published"


async def test_a_key_that_points_at_no_option_is_still_refused(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """Allowing an EMPTY key must not have loosened a WRONG one."""
    hr = await _add_hr(db)
    out = await client.post(URL, json=_paper(correct=[9]), headers=auth_headers(settings, hr))
    assert out.status_code == 422, out.text


# --- competency ------------------------------------------------------------ #
async def test_the_taker_sees_the_competency_but_never_the_key(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(), headers=auth_headers(settings, hr))
    aid = made.json()["id"]
    assert (
        await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, hr))
    ).status_code == 200
    sent = await client.post(
        f"{URL}/{aid}/assign",
        json={"audience": {"kind": "people", "employee_ids": [str(seed.report.id)]}},
        headers=auth_headers(settings, hr),
    )
    assert sent.status_code == 201, sent.text

    mine = await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
    attempt_id = mine.json()[0]["id"]
    paper = await client.get(
        f"{URL}/attempts/{attempt_id}", headers=auth_headers(settings, seed.report)
    )
    assert paper.status_code == 200, paper.text
    question = paper.json()["questions"][0]
    assert question["competency"] == "Technical Knowledge"
    assert "correct" not in question, "the key must not travel with the paper"


async def test_the_review_after_marking_names_the_competency(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """What a per-competency breakdown on the result screen is built from."""
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(), headers=auth_headers(settings, hr))
    aid = made.json()["id"]
    await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, hr))
    await client.post(
        f"{URL}/{aid}/assign",
        json={"audience": {"kind": "people", "employee_ids": [str(seed.report.id)]}},
        headers=auth_headers(settings, hr),
    )
    mine = await client.get(f"{URL}/attempts/me", headers=auth_headers(settings, seed.report))
    attempt_id = mine.json()[0]["id"]
    question_id = (
        await client.get(
            f"{URL}/attempts/{attempt_id}", headers=auth_headers(settings, seed.report)
        )
    ).json()["questions"][0]["id"]

    done = await client.post(
        f"{URL}/attempts/{attempt_id}/submit",
        json={"answers": [{"question_id": question_id, "chosen": [1]}]},
        headers=auth_headers(settings, seed.report),
    )
    assert done.status_code == 200, done.text
    answer = done.json()["answers"][0]
    assert answer["competency"] == "Technical Knowledge"
    assert answer["is_correct"] is True


async def test_a_blank_competency_is_stored_as_nothing(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """Whitespace from a pasted import must not become a section heading."""
    hr = await _add_hr(db)
    paper = {**_paper(), "questions": [_question([1], competency="   ")]}
    made = await client.post(URL, json=paper, headers=auth_headers(settings, hr))
    assert made.status_code == 201, made.text
    assert made.json()["questions"][0]["competency"] is None


# --- the bundled papers ---------------------------------------------------- #
def test_every_bundled_paper_is_valid_and_shaped_as_the_document_says() -> None:
    """Guards the JSON in `app/data/cpd`: a bad edit fails here, not on import."""
    papers = load_papers()
    assert len(papers) == len(paper_paths()) == 7

    departments = {p.department for p in papers}
    assert departments == {
        "Marketing",
        "Tech",
        "Finance",
        "Game Development",
        "HR",
        "3D Designer",
        "Motion & Graphic Designer",
    }

    for paper in papers:
        assert paper.department, f"{paper.title} must name its department"
        assert paper.title == f"CPD Assessment - {paper.department}"
        assert paper.pass_mark == 60, "60% is L3 Competent on the document's own scale"
        assert len(paper.questions) == QUESTIONS_PER_PAPER

        split: dict[str, int] = {}
        for question in paper.questions:
            assert question.competency in COMPETENCIES, question.competency
            assert len(question.options) == 4
            assert question.points == 1
            split[str(question.competency)] = split.get(str(question.competency), 0) + 1
        assert split == {
            "Technical Knowledge": 8,
            "Problem Solving": 3,
            "Communication": 3,
            "Interpersonal Effectiveness": 3,
            "Cultural Alignment & Ownership": 3,
        }, f"{paper.department}: {split}"


def test_only_the_marketing_paper_is_unkeyed() -> None:
    """The source document carried a key for every paper but Marketing.

    Asserted both ways round: if a key is ever added for Marketing this test
    should fail and be updated, and if a key goes MISSING from another paper
    it fails too - which would otherwise only surface as a refused publish.
    """
    unkeyed = {
        str(paper.department): sum(1 for q in paper.questions if not q.correct)
        for paper in load_papers()
    }
    assert unkeyed.pop("Marketing") == QUESTIONS_PER_PAPER
    assert set(unkeyed.values()) == {0}, unkeyed


def test_every_key_points_at_a_real_option() -> None:
    """Cheap, and the one transcription error that would silently mismark."""
    for paper in load_papers():
        for n, question in enumerate(paper.questions, start=1):
            for index in question.correct:
                assert 0 <= index < len(question.options), f"{paper.department} Q{n}"


async def test_an_ordinary_employee_never_sees_an_unkeyed_draft(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """CLAUDE.md §9. Drafts are HR's working copies, and the unkeyed count is
    HR's to act on - so the list must not carry either to a taker."""
    hr = await _add_hr(db)
    made = await client.post(URL, json=_paper(correct=[]), headers=auth_headers(settings, hr))
    assert made.status_code == 201, made.text

    listed = await client.get(URL, headers=auth_headers(settings, seed.report))
    assert listed.status_code == 200, listed.text
    assert all(row["id"] != made.json()["id"] for row in listed.json())
    assert all(row["unkeyed_count"] == 0 for row in listed.json())

    denied = await client.post(URL, json=_paper(), headers=auth_headers(settings, seed.report))
    assert denied.status_code == 403, denied.text


async def test_every_bundled_paper_imports_and_only_marketing_cannot_publish(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """The seeding script's path, end to end, against a real database.

    `scripts/seed_cpd_assessments.py` calls the same service these requests
    reach, so this proves the bundled JSON survives the schema, the service
    and the publish guard - the three places a transcription problem would
    otherwise surface for the first time on somebody's production import.
    """
    hr = await _add_hr(db)
    created: dict[str, str] = {}
    for paper in load_papers():
        made = await client.post(
            URL,
            json=paper.model_dump(mode="json"),
            headers=auth_headers(settings, hr),
        )
        assert made.status_code == 201, f"{paper.department}: {made.text}"
        body = made.json()
        assert body["department"] == paper.department
        assert body["question_count"] == QUESTIONS_PER_PAPER
        assert body["total_points"] == QUESTIONS_PER_PAPER
        created[str(paper.department)] = body["id"]

    assert len(created) == 7

    for department, aid in created.items():
        out = await client.post(f"{URL}/{aid}/publish", headers=auth_headers(settings, hr))
        if department == "Marketing":
            assert out.status_code == 409, "it has no answer key, so nobody could pass it"
            assert "answer key" in out.text.lower()
        else:
            assert out.status_code == 200, f"{department}: {out.text}"
            assert out.json()["status"] == "published"


async def test_re_importing_a_paper_is_what_the_script_skips_on(
    client: AsyncClient, db: AsyncSession, settings: Settings
) -> None:
    """The seeder's idempotency rests on the title being stable and unique
    per department, so that is what is pinned here."""
    titles = [paper.title for paper in load_papers()]
    assert len(set(titles)) == len(titles), "two papers share a title, so one would be skipped"

    hr = await _add_hr(db)
    tech = next(p for p in load_papers() if p.department == "Tech")
    made = await client.post(
        URL, json=tech.model_dump(mode="json"), headers=auth_headers(settings, hr)
    )
    assert made.status_code == 201, made.text

    listed = await client.get(URL, headers=auth_headers(settings, hr))
    held = {row["title"] for row in listed.json()}
    assert tech.title in held, "the script reads this set to decide what to skip"
