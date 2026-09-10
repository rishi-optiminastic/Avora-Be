"""Probation outcomes and the letters that go with them.

The wording is HR's, supplied verbatim, so these tests pin the parts that would
be embarrassing to get wrong in a letter about someone's employment: the name,
the title, the date, and - the easy one to ship broken - never claiming a letter
is attached when nothing is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.employee import Employee
from app.models.probation_decision import ProbationDecision, ProbationOutcome
from app.services.email_templates import (
    probation_confirmation_email,
    probation_extension_email,
)
from tests.conftest import _Seed, auth_headers

_PDF = b"%PDF-1.4\n%fake pdf for the attachment path\n"


async def _probationer(db: AsyncSession, seed: _Seed) -> None:
    person = await db.get(Employee, seed.report.id)
    assert person is not None
    person.hire_date = datetime.now(UTC).date() - timedelta(days=30)
    person.job_title = "Software Engineer"
    await db.commit()


async def _decide(
    client: AsyncClient,
    settings: Settings,
    caller: Employee,
    target: Employee,
    data: dict[str, str],
    files: dict[str, tuple[str, bytes, str]] | None = None,
):
    return await client.post(
        f"/api/v1/probation/{target.id}/decision",
        headers=auth_headers(settings, caller),
        data=data,
        files=files,
    )


# --- the letters themselves -------------------------------------------------- #
def test_confirmation_letter_carries_the_name_title_and_date() -> None:
    subject, html = probation_confirmation_email(
        employee_name="Remy Report",
        job_title="Software Engineer",
        effective_label="27 October 2026",
        has_letter=False,
    )
    assert "probation is confirmed" in subject
    assert "Remy Report" in html
    assert "Software Engineer" in html
    assert "27 October 2026" in html
    assert "30 days" in html  # the notice period the letter promises
    assert "glassdoor" in html.lower()


def test_a_letter_is_only_mentioned_when_one_is_attached() -> None:
    """Shipping "attached to this email" with no attachment is the obvious bug
    here, and the reader cannot tell it was our mistake rather than a lost file."""
    without = probation_confirmation_email(
        employee_name="Remy", job_title="Engineer", effective_label="1 Jan 2027", has_letter=False
    )[1]
    with_letter = probation_confirmation_email(
        employee_name="Remy", job_title="Engineer", effective_label="1 Jan 2027", has_letter=True
    )[1]
    assert "attached" not in without.lower()
    assert "Probation Confirmation Letter is attached" in with_letter

    ext_without = probation_extension_email(
        employee_name="Remy", new_end_label="1 Mar 2027", has_letter=False
    )[1]
    ext_with = probation_extension_email(
        employee_name="Remy", new_end_label="1 Mar 2027", has_letter=True
    )[1]
    assert "attached" not in ext_without.lower()
    assert "Extension Letter attached" in ext_with


def test_letters_escape_the_name() -> None:
    html = probation_confirmation_email(
        employee_name="<script>alert(1)</script>",
        job_title="Engineer",
        effective_label="1 Jan 2027",
        has_letter=False,
    )[1]
    assert "<script>" not in html


# --- recording the outcome --------------------------------------------------- #
async def test_hr_can_confirm_and_the_letter_is_marked_sent(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27"},
    )

    assert res.status_code == 200
    body = res.json()["decision"]
    assert body["outcome"] == "confirmed"
    assert body["effective_date"] == "2026-10-27"
    assert body["job_title"] == "Software Engineer"  # snapshotted from the profile
    assert body["decided_by_name"] == seed.admin.full_name
    assert body["letter_sent_at"] is not None


async def test_the_outcome_is_recorded_even_when_no_letter_is_sent(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "extended", "effective_date": "2027-01-27", "send_letter": "false"},
    )

    assert res.status_code == 200
    body = res.json()["decision"]
    assert body["outcome"] == "extended"
    assert body["letter_sent_at"] is None  # nothing went out, and it says so


async def test_termination_records_but_sends_nothing(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """HR supplied no termination wording, so Avora must not invent a dismissal
    letter. The outcome is still recorded."""
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "terminated", "effective_date": "2026-11-30"},
    )

    assert res.status_code == 200
    body = res.json()["decision"]
    assert body["outcome"] == "terminated"
    assert body["letter_sent_at"] is None


async def test_re_deciding_replaces_and_clears_the_sent_stamp(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """A changed outcome has not been communicated yet, whatever the last one was."""
    await _probationer(db, seed)
    await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27"},
    )

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "extended", "effective_date": "2027-01-27", "send_letter": "false"},
    )

    assert res.status_code == 200
    assert res.json()["decision"]["letter_sent_at"] is None
    rows = (
        (
            await db.execute(
                select(ProbationDecision).where(ProbationDecision.employee_id == seed.report.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # replaced, not stacked
    assert rows[0].outcome is ProbationOutcome.EXTENDED


# --- authorization ----------------------------------------------------------- #
async def test_a_manager_cannot_issue_the_outcome(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The manager contributes to the decision, but the letter is a company
    communication about someone's employment - that is people-ops."""
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.manager,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27"},
    )

    assert res.status_code == 403


async def test_an_outsider_gets_nothing(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.outsider,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27"},
    )

    assert res.status_code == 404


async def test_the_employee_cannot_confirm_themselves(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.report,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27"},
    )

    assert res.status_code == 403


# --- the attachment ---------------------------------------------------------- #
async def test_a_pdf_letter_is_accepted(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27"},
        files={"letter": ("confirmation.pdf", _PDF, "application/pdf")},
    )

    assert res.status_code == 200
    assert res.json()["decision"]["letter_sent_at"] is not None


async def test_a_non_pdf_letter_is_rejected_on_its_CONTENT(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    """The browser's content-type is a claim. This one lies, and is still caught."""
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27"},
        files={"letter": ("evil.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")},
    )

    assert res.status_code == 422
    assert (
        await db.scalar(
            select(ProbationDecision).where(ProbationDecision.employee_id == seed.report.id)
        )
    ) is None  # rejected before anything was recorded


def test_effective_date_is_a_real_date() -> None:
    assert date.fromisoformat("2026-10-27") == date(2026, 10, 27)
