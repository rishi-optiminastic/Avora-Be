"""The three probation review forms.

Pinned here: when they go out (21 days before the probation end date, never
after it, following an extension), the manager form waiting for a manager,
the shared HR form (any HR/admin, first to submit claims it), weighted
scores, the checklist ticks Avora makes, and who may see what.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tenure import probation_end
from app.models.employee import Employee, EmployeeStatus, Role
from app.models.form import (
    AssignmentStatus,
    Form,
    FormAssignment,
    FormField,
    FormFieldType,
    FormStatus,
    FormTrigger,
)
from app.models.probation_checklist import ProbationChecklistItem, ProbationStepStatus
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.form import FormAssignmentRepository, FormRepository, FormResponseRepository
from app.repositories.notification import NotificationRepository
from app.repositories.probation_checklist import SYSTEM_NOTE
from app.schemas.form import AnswerRead, FormFieldWrite, FormWrite
from app.services.form_assignment_service import FormAssignmentService
from app.services.form_scoring import score_answers, weighted_score
from app.services.notification_service import NotificationService
from app.services.probation_forms import is_review_due
from tests.conftest import _Seed, auth_headers, probation_link

LEAD_DAYS = 21
MONTHS = 6
_LABELS = ["1", "2", "3", "4", "5"]


class _RecordingEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        # Whether each send asked for the HR mailbox copy, in send order.
        self.copied_hr: list[bool] = []

    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        cc: list[str] | None = None,
        attachments: list[object] | None = None,
        copy_hr: bool = False,
    ) -> None:
        del html, cc, attachments
        self.copied_hr.append(copy_hr)
        self.sent.append((to, subject))


def _service(db: AsyncSession, settings: Settings, email: _RecordingEmail) -> FormAssignmentService:
    return FormAssignmentService(
        FormRepository(db),
        FormAssignmentRepository(db),
        FormResponseRepository(db),
        EmployeeRepository(db),
        NotificationService(NotificationRepository(db)),
        email,  # type: ignore[arg-type]
        AuditRepository(db),
        settings,
        probation_link(db),
    )


async def _today(db: AsyncSession) -> date:
    return await probation_link(db).org_today()


async def _live_form(
    db: AsyncSession, trigger: FormTrigger, *, weights: tuple[int, ...] = ()
) -> Form:
    form = Form(
        family_id=uuid.uuid4(),
        title=f"{trigger.value} review",
        status=FormStatus.PUBLISHED,
        trigger=trigger,
        trigger_days=LEAD_DAYS,
        due_days=7,
        auto_assign_since=date(2020, 1, 1),
        rating_labels=_LABELS,
        published_at=datetime.now(UTC),
    )
    db.add(form)
    await db.flush()
    for position, weight in enumerate(weights or (None,)):
        db.add(
            FormField(
                form_id=form.id,
                position=position,
                type=FormFieldType.RATING,
                label=f"Criterion {position}",
                weight=weight,
            )
        )
    await db.commit()
    return form


async def _ends_in(db: AsyncSession, person: Employee, days: int) -> date:
    """Give `person` a joining date whose 6-month probation ends `days` from
    the org's today (or as close as month arithmetic allows); return the end."""
    today = await _today(db)
    hired = today - timedelta(days=183) + timedelta(days=days)
    while probation_end(hired, MONTHS) > today + timedelta(days=days):
        hired -= timedelta(days=1)
    while probation_end(hired, MONTHS) < today + timedelta(days=days):
        hired += timedelta(days=1)
    person.hire_date = hired
    person.probation_months = MONTHS
    await db.commit()
    return probation_end(hired, MONTHS)


@pytest.fixture(autouse=True)
async def _everyone_else_is_confirmed(db: AsyncSession, seed: _Seed) -> None:
    for person in (seed.admin, seed.manager, seed.report, seed.outsider):
        person.hire_date = date(2020, 1, 1)
    await db.commit()


