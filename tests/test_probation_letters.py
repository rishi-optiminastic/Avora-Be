"""The generated probation letters: HR's templates, filled and attached.

Pins what matters in a letter about someone's employment: it renders, it names
the right person, title and dates, hostile text cannot restyle or break it, an
extension cannot go out without the expectations its template promises, HR's
own upload still wins, and only HR/Admin can download it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from reportlab.platypus import KeepTogether, Paragraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.probation_letter_pdf import (
    COMPANY_LEGAL_NAME,
    ConfirmationLetter,
    ExtensionLetter,
    confirmation_story,
    extension_story,
    render_confirmation_letter,
    render_extension_letter,
)
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.probation_decision import ProbationDecision, ProbationOutcome
from app.schemas.probation import ProbationDecisionCreate
from app.services.email_service import EmailAttachment
from tests.conftest import _FakeEmailService, _Seed, auth_headers

_PDF = b"%PDF-1.4\n%HR's own signed letter\n"
_HOSTILE = "<font size=90>Big</font> & <b>unclosed <para> \x00\x07 </para><img src='x'/>"


def _plain_text(story: list[object]) -> str:
    """Every Paragraph's text, including those grouped inside a KeepTogether."""
    lines: list[str] = []
    for flowable in story:
        if isinstance(flowable, Paragraph):
            lines.append(flowable.getPlainText())
        elif isinstance(flowable, KeepTogether):
            lines.append(_plain_text(list(flowable._content)))
    return "\n".join(lines)


def _confirmation(
    name: str = "Remy Report", title: str = "Software Engineer"
) -> ConfirmationLetter:
    return ConfirmationLetter(
        issued_on=date(2026, 10, 16),
        employee_name=name,
        job_title=title,
        effective_date=date(2026, 10, 27),
    )


def _extension(expectations: tuple[str, ...] = ("Ship the billing rewrite.",)) -> ExtensionLetter:
    return ExtensionLetter(
        issued_on=date(2026, 10, 16),
        employee_name="Remy Report",
        job_title="Software Engineer",
        original_end_date=date(2026, 11, 1),
        new_end_date=date(2027, 1, 1),
        expectations=expectations,
    )


# --- rendering --------------------------------------------------------------- #
def test_confirmation_letter_renders_with_its_placeholders_filled() -> None:
    assert render_confirmation_letter(_confirmation()).startswith(b"%PDF")

    text = _plain_text(confirmation_story(_confirmation()))
    assert "Date: 16 October 2026" in text
    assert "Dear Remy Report," in text
    assert "Software Engineer" in text
    assert "Tuesday, 27 October 2026" in text  # "[Next Day and Date]"
    assert COMPANY_LEGAL_NAME in text
    assert "Infomedia" not in text  # one company name, not the template's two
    assert "Akshae Golekar" in text and "Authorized Signatory" in text
    assert chr(0x2014) not in text  # no em dash, whatever the template had


def test_extension_letter_lists_the_dates_and_expectations() -> None:
    letter = _extension(("Close the onboarding backlog.", "Pair with the lead weekly."))
    assert render_extension_letter(letter).startswith(b"%PDF")

    text = _plain_text(extension_story(letter))
    assert "originally set to end on 1 November 2026" in text
    assert "extended to 1 January 2027" in text
    assert "New Probation End Date: 1 January 2027." in text
    assert "Close the onboarding backlog." in text
    assert "Pair with the lead weekly." in text


def test_hostile_text_is_printed_not_parsed() -> None:
    """Paragraph parses markup. Escaped, the attack is just odd text on the page;
    unescaped it restyles the letter or makes the build raise."""
    pdf = render_confirmation_letter(_confirmation(name=_HOSTILE, title=_HOSTILE))
    assert pdf.startswith(b"%PDF")
    assert render_extension_letter(_extension((_HOSTILE,) * 5)).startswith(b"%PDF")

    text = _plain_text(confirmation_story(_confirmation(name=_HOSTILE)))
    assert "<font size=90>Big</font>" in text  # literal, so it was not interpreted
    assert "\x00" not in text and "\x07" not in text


