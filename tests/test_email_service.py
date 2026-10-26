"""EmailService transport selection + SMTP message construction.

No network or real SMTP: the SendGrid path is asserted via dispatch, and the
SMTP path delivers into a captured EmailMessage instead of a live server.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from app.core.config import Environment, Settings
from app.services.email_service import EmailService


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": Environment.LOCAL,
        "database_url": "postgresql+asyncpg://unused:unused@localhost/unused",
        "email_from": "no-reply@avora.test",
        "email_from_name": "Avora",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_uses_smtp_reads_provider() -> None:
    assert _settings(email_provider="smtp").uses_smtp is True
    assert _settings(email_provider="SMTP").uses_smtp is True  # case-insensitive
    assert _settings(email_provider="sendgrid").uses_smtp is False


@pytest.mark.asyncio
async def test_send_dispatches_to_smtp_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    delivered: list[EmailMessage] = []
    service = EmailService(_settings(email_provider="smtp", smtp_host="smtp.test"))
    monkeypatch.setattr(service, "_smtp_deliver", delivered.append)

    await service.send(to="dev@avora.test", subject="Hi", html="<p>body</p>")

    assert len(delivered) == 1
    message = delivered[0]
    assert message["To"] == "dev@avora.test"
    assert message["Subject"] == "Hi"
    assert message["From"] == "Avora <no-reply@avora.test>"
    # HTML is carried as an alternative part, with a plaintext fallback present.
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    assert "<p>body</p>" in html_part.get_content()


@pytest.mark.asyncio
async def test_send_uses_sendgrid_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    service = EmailService(_settings(email_provider="sendgrid", sendgrid_api_key="sg-key-value"))
    monkeypatch.setattr(
        service,
        "_send_sendgrid",
        lambda **_: _record(called, "sendgrid"),  # type: ignore[misc]
    )
    monkeypatch.setattr(
        service,
        "_send_smtp",
        lambda **_: _record(called, "smtp"),  # type: ignore[misc]
    )

    await service.send(to="dev@avora.test", subject="Hi", html="<p>body</p>")

    assert called == ["sendgrid"]


async def _record(sink: list[str], name: str) -> None:
    sink.append(name)


# --------------------------------------------------------------------------- #
# The shared HR mailbox copy (`hr_notification_email`)
#
# HR asked to hold a record of every HR-relevant communication the system
# sends. The copy is a BCC decided inside the named `send_*` wrapper, so what
# these tests pin down is not just the plumbing but WHICH mail carries it - the
# part that would otherwise drift as new emails are added.
# --------------------------------------------------------------------------- #
_HR_MAILBOX = "hr@avora.test"


def _smtp_service(
    monkeypatch: pytest.MonkeyPatch, sink: list[EmailMessage], **overrides: object
) -> EmailService:
    service = EmailService(_settings(email_provider="smtp", smtp_host="smtp.test", **overrides))
    monkeypatch.setattr(service, "_smtp_deliver", sink.append)
    return service


@pytest.mark.asyncio
async def test_an_hr_relevant_email_bccs_the_hr_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leave decision stands in for the whole HR-relevant set.

    Asserted on the real wrapper rather than on `send(copy_hr=True)`: the flag
    working proves nothing if no email ever passes it.
    """
    sent: list[EmailMessage] = []
    service = _smtp_service(monkeypatch, sent, hr_notification_email=_HR_MAILBOX)

    await service.send_leave_decision(
        to="person@avora.test",
        employee_name="Remy",
        approved=True,
        leave_type_label="Sick",
        date_range_label="2 Mar",
        decided_by="Their Manager",
        note=None,
        link_path="/dashboard/me/leave",
    )

    assert len(sent) == 1
    assert sent[0]["To"] == "person@avora.test"
    assert sent[0]["Bcc"] == _HR_MAILBOX
    # BCC, not CC: the employee must not see HR on their own decision mail.
    assert sent[0]["Cc"] is None


@pytest.mark.asyncio
async def test_an_ordinary_email_does_not_reach_the_hr_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task assignment is not HR's business, and the default has to be silence -
    otherwise every email added later quietly lands in HR's inbox."""
    sent: list[EmailMessage] = []
    service = _smtp_service(monkeypatch, sent, hr_notification_email=_HR_MAILBOX)

    await service.send_task_assigned(
        to="person@avora.test",
        employee_name="Remy",
        task_title="Ship it",
        assigned_by="Their Manager",
        due_label=None,
        link_path="/dashboard/goals/tasks",
    )

    assert len(sent) == 1
    assert sent[0]["Bcc"] is None


@pytest.mark.asyncio
async def test_a_birthday_broadcast_does_not_reach_the_hr_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broadcasts go to the whole org, HR included, and they are chunked - so a
    copy would arrive once per chunk on top of the one HR already gets."""
    sent: list[EmailMessage] = []
    service = _smtp_service(monkeypatch, sent, hr_notification_email=_HR_MAILBOX)

    await service.send_birthday(
        to="person@avora.test", person_name="Remy", audience=["a@avora.test", "b@avora.test"]
    )

    assert len(sent) == 1
    assert sent[0]["Bcc"] is None


@pytest.mark.asyncio
async def test_without_a_configured_mailbox_nothing_is_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is empty, so an org that has not set an HR address keeps the
    behaviour it had before this existed."""
    sent: list[EmailMessage] = []
    service = _smtp_service(monkeypatch, sent)  # no hr_notification_email

    await service.send(to="person@avora.test", subject="Hi", html="<p>body</p>", copy_hr=True)

    assert len(sent) == 1
    assert sent[0]["Bcc"] is None


@pytest.mark.asyncio
async def test_hr_is_not_copied_on_a_message_it_already_receives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two copies of one message in a shared inbox reads as a bug and breaks
    threading. Matched case-insensitively, because an address typed into config
    will not match the casing a caller passes."""
    sent: list[EmailMessage] = []
    service = _smtp_service(monkeypatch, sent, hr_notification_email=_HR_MAILBOX)

    await service.send(to="HR@Avora.test", subject="Hi", html="<p>b</p>", copy_hr=True)
    await service.send(
        to="person@avora.test",
        subject="Hi",
        html="<p>b</p>",
        cc=["HR@AVORA.TEST"],
        copy_hr=True,
    )

    assert [m["Bcc"] for m in sent] == [None, None]


@pytest.mark.asyncio
async def test_whitespace_around_the_configured_address_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The value arrives from an env var, where a stray space is easy and
    invisible - and would otherwise be treated as a real address."""
    sent: list[EmailMessage] = []
    service = _smtp_service(monkeypatch, sent, hr_notification_email="   ")

    await service.send(to="person@avora.test", subject="Hi", html="<p>b</p>", copy_hr=True)

    assert sent[0]["Bcc"] is None


@pytest.mark.asyncio
async def test_the_sendgrid_path_carries_the_copy_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both transports, or the copy would silently depend on which provider the
    org happens to be on."""
    captured: list[dict[str, object]] = []
    service = EmailService(
        _settings(
            email_provider="sendgrid",
            sendgrid_api_key="sg-key-value",
            hr_notification_email=_HR_MAILBOX,
        )
    )

    async def capture(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(service, "_send_sendgrid", capture)
    await service.send(to="person@avora.test", subject="Hi", html="<p>b</p>", copy_hr=True)

    assert captured[0]["bcc"] == [_HR_MAILBOX]