async def _hr(db: AsyncSession, name: str) -> Employee:
    person = Employee(
        hr_external_id=f"hr-{name}",
        work_email=f"{name}@corp.test",
        full_name=name.title(),
        role=Role.HR,
        status=EmployeeStatus.ACTIVE,
        is_active=True,
        hire_date=date(2020, 1, 1),
    )
    db.add(person)
    await db.commit()
    return person


async def _assignments(db: AsyncSession) -> list[FormAssignment]:
    rows = await db.execute(select(FormAssignment).execution_options(populate_existing=True))
    return list(rows.scalars().all())


async def _run(db: AsyncSession, settings: Settings, email: _RecordingEmail, today: date) -> None:
    service = _service(db, settings, email)
    await service.run_due(today)
    await db.commit()
    await service.notify_due(today)
    await db.commit()
    await service.alert_missing_managers()
    await db.commit()


async def _steps(db: AsyncSession, employee: Employee) -> dict[int, ProbationChecklistItem]:
    rows = await db.execute(
        select(ProbationChecklistItem)
        .where(ProbationChecklistItem.employee_id == employee.id)
        .execution_options(populate_existing=True)
    )
    return {r.step_no: r for r in rows.scalars().all()}


def _answers(detail: dict, rating: int = 4) -> dict:
    return {
        "answers": [
            {"field_id": f["id"], "rating": rating}
            for f in detail["form"]["fields"]
            if f["type"] == "rating"
        ]
    }


# --- the window ------------------------------------------------------------ #
async def test_window_opens_21_days_before_the_end_and_closes_at_it(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.PROBATION_SELF)
    ends = await _ends_in(db, seed.report, 30)
    email = _RecordingEmail()

    await _run(db, settings, email, ends - timedelta(days=LEAD_DAYS + 1))
    assert await _assignments(db) == []  # before the window

    await _run(db, settings, email, ends - timedelta(days=LEAD_DAYS))
    [assignment] = await _assignments(db)
    assert assignment.assignee_id == seed.report.id == assignment.subject_id
    assert assignment.due_on is not None and assignment.due_on < ends
    assert [to for to, _ in email.sent] == [seed.report.work_email]


async def test_nobody_whose_probation_ended_is_sent_the_forms(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.PROBATION_SELF)
    ends = await _ends_in(db, seed.report, 30)
    email = _RecordingEmail()
    await _run(db, settings, email, ends)  # the end date itself: confirmed
    await _run(db, settings, email, ends + timedelta(days=5))
    assert await _assignments(db) == []
    assert email.sent == []


async def test_extension_moves_the_window(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.PROBATION_SELF)
    ends = await _ends_in(db, seed.report, 30)
    today = ends + timedelta(days=10)  # the original 6 months are over
    await _run(db, settings, _RecordingEmail(), today)
    assert await _assignments(db) == []

    seed.report.probation_months = MONTHS + 1  # extended by a month
    await db.commit()
    extended = probation_end(seed.report.hire_date or today, MONTHS + 1)
    assert extended - timedelta(days=LEAD_DAYS) <= today < extended
    await _run(db, settings, _RecordingEmail(), today)
    assert len(await _assignments(db)) == 1


def test_go_live_date_is_compared_to_the_end_date() -> None:
    form = Form(trigger_days=LEAD_DAYS, auto_assign_since=date(2026, 10, 1))
    person = Employee(is_active=True)
    today = date(2026, 10, 5)
    assert is_review_due(person, date(2026, 10, 10), form, today)
    assert not is_review_due(person, date(2026, 9, 30), form, today)
    assert not is_review_due(person, None, form, today)


