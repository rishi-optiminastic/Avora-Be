"""Transactional email via SendGrid (HTTP API) or SMTP (e.g. Gmail).

The transport is selected by `Settings.email_provider`; every caller uses the
same `send()` surface. External call → lives in the service layer (Layering §4).
Credentials come from Settings only, and we never log the key/password, recipient
payload, or message body (Security rule 5.6) — only a generic failure.
"""

from __future__ import annotations

import asyncio
import base64
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from app.core.config import Settings
from app.services.email_templates import (
    agent_reinstall_email,
    anniversary_email,
    birthday_email,
    festival_email,
    forgot_checkout_email,
    invite_email,
    leave_decision_email,
    leave_request_email,
    payslip_email,
    resignation_decision_email,
    resignation_submitted_email,
    task_assigned_email,
    task_escalated_email,
)

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


@dataclass(frozen=True)
class EmailAttachment:
    """One file to attach. `content` is the raw bytes; it is base64-encoded for
    the SendGrid payload here so callers never deal with the transport encoding."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


class EmailError(Exception):
    """Raised when an email could not be handed off to the provider."""


class EmailService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        """Deliver one HTML email through the configured provider."""
        if self._settings.uses_smtp:
            await self._send_smtp(to=to, subject=subject, html=html, attachments=attachments)
        else:
            await self._send_sendgrid(to=to, subject=subject, html=html, attachments=attachments)

    async def _send_sendgrid(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {
                "email": self._settings.email_from,
                "name": self._settings.email_from_name,
            },
            "subject": subject,
            "content": [{"type": "text/html", "value": html}],
        }
        if attachments:
            payload["attachments"] = [
                {
                    "content": base64.b64encode(a.content).decode("ascii"),
                    "type": a.content_type,
                    "filename": a.filename,
                    "disposition": "attachment",
                }
                for a in attachments
            ]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    _SENDGRID_URL,
                    headers={"Authorization": f"Bearer {self._settings.sendgrid_api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise EmailError("email provider unreachable") from exc
        if response.status_code >= 400:
            # Status only — never echo the body/key (it can contain PII/secrets).
            raise EmailError(f"email send failed ({response.status_code})")

    async def _send_smtp(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        attachments: list[EmailAttachment] | None = None,
    ) -> None:
        message = EmailMessage()
        message["From"] = formataddr((self._settings.email_from_name, self._settings.email_from))
        message["To"] = to
        message["Subject"] = subject
        # HTML with a minimal plaintext fallback for non-HTML clients.
        message.set_content("This email requires an HTML-capable email client to view.")
        message.add_alternative(html, subtype="html")
        for attachment in attachments or []:
            maintype, _, subtype = attachment.content_type.partition("/")
            message.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        # smtplib is blocking; run it off the event loop to preserve the async path.
        try:
            await asyncio.to_thread(self._smtp_deliver, message)
        except (smtplib.SMTPException, OSError) as exc:
            # Never echo the exception text — it can contain the recipient or auth detail.
            raise EmailError("email send failed (smtp)") from exc

    def _smtp_deliver(self, message: EmailMessage) -> None:
        settings = self._settings
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            if settings.smtp_starttls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)

    async def send_invite(
        self,
        *,
        to: str,
        inviter_name: str,
        role_label: str,
        org_name: str,
        accept_url: str,
        expires_label: str,
    ) -> None:
        subject, html = invite_email(
            inviter_name=inviter_name,
            role_label=role_label,
            org_name=org_name,
            accept_url=accept_url,
            expires_label=expires_label,
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_payslip(
        self,
        *,
        to: str,
        employee_name: str,
        month_label: str,
        currency: str,
        net_payable_minor: int,
        pdf: bytes,
        pdf_filename: str,
    ) -> None:
        """Email an employee their released payslip with the PDF attached."""
        subject, html = payslip_email(
            employee_name=employee_name,
            month_label=month_label,
            currency=currency,
            net_payable_minor=net_payable_minor,
            pay_url=self._absolute("/dashboard/me/pay"),
        )
        await self.send(
            to=to,
            subject=subject,
            html=html,
            attachments=[
                EmailAttachment(filename=pdf_filename, content=pdf, content_type="application/pdf")
            ],
        )

    def _absolute(self, link_path: str) -> str:
        """Turn a relative dashboard link into an absolute URL for the email."""
        return f"{self._settings.app_base_url.rstrip('/')}{link_path}"

    async def send_leave_decision(
        self,
        *,
        to: str,
        employee_name: str,
        approved: bool,
        leave_type_label: str,
        date_range_label: str,
        decided_by: str,
        note: str | None,
        link_path: str,
    ) -> None:
        subject, html = leave_decision_email(
            employee_name=employee_name,
            approved=approved,
            leave_type_label=leave_type_label,
            date_range_label=date_range_label,
            decided_by=decided_by,
            note=note,
            leave_url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_task_assigned(
        self,
        *,
        to: str,
        employee_name: str,
        task_title: str,
        assigned_by: str,
        due_label: str | None,
        link_path: str,
    ) -> None:
        subject, html = task_assigned_email(
            employee_name=employee_name,
            task_title=task_title,
            assigned_by=assigned_by,
            due_label=due_label,
            task_url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_leave_request(
        self,
        *,
        to: str,
        recipient_name: str,
        requester_name: str,
        leave_type_label: str,
        date_range_label: str,
        reason: str | None,
        can_decide: bool,
        link_path: str,
    ) -> None:
        """Tell an approver (or the requester's reporting manager) about a new
        leave request."""
        subject, html = leave_request_email(
            recipient_name=recipient_name,
            requester_name=requester_name,
            leave_type_label=leave_type_label,
            date_range_label=date_range_label,
            reason=reason,
            can_decide=can_decide,
            leave_url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_task_escalated(
        self,
        *,
        to: str,
        manager_name: str,
        assignee_name: str,
        task_title: str,
        escalated_by: str,
        due_label: str | None,
        blocked_reason: str | None,
        link_path: str,
    ) -> None:
        """Tell a reporting manager that a task on one of their people was escalated."""
        subject, html = task_escalated_email(
            manager_name=manager_name,
            assignee_name=assignee_name,
            task_title=task_title,
            escalated_by=escalated_by,
            due_label=due_label,
            blocked_reason=blocked_reason,
            task_url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_forgot_checkout(
        self, *, to: str, employee_name: str, day_label: str, checkout_label: str
    ) -> None:
        subject, html = forgot_checkout_email(
            employee_name=employee_name,
            day_label=day_label,
            checkout_label=checkout_label,
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_agent_reinstall(self, *, to: str, employee_name: str, link_path: str) -> None:
        subject, html = agent_reinstall_email(
            employee_name=employee_name,
            install_url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_resignation_submitted(
        self,
        *,
        to: str,
        recipient_name: str,
        resigner_name: str,
        last_working_label: str,
        reason: str | None,
        link_path: str,
    ) -> None:
        subject, html = resignation_submitted_email(
            recipient_name=recipient_name,
            resigner_name=resigner_name,
            last_working_label=last_working_label,
            reason=reason,
            url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_resignation_decision(
        self,
        *,
        to: str,
        employee_name: str,
        accepted: bool,
        last_working_label: str,
        decided_by: str,
        note: str | None,
        link_path: str,
    ) -> None:
        subject, html = resignation_decision_email(
            employee_name=employee_name,
            accepted=accepted,
            last_working_label=last_working_label,
            decided_by=decided_by,
            note=note,
            url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)

    async def send_birthday(self, *, to: str, person_name: str) -> None:
        subject, html = birthday_email(person_name=person_name)
        await self.send(to=to, subject=subject, html=html)

    async def send_anniversary(self, *, to: str, person_name: str, years: int) -> None:
        subject, html = anniversary_email(person_name=person_name, years=years)
        await self.send(to=to, subject=subject, html=html)

    async def send_festival(self, *, to: str, festival_name: str, message: str) -> None:
        subject, html = festival_email(festival_name=festival_name, message=message)
        await self.send(to=to, subject=subject, html=html)
