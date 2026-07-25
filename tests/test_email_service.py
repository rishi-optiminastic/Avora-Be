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
        service, "_send_sendgrid", lambda **_: _record(called, "sendgrid")  # type: ignore[misc]
    )
    monkeypatch.setattr(
        service, "_send_smtp", lambda **_: _record(called, "smtp")  # type: ignore[misc]
    )

    await service.send(to="dev@avora.test", subject="Hi", html="<p>body</p>")

    assert called == ["sendgrid"]


async def _record(sink: list[str], name: str) -> None:
    sink.append(name)
