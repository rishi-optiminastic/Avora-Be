"""Transactional email via SendGrid.

External call → lives in the service layer (Layering §4). The API key comes
from Settings only, and we never log the key, recipient payload, or message
body (Security rule 5.6) — only a generic failure with the HTTP status.
"""

from __future__ import annotations

import httpx

from app.core.config import Settings
from app.services.email_templates import (
    agent_reinstall_email,
    invite_email,
    leave_decision_email,
    task_assigned_email,
)

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


class EmailError(Exception):
    """Raised when an email could not be handed off to the provider."""


class EmailService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, *, to: str, subject: str, html: str) -> None:
        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {
                "email": self._settings.email_from,
                "name": self._settings.email_from_name,
            },
            "subject": subject,
            "content": [{"type": "text/html", "value": html}],
        }
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

    async def send_agent_reinstall(
        self, *, to: str, employee_name: str, link_path: str
    ) -> None:
        subject, html = agent_reinstall_email(
            employee_name=employee_name,
            install_url=self._absolute(link_path),
        )
        await self.send(to=to, subject=subject, html=html)