# --- expectations validation ------------------------------------------------- #
def _create(outcome: str, expectations: list[str]) -> ProbationDecisionCreate:
    return ProbationDecisionCreate.model_validate(
        {"outcome": outcome, "effective_date": "2027-01-01", "expectations": expectations}
    )


def test_expectations_are_trimmed() -> None:
    assert _create("extended", ["  Ship it.  "]).expectations == ["Ship it."]


@pytest.mark.parametrize(
    "expectations",
    [
        ["one"] * 6,  # more than five
        ["fine", "   "],  # blank once trimmed
        ["x" * 301],  # too long
    ],
)
def test_bad_expectations_are_refused(expectations: list[str]) -> None:
    with pytest.raises(ValidationError):
        _create("extended", expectations)


def test_expectations_only_belong_to_an_extension() -> None:
    with pytest.raises(ValidationError):
        _create("confirmed", ["Ship it."])


def test_three_hundred_characters_is_allowed() -> None:
    assert _create("extended", ["x" * 300]).expectations == ["x" * 300]


# --- recording a decision ---------------------------------------------------- #
async def _probationer(db: AsyncSession, seed: _Seed) -> None:
    person = await db.get(Employee, seed.report.id)
    assert person is not None
    person.hire_date = datetime.now(UTC).date() - timedelta(days=30)
    person.job_title = "Software Engineer"
    await db.commit()


def _extend_to() -> str:
    return (datetime.now(UTC).date() + timedelta(days=270)).isoformat()


async def _decide(
    client: AsyncClient,
    settings: Settings,
    caller: Employee,
    target: Employee,
    data: dict[str, str | list[str]],
    files: dict[str, tuple[str, bytes, str]] | None = None,
):
    return await client.post(
        f"/api/v1/probation/{target.id}/decision",
        headers=auth_headers(settings, caller),
        data=data,
        files=files,
    )


def _sent_letter() -> EmailAttachment:
    assert len(_FakeEmailService.letters) == 1
    letter = _FakeEmailService.letters[0]
    assert isinstance(letter, EmailAttachment)
    return letter


async def test_confirmation_without_upload_attaches_the_standard_letter(
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

    assert res.status_code == 200, res.text
    letter = _sent_letter()
    assert letter.content.startswith(b"%PDF")
    assert letter.filename == "probation-confirmation-letter.pdf"
    assert res.json()["decision"]["letter_available"] is True


async def test_an_uploaded_letter_still_wins(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "extended", "effective_date": _extend_to()},
        files={"letter": ("signed.pdf", _PDF, "application/pdf")},
    )

    assert res.status_code == 200, res.text  # no expectations needed with an upload
    assert _sent_letter().content == _PDF


async def test_extension_letter_needs_expectations(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "extended", "effective_date": _extend_to()},
    )

    assert res.status_code == 422, res.text
    assert "expectation" in res.json()["error"]["message"]
    assert (
        await db.scalar(
            select(ProbationDecision).where(ProbationDecision.employee_id == seed.report.id)
        )
    ) is None  # refused before anything was recorded
    assert _FakeEmailService.letters == []


async def test_too_many_expectations_are_a_422(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "extended", "effective_date": _extend_to(), "expectations": ["x"] * 6},
    )

    assert res.status_code == 422, res.text


async def test_extension_with_expectations_generates_and_stores_the_letter(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)
    before = (
        await client.get(
            f"/api/v1/probation/{seed.report.id}", headers=auth_headers(settings, seed.admin)
        )
    ).json()["probation_end_date"]

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {
            "outcome": "extended",
            "effective_date": _extend_to(),
            "expectations": ["  Close the onboarding backlog. ", "Pair with the lead weekly."],
        },
    )

    assert res.status_code == 200, res.text
    decision = res.json()["decision"]
    assert decision["expectations"] == [
        "Close the onboarding backlog.",
        "Pair with the lead weekly.",
    ]
    assert decision["original_end_date"] == before  # the end it replaced
    assert decision["letter_available"] is True
    assert decision["letter_sent_at"] is not None
    letter = _sent_letter()
    assert letter.filename == "probation-extension-letter.pdf"
    assert letter.content.startswith(b"%PDF")


