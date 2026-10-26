"""The daily job that sends the two onboarding forms.

New joiner: their own form, a set number of days after they start. Buddy: a
form about the joiner, to their buddy, a set number of days after the joiner
starts. Pinned here: nobody who joined before the form went live is mailed,
nobody is ever sent the same form twice (re-runs, rejoiners), a missing buddy
never blocks anything, and a mail failure never undoes an assignment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.models.employee import Employee
from app.models.form import (
    AssignmentStatus,
    Form,
    FormAssignment,
    FormField,
    FormFieldType,
    FormStatus,
    FormTrigger,
)
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.form import FormAssignmentRepository, FormRepository, FormResponseRepository
from app.repositories.notification import NotificationRepository
from app.schemas.form import AnswerWrite
from app.services.form_answers import check_answers
from app.services.form_assignment_service import FormAssignmentService
from app.services.notification_service import NotificationService
from tests.conftest import _Seed, probation_link

TODAY = date(2026, 11, 20)
LIVE_SINCE = date(2026, 10, 1)
JOINER_DAYS = 15
BUDDY_DAYS = 30


class _RecordingEmail:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        # Whether each send asked for the HR mailbox copy, in send order.
        self.copied_hr: list[bool] = []
        self._fail = fail

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
        if self._fail:
            raise RuntimeError("smtp down")
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


@pytest.fixture(autouse=True)
async def _everyone_else_joined_long_ago(db: AsyncSession, seed: _Seed) -> None:
    """Only the joiner in each test is new; the rest of the seeded org started
    well before the forms went live."""
    for person in (seed.admin, seed.manager, seed.report, seed.outsider):
        person.hire_date = LIVE_SINCE - timedelta(days=365)
    await db.commit()


async def _live_form(db: AsyncSession, trigger: FormTrigger, days: int) -> Form:
    form = Form(
        family_id=uuid.uuid4(),
        title=f"{trigger.value} feedback",
        status=FormStatus.PUBLISHED,
        trigger=trigger,
        trigger_days=days,
        due_days=7,
        auto_assign_since=LIVE_SINCE,
        rating_labels=["1", "2", "3", "4", "5"],
        published_at=datetime.now(UTC),
    )
    db.add(form)
    await db.flush()
    db.add(FormField(form_id=form.id, position=0, type=FormFieldType.RATING, label="How was it?"))
    await db.commit()
    return form


async def _joined(
    db: AsyncSession, person: Employee, days_ago: int, buddy: Employee | None = None
) -> None:
    person.hire_date = TODAY - timedelta(days=days_ago)
    person.buddy_id = buddy.id if buddy else None
    await db.commit()


async def _assignments(db: AsyncSession) -> list[FormAssignment]:
    return list((await db.execute(select(FormAssignment))).scalars().all())


async def _run(
    db: AsyncSession, settings: Settings, email: _RecordingEmail, today: date = TODAY
) -> None:
    service = _service(db, settings, email)
    await service.run_due(today)
    await db.commit()
    await service.notify_due(today)
    await db.commit()
    await service.alert_missing_buddies()
    await db.commit()


async def test_joiner_gets_their_form_on_day_15_once(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.NEW_JOINER, JOINER_DAYS)
    await _joined(db, seed.report, days_ago=JOINER_DAYS - 1)
    email = _RecordingEmail()
    await _run(db, settings, email)
    assert await _assignments(db) == []  # day 14: not yet

    await _joined(db, seed.report, days_ago=JOINER_DAYS)
    await _run(db, settings, email)
    await _run(db, settings, email)  # a second tick sends nothing more
    [assignment] = await _assignments(db)
    assert assignment.assignee_id == seed.report.id == assignment.subject_id
    assert assignment.status is AssignmentStatus.PENDING
    assert assignment.notified_at is not None
    assert [to for to, _ in email.sent] == [seed.report.work_email]
    # HR holds a copy of every form sent out. Asserted here because the failure
    # mode is silent: the service swallows any exception from `send` and simply
    # reports the mail as failed, so a dropped or mistyped argument looks like
    # an outage rather than a bug.
    assert email.copied_hr == [True]


async def test_people_who_joined_before_go_live_are_left_alone(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.NEW_JOINER, JOINER_DAYS)
    seed.report.hire_date = LIVE_SINCE - timedelta(days=1)
    await db.commit()
    email = _RecordingEmail()
    await _run(db, settings, email)
    assert await _assignments(db) == []
    assert email.sent == []


async def test_no_joining_date_means_no_automatic_form(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """Existing staff added to Avora later (by invite) have no joining date and
    must not be mistaken for new joiners."""
    await _live_form(db, FormTrigger.NEW_JOINER, JOINER_DAYS)
    seed.report.hire_date = None
    await db.commit()
    await _run(db, settings, _RecordingEmail(), today=TODAY + timedelta(days=90))
    assert await _assignments(db) == []


async def test_rejoiner_is_never_sent_the_form_again(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.NEW_JOINER, JOINER_DAYS)
    await _joined(db, seed.report, days_ago=JOINER_DAYS)
    await _run(db, settings, _RecordingEmail())
    # Leaves and comes back with a fresh start date.
    await _joined(db, seed.report, days_ago=JOINER_DAYS + 1)
    await _run(db, settings, _RecordingEmail(), today=TODAY + timedelta(days=60))
    assert len(await _assignments(db)) == 1


async def test_buddy_gets_the_buddy_form_on_day_30(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.BUDDY, BUDDY_DAYS)
    await _joined(db, seed.report, days_ago=BUDDY_DAYS, buddy=seed.outsider)
    email = _RecordingEmail()
    await _run(db, settings, email)
    [assignment] = await _assignments(db)
    assert assignment.assignee_id == seed.outsider.id
    assert assignment.subject_id == seed.report.id
    assert [to for to, _ in email.sent] == [seed.outsider.work_email]


async def test_missing_buddy_waits_alerts_hr_once_then_goes_out(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.BUDDY, BUDDY_DAYS)
    await _joined(db, seed.report, days_ago=BUDDY_DAYS)
    email = _RecordingEmail()
    await _run(db, settings, email)
    [assignment] = await _assignments(db)
    assert assignment.status is AssignmentStatus.NEEDS_BUDDY
    assert assignment.assignee_id is None
    assert [to for to, _ in email.sent] == [seed.admin.work_email]  # HR/admin told

    await _run(db, settings, email)
    assert len(email.sent) == 1  # told once, not every tick

    seed.report.buddy_id = seed.outsider.id  # buddy arrives from Circle
    await db.commit()
    await _run(db, settings, email)
    await db.refresh(assignment)
    assert assignment.status is AssignmentStatus.PENDING
    assert assignment.assignee_id == seed.outsider.id
    assert email.sent[-1][0] == seed.outsider.work_email


async def test_buddy_who_has_left_is_treated_as_no_buddy(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.BUDDY, BUDDY_DAYS)
    await _joined(db, seed.report, days_ago=BUDDY_DAYS, buddy=seed.outsider)
    seed.outsider.is_active = False  # left; Circle no longer names them
    await db.commit()
    email = _RecordingEmail()
    await _run(db, settings, email)
    [assignment] = await _assignments(db)
    assert assignment.status is AssignmentStatus.NEEDS_BUDDY
    assert seed.outsider.work_email not in [to for to, _ in email.sent]


async def test_each_person_is_told_in_their_own_step(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """`notify_one` marks exactly one assignment, so the scheduler can commit
    after each and a crash never forgets who was already mailed."""
    await _live_form(db, FormTrigger.NEW_JOINER, JOINER_DAYS)
    await _joined(db, seed.report, days_ago=JOINER_DAYS)
    service = _service(db, settings, _RecordingEmail())
    await service.run_due(TODAY)
    await db.commit()
    [assignment_id] = await service.unnotified_ids(TODAY)
    assert await service.notify_one(assignment_id) is True
    assert await service.notify_one(assignment_id) is False  # already told
    assert await service.unnotified_ids(TODAY) == []


async def test_buddy_form_is_dropped_if_the_joiner_leaves(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.BUDDY, BUDDY_DAYS)
    await _joined(db, seed.report, days_ago=BUDDY_DAYS)
    await _run(db, settings, _RecordingEmail())
    seed.report.is_active = False
    await db.commit()
    await _run(db, settings, _RecordingEmail())
    [assignment] = await _assignments(db)
    assert assignment.status is AssignmentStatus.CANCELLED


async def test_mail_failure_keeps_the_assignment_and_retries(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.NEW_JOINER, JOINER_DAYS)
    await _joined(db, seed.report, days_ago=JOINER_DAYS)
    await _run(db, settings, _RecordingEmail(fail=True))
    [assignment] = await _assignments(db)
    assert assignment.notified_at is None  # kept, not yet told

    email = _RecordingEmail()
    await _run(db, settings, email)
    await db.refresh(assignment)
    assert assignment.notified_at is not None
    assert [to for to, _ in email.sent] == [seed.report.work_email]


def _field(
    kind: FormFieldType, *, required: bool = True, options: list[str] | None = None
) -> FormField:
    return FormField(
        id=uuid.uuid4(),
        form_id=uuid.uuid4(),
        position=0,
        type=kind,
        label="Q",
        required=required,
        options=options or [],
        with_comment=kind is FormFieldType.RATING,
    )


def test_answers_are_checked_against_the_questions() -> None:
    rating = _field(FormFieldType.RATING)
    choice = _field(FormFieldType.CHOICE, options=["Yes", "No"])
    note = _field(FormFieldType.PARAGRAPH, required=False)
    heading = _field(FormFieldType.SECTION, required=False)
    fields = [heading, rating, choice, note]

    rows = check_answers(
        fields,
        [
            AnswerWrite(field_id=rating.id, rating=3, text=" ok "),
            AnswerWrite(field_id=choice.id, choice="No"),
        ],
    )
    assert rows == [
        {"field_id": rating.id, "rating": 3, "text": "ok"},
        {"field_id": choice.id, "choice": "No"},
    ]
    with pytest.raises(ValidationError):
        check_answers(fields, [AnswerWrite(field_id=choice.id, choice="No")])  # rating missing
    with pytest.raises(ValidationError):
        check_answers(fields, [AnswerWrite(field_id=uuid.uuid4(), text="?")])  # not on the form


async def test_joiner_who_leaves_before_the_emails_gets_none_sent(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.NEW_JOINER, JOINER_DAYS)
    await _live_form(db, FormTrigger.BUDDY, BUDDY_DAYS)
    await _joined(db, seed.report, days_ago=BUDDY_DAYS, buddy=seed.outsider)
    service = _service(db, settings, _RecordingEmail())
    await service.run_due(TODAY)
    await db.commit()
    seed.report.is_active = False  # leaves before anyone is told
    await db.commit()
    email = _RecordingEmail()
    await _run(db, settings, email)
    statuses = {a.trigger: a.status for a in await _assignments(db)}
    assert statuses == {
        FormTrigger.NEW_JOINER: AssignmentStatus.CANCELLED,
        FormTrigger.BUDDY: AssignmentStatus.CANCELLED,
    }
    assert email.sent == []


async def test_buddy_who_leaves_before_the_email_is_replaced(
    db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _live_form(db, FormTrigger.BUDDY, BUDDY_DAYS)
    await _joined(db, seed.report, days_ago=BUDDY_DAYS, buddy=seed.outsider)
    service = _service(db, settings, _RecordingEmail())
    await service.run_due(TODAY)
    await db.commit()
    seed.outsider.is_active = False
    seed.report.buddy_id = seed.manager.id  # HR assigns a new buddy in Circle
    await db.commit()
    email = _RecordingEmail()
    await _run(db, settings, email)
    [assignment] = await _assignments(db)
    assert assignment.assignee_id == seed.manager.id
    assert [to for to, _ in email.sent] == [seed.manager.work_email]