# --- the manager form ----------------------------------------------------- #
async def test_missing_manager_waits_alerts_hr_once_then_goes_out(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.PROBATION_MANAGER)
    ends = await _ends_in(db, seed.outsider, 10)  # outsider has no manager
    today = await _today(db)
    email = _RecordingEmail()
    await _run(db, settings, email, today)
    [assignment] = await _assignments(db)
    assert assignment.status is AssignmentStatus.NEEDS_MANAGER
    assert assignment.assignee_id is None
    assert [to for to, _ in email.sent] == [seed.admin.work_email]
    steps = await _steps(db, seed.outsider)
    assert 1 in steps and 4 not in steps  # started, but not shared with anyone yet

    await _run(db, settings, email, today)
    assert len(email.sent) == 1  # HR told once

    seed.outsider.manager_id = seed.manager.id
    await db.commit()
    await _run(db, settings, email, today)
    [assignment] = await _assignments(db)
    assert assignment.status is AssignmentStatus.PENDING
    assert assignment.assignee_id == seed.manager.id
    assert email.sent[-1][0] == seed.manager.work_email
    assert (await _steps(db, seed.outsider))[4].status is ProbationStepStatus.DONE
    assert ends > today


# --- checklist ------------------------------------------------------------ #
async def test_checklist_ticks_never_overwrite_a_person(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    for trigger in (FormTrigger.PROBATION_SELF, FormTrigger.PROBATION_MANAGER):
        await _live_form(db, trigger)
    await _ends_in(db, seed.report, 10)
    # HR already skipped step 2 by hand.
    db.add(
        ProbationChecklistItem(
            employee_id=seed.report.id,
            step_no=2,
            status=ProbationStepStatus.SKIPPED,
            actor_id=seed.admin.id,
            note="Done on paper",
        )
    )
    await db.commit()
    await _run(db, settings, _RecordingEmail(), await _today(db))
    steps = await _steps(db, seed.report)
    assert steps[1].status is ProbationStepStatus.DONE and steps[1].actor_id is None
    assert (steps[1].note or "").startswith(SYSTEM_NOTE)
    assert steps[2].status is ProbationStepStatus.SKIPPED and steps[2].actor_id == seed.admin.id
    assert steps[4].status is ProbationStepStatus.DONE

    by_trigger = {a.trigger: a for a in await _assignments(db)}
    self_id = by_trigger[FormTrigger.PROBATION_SELF].id
    detail = (
        await client.get(
            f"/api/v1/forms/assignments/{self_id}", headers=auth_headers(settings, seed.report)
        )
    ).json()
    res = await client.post(
        f"/api/v1/forms/assignments/{self_id}/submit",
        json=_answers(detail),
        headers=auth_headers(settings, seed.report),
    )
    assert res.status_code == 200, res.text
    assert (await _steps(db, seed.report))[3].status is ProbationStepStatus.DONE

    manager_id = by_trigger[FormTrigger.PROBATION_MANAGER].id
    detail = (
        await client.get(
            f"/api/v1/forms/assignments/{manager_id}", headers=auth_headers(settings, seed.manager)
        )
    ).json()
    res = await client.post(
        f"/api/v1/forms/assignments/{manager_id}/submit",
        json=_answers(detail),
        headers=auth_headers(settings, seed.manager),
    )
    assert res.status_code == 200, res.text
    steps = await _steps(db, seed.report)
    assert steps[5].status is steps[6].status is ProbationStepStatus.DONE


# --- the HR pool ---------------------------------------------------------- #
async def test_hr_pool_first_submit_claims_it(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    await _live_form(db, FormTrigger.PROBATION_HR)
    await _ends_in(db, seed.report, 10)
    first, second = await _hr(db, "harriet"), await _hr(db, "hugo")
    email = _RecordingEmail()
    await _run(db, settings, email, await _today(db))
    [assignment] = await _assignments(db)
    assert assignment.assignee_id is None and assignment.status is AssignmentStatus.PENDING
    told = sorted(to for to, _ in email.sent)
    assert told == sorted([seed.admin.work_email, first.work_email, second.work_email])
    await _run(db, settings, email, await _today(db))
    assert len(email.sent) == len(told)  # told once

    for person in (first, second, seed.admin):
        mine = await client.get(
            "/api/v1/forms/assignments/mine", headers=auth_headers(settings, person)
        )
        assert [a["id"] for a in mine.json()] == [str(assignment.id)]
    for person in (seed.report, seed.manager):  # not HR: never sees it
        mine = await client.get(
            "/api/v1/forms/assignments/mine", headers=auth_headers(settings, person)
        )
        assert mine.json() == []
        res = await client.get(
            f"/api/v1/forms/assignments/{assignment.id}", headers=auth_headers(settings, person)
        )
        assert res.status_code == 404

    url = f"/api/v1/forms/assignments/{assignment.id}"
    detail = (await client.get(url, headers=auth_headers(settings, first))).json()
    assert detail["subject_details"]["employee_name"] == seed.report.full_name
    res = await client.post(
        f"{url}/submit", json=_answers(detail), headers=auth_headers(settings, first)
    )
    assert res.status_code == 200, res.text
    assert res.json()["assignee_id"] == str(first.id)

    res = await client.post(
        f"{url}/submit", json=_answers(detail), headers=auth_headers(settings, second)
    )
    assert res.status_code in (404, 409)
    mine = await client.get(
        "/api/v1/forms/assignments/mine", headers=auth_headers(settings, second)
    )
    assert mine.json() == []
    steps = await _steps(db, seed.report)
    assert steps[8].status is steps[9].status is ProbationStepStatus.DONE


async def test_hr_never_handles_their_own_hr_review(
    db: AsyncSession, settings: Settings, client: AsyncClient
) -> None:
    await _live_form(db, FormTrigger.PROBATION_HR)
    probationer = await _hr(db, "petra")
    await _ends_in(db, probationer, 10)
    await _run(db, settings, _RecordingEmail(), await _today(db))
    [assignment] = await _assignments(db)
    headers = auth_headers(settings, probationer)
    assert (await client.get("/api/v1/forms/assignments/mine", headers=headers)).json() == []
    res = await client.get(f"/api/v1/forms/assignments/{assignment.id}", headers=headers)
    assert res.status_code == 404


# --- weights -------------------------------------------------------------- #
def test_weight_only_on_rating_questions() -> None:
    with pytest.raises(ValueError, match="Only rating questions"):
        FormFieldWrite(type=FormFieldType.PARAGRAPH, label="Why?", weight=10)
    with pytest.raises(ValueError):
        FormFieldWrite(type=FormFieldType.RATING, label="Skill", weight=0)
    assert FormFieldWrite(type=FormFieldType.RATING, label="Skill", weight=20).weight == 20


def test_probation_trigger_days_count_back_from_the_end() -> None:
    with pytest.raises(ValueError, match="before the probation end date"):
        FormWrite(
            title="Self",
            trigger=FormTrigger.PROBATION_SELF,
            fields=[FormFieldWrite(type=FormFieldType.PARAGRAPH, label="How was it?")],
        )


def test_weighted_scores() -> None:
    assert weighted_score(4, 20) == 16.0
    assert weighted_score(None, 20) is None
    a, b = uuid.uuid4(), uuid.uuid4()
    scored, total = score_answers(
        {a: 60, b: 40},
        [
            AnswerRead(field_id=a, rating=5, text=None, choice=None),
            AnswerRead(field_id=b, rating=3, text=None, choice=None),
        ],
    )
    assert [s.weighted_score for s in scored] == [60.0, 24.0]
    assert total == 84.0
    assert score_answers({}, [])[1] is None


async def test_publish_refuses_weights_that_do_not_add_up(
    client: AsyncClient, settings: Settings, seed: _Seed
) -> None:
    headers = auth_headers(settings, seed.admin)

    def body(weights: list[int]) -> dict:
        return {
            "title": "Manager evaluation",
            "rating_labels": _LABELS,
            "trigger": "probation_manager",
            "trigger_days": LEAD_DAYS,
            "fields": [
                {"type": "rating", "label": f"Criterion {i}", "weight": w}
                for i, w in enumerate(weights)
            ],
        }

    form = (await client.post("/api/v1/forms", json=body([60, 30]), headers=headers)).json()
    res = await client.post(f"/api/v1/forms/{form['id']}/publish", headers=headers)
    assert res.status_code == 422
    assert "90%" in res.text

    res = await client.put(f"/api/v1/forms/{form['id']}", json=body([60, 40]), headers=headers)
    assert [f["weight"] for f in res.json()["fields"]] == [60, 40]
    res = await client.post(f"/api/v1/forms/{form['id']}/publish", headers=headers)
    assert res.status_code == 200, res.text


# --- the probation page panel and scope ----------------------------------- #
async def test_panel_scores_and_scope(
    db: AsyncSession, settings: Settings, seed: _Seed, client: AsyncClient
) -> None:
    await _live_form(db, FormTrigger.PROBATION_MANAGER, weights=(60, 40))
    await _live_form(db, FormTrigger.PROBATION_SELF)
    await _ends_in(db, seed.report, 10)
    await _run(db, settings, _RecordingEmail(), await _today(db))
    by_trigger = {a.trigger: a for a in await _assignments(db)}
    url = f"/api/v1/forms/assignments/{by_trigger[FormTrigger.PROBATION_MANAGER].id}"
    manager = auth_headers(settings, seed.manager)
    detail = (await client.get(url, headers=manager)).json()
    assert detail["subject_details"]["reporting_manager"] == seed.manager.full_name
    assert detail["subject_details"]["probation_end_date"] is not None
    res = await client.post(f"{url}/submit", json=_answers(detail, rating=5), headers=manager)
    assert res.json()["weighted_total"] == 100.0

    # Nobody outside it sees the form or the facts about the probationer.
    outsider = auth_headers(settings, seed.outsider)
    assert (await client.get(url, headers=outsider)).status_code == 404
    panel = f"/api/v1/forms/probation/{seed.report.id}"
    assert (await client.get(panel, headers=outsider)).status_code == 404

    admin = (await client.get(panel, headers=auth_headers(settings, seed.admin))).json()
    rows = {f["trigger"]: f for f in admin["forms"]}
    assert rows["probation_manager"]["weighted_total"] == 100.0
    assert rows["probation_manager"]["can_open"] is True
    assert rows["probation_hr"]["status"] is None  # no HR form live

    own = (await client.get(panel, headers=manager)).json()
    rows = {f["trigger"]: f for f in own["forms"]}
    assert rows["probation_manager"]["can_open"] is True  # their own submission
    assert rows["probation_self"]["can_open"] is False  # not theirs to read
    assert rows["probation_self"]["weighted_total"] is None

    results = await client.get(
        f"/api/v1/forms/{by_trigger[FormTrigger.PROBATION_MANAGER].form_id}/results",
        headers=auth_headers(settings, seed.admin),
    )
    [response] = results.json()["responses"]
    assert response["weighted_total"] == 100.0
    assert response["subject_details"]["employee_name"] == seed.report.full_name


def test_seeded_probation_forms_are_valid_drafts() -> None:
    """The seed script's three forms pass the same validation HR's would, and
    the manager's weights add up to 100 so it can be published as is."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "seed_probation_forms.py"
    spec = importlib.util.spec_from_file_location("seed_probation_forms", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    triggers = []
    for definition in module.FORMS:
        form = FormWrite.model_validate(definition)
        assert form.trigger_days == LEAD_DAYS
        triggers.append(form.trigger)
        weights = [f.weight for f in form.fields if f.weight is not None]
        assert not weights or sum(weights) == 100
    assert set(triggers) == {
        FormTrigger.PROBATION_SELF,
        FormTrigger.PROBATION_MANAGER,
        FormTrigger.PROBATION_HR,
    }