async def test_a_re_extension_keeps_the_original_end(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)
    first = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "extended", "effective_date": _extend_to(), "expectations": ["One."]},
    )
    original = first.json()["decision"]["original_end_date"]

    later = (datetime.now(UTC).date() + timedelta(days=330)).isoformat()
    second = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "extended", "effective_date": later, "expectations": ["Two."]},
    )

    assert second.status_code == 200, second.text
    assert second.json()["decision"]["original_end_date"] == original


# --- downloading ------------------------------------------------------------- #
async def _download(client: AsyncClient, settings: Settings, caller: Employee, target: Employee):
    return await client.get(
        f"/api/v1/probation/{target.id}/decision/letter", headers=auth_headers(settings, caller)
    )


async def test_hr_can_download_the_letter_and_it_is_audited(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)
    await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27", "send_letter": "false"},
    )

    res = await _download(client, settings, seed.admin, seed.report)

    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/pdf"
    assert "probation-confirmation-letter.pdf" in res.headers["content-disposition"]
    assert res.content.startswith(b"%PDF")
    actions = (await db.scalars(select(AuditLog.action))).all()
    assert "probation.letter.download" in actions


# `report` is the SUBJECT: 404 rather than 403, because their own review is
# not theirs to reach. Their letter still reaches them - HR emails it.
@pytest.mark.parametrize(("who", "status"), [("manager", 403), ("report", 404), ("outsider", 404)])
async def test_only_hr_or_admin_can_download(
    client: AsyncClient,
    db: AsyncSession,
    settings: Settings,
    seed: _Seed,
    who: str,
    status: int,
) -> None:
    await _probationer(db, seed)
    await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27", "send_letter": "false"},
    )

    res = await _download(client, settings, getattr(seed, who), seed.report)

    assert res.status_code == status
    assert not res.content.startswith(b"%PDF")


async def test_no_decision_means_no_letter(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _download(client, settings, seed.admin, seed.report)

    assert res.status_code == 404


async def test_a_termination_has_no_letter(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)
    await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "terminated", "effective_date": "2026-11-30"},
    )

    res = await _download(client, settings, seed.admin, seed.report)

    assert res.status_code == 404
    row = await db.scalar(
        select(ProbationDecision).where(ProbationDecision.employee_id == seed.report.id)
    )
    assert row is not None and row.outcome is ProbationOutcome.TERMINATED


# --- the checklist steps an outcome settles --------------------------------- #
def _step_notes(body: dict[str, object]) -> dict[int, str | None]:
    steps = body["steps"]
    assert isinstance(steps, list)
    return {s["no"]: s["note"] for s in steps if s["status"] != "pending"}


async def test_a_confirmation_ticks_the_decision_letter_and_hrms_steps(
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

    assert res.status_code == 200, res.text
    notes = _step_notes(res.json())
    assert notes[7] == "Done in Avora: outcome recorded (confirmed)."
    assert notes[13] == "Done in Avora: letter prepared and emailed to the employee."
    assert (notes[15] or "").startswith("Done in Avora: probation end date")
    assert 16 not in notes  # GoDigit is outside Avora: HR ticks it


async def test_a_termination_only_ticks_the_decision(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "terminated", "effective_date": "2026-11-30"},
    )

    assert res.status_code == 200, res.text
    assert set(_step_notes(res.json())) == {7}


async def test_a_changed_outcome_refreshes_avoras_note_but_not_a_persons(
    client: AsyncClient, db: AsyncSession, settings: Settings, seed: _Seed
) -> None:
    await _probationer(db, seed)
    await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "terminated", "effective_date": "2026-11-30"},
    )
    ticked = await client.put(
        f"/api/v1/probation/{seed.report.id}/steps/15",
        headers=auth_headers(settings, seed.admin),
        json={"status": "done", "note": "Updated by hand"},
    )
    assert ticked.status_code == 200, ticked.text

    res = await _decide(
        client,
        settings,
        seed.admin,
        seed.report,
        {"outcome": "confirmed", "effective_date": "2026-10-27", "send_letter": "false"},
    )

    assert res.status_code == 200, res.text
    notes = _step_notes(res.json())
    assert notes[7] == "Done in Avora: outcome recorded (confirmed)."
    assert notes[13] == "Done in Avora: letter prepared (not emailed from Avora)."
    assert notes[15] == "Updated by hand"
