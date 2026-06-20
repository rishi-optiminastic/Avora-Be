"""Quick Meet — one click creates a Google Meet, posts it to Slack, and returns
the link to join.

Google Workspace service account + domain-wide delegation: the backend mints an
access token impersonating the clicking user (or a configured subject), calls the
Meet API `spaces.create` to get a real Meet link (no calendar clutter), then
posts to a Slack incoming webhook. The SA private key is a secret from Settings
only and is never logged (Security rule 5.6). No new dependency — the JWT
assertion is signed with PyJWT (RS256) and the HTTP is httpx.
"""

from __future__ import annotations

import time

import httpx
import jwt

from app.core.config import Settings
from app.core.exceptions import AppError
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.schemas.auth import CurrentUser
from app.schemas.meeting import QuickMeetingRead

_MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.created"
_MEET_SPACES_URL = "https://meet.googleapis.com/v2/spaces"


class IntegrationError(AppError):
    """An upstream integration (Google/Slack) is unavailable or misconfigured."""

    status_code = 503
    code = "integration_unavailable"
    message = "Meeting integration is unavailable."


class MeetingService:
    def __init__(
        self,
        settings: Settings,
        employees: EmployeeRepository,
        audit: AuditRepository,
    ) -> None:
        self._settings = settings
        self._employees = employees
        self._audit = audit

    async def start_quick(self, caller: CurrentUser) -> QuickMeetingRead:
        if not self._settings.quick_meet_configured:
            raise IntegrationError("Quick Meet isn't set up yet — add the Google service account.")

        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise IntegrationError("Your employee record could not be found.")
        subject = self._settings.google_meet_impersonate_subject or employee.work_email

        token = await self._access_token(subject)
        meet_url = await self._create_space(token)
        slack_posted = await self._post_to_slack(employee.full_name, meet_url)

        await self._audit.append(
            actor=str(caller.employee_id),
            action="meeting.quick_start",
            target="quick-meet",
        )
        return QuickMeetingRead(
            meet_url=meet_url, slack_posted=slack_posted, started_by=employee.full_name
        )

    # ---- Google service-account (domain-wide delegation) ------------------- #
    def _build_assertion(self, subject: str) -> str:
        """Signed JWT the SA exchanges for an access token, impersonating `subject`."""
        now = int(time.time())
        claims = {
            "iss": self._settings.google_sa_client_email,
            "sub": subject,  # the Workspace user we act as (domain-wide delegation)
            "scope": _MEET_SCOPE,
            "aud": self._settings.google_sa_token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        # Env stores the PEM with literal "\n"; turn them back into real newlines.
        key = self._settings.google_sa_private_key.replace("\\n", "\n")
        return jwt.encode(claims, key, algorithm="RS256")

    async def _access_token(self, subject: str) -> str:
        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self._build_assertion(subject),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self._settings.google_sa_token_uri, data=payload)
        except httpx.HTTPError as exc:
            raise IntegrationError("Could not reach Google to authorize.") from exc
        if resp.status_code >= 400:
            raise IntegrationError("Google rejected the meeting authorization.")
        token = resp.json().get("access_token")
        if not isinstance(token, str):
            raise IntegrationError("Google did not return an access token.")
        return token

    async def _create_space(self, token: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    _MEET_SPACES_URL, headers={"Authorization": f"Bearer {token}"}, json={}
                )
        except httpx.HTTPError as exc:
            raise IntegrationError("Could not reach Google Meet.") from exc
        if resp.status_code >= 400:
            raise IntegrationError("Google Meet could not create the meeting.")
        uri = resp.json().get("meetingUri")
        if not isinstance(uri, str):
            raise IntegrationError("Google Meet did not return a link.")
        return uri

    # ---- Slack ------------------------------------------------------------- #
    async def _post_to_slack(self, starter: str, url: str) -> bool:
        webhook = self._settings.slack_webhook_url
        if not webhook:
            return False
        text = self._settings.quick_meet_message.format(starter=starter, url=url)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook, json={"text": text})
        except httpx.HTTPError:
            return False  # the meeting still works even if Slack is down
        return resp.status_code < 400
