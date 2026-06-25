"""Quick Meet — one click creates a Google Calendar event with a Meet link,
emails the invite to the default attendees, posts it to Slack, and returns the
link to join.

Google Workspace service account + domain-wide delegation: the backend mints an
access token impersonating the clicking user (or a configured subject), calls the
Calendar API `events.insert` with `conferenceData` to get a real Meet link and
`sendUpdates=all` so Google emails the invite to every attendee, then posts to a
Slack incoming webhook. The SA private key is a secret from Settings only and is
never logged (Security rule 5.6). No new dependency — the JWT assertion is signed
with PyJWT (RS256) and the HTTP is httpx.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.quick_meet import QuickMeetRepository
from app.schemas.auth import CurrentUser
from app.schemas.meeting import QuickMeetDefaultsRead, QuickMeetingRead

# calendar.events lets us insert an event with a Meet conference and invite people.
_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

logger = get_logger("app.meeting")


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
        quick_meet: QuickMeetRepository,
    ) -> None:
        self._settings = settings
        self._employees = employees
        self._audit = audit
        self._quick_meet = quick_meet

    async def start_quick(self, caller: CurrentUser) -> QuickMeetingRead:
        if not self._settings.quick_meet_configured:
            raise IntegrationError("Quick Meet isn't set up yet — add the Google service account.")

        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise IntegrationError("Your employee record could not be found.")
        subject = self._settings.google_meet_impersonate_subject or employee.work_email

        # The caller's own saved default invitees; don't re-invite the organizer
        # (the calendar owner is already a participant).
        defaults = await self._quick_meet.get_for_owner(caller.employee_id)
        saved = defaults.invitee_emails if defaults else []
        attendees = [e for e in saved if e.lower() != subject.lower()]

        token = await self._access_token(subject)
        meet_url = await self._create_event(token, employee.full_name, attendees)
        slack_posted = await self._post_to_slack(employee.full_name, meet_url)

        await self._audit.append(
            actor=str(caller.employee_id),
            action="meeting.quick_start",
            target="quick-meet",
        )
        return QuickMeetingRead(
            meet_url=meet_url,
            slack_posted=slack_posted,
            started_by=employee.full_name,
            invited_count=len(attendees),
        )

    async def get_defaults(self, caller: CurrentUser) -> QuickMeetDefaultsRead:
        """The caller's own default invitee list (empty when never set)."""
        defaults = await self._quick_meet.get_for_owner(caller.employee_id)
        return QuickMeetDefaultsRead(invitee_emails=defaults.invitee_emails if defaults else [])

    async def set_defaults(
        self, caller: CurrentUser, invitee_emails: list[str]
    ) -> QuickMeetDefaultsRead:
        """Replace the caller's own default invitee list."""
        row = await self._quick_meet.set_for_owner(caller.employee_id, invitee_emails)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="meeting.quick_defaults_set",
            target="quick-meet",
        )
        return QuickMeetDefaultsRead(invitee_emails=row.invitee_emails)

    # ---- Google service-account (domain-wide delegation) ------------------- #
    def _build_assertion(self, subject: str) -> str:
        """Signed JWT the SA exchanges for an access token, impersonating `subject`."""
        now = int(time.time())
        claims = {
            "iss": self._settings.google_sa_client_email,
            "sub": subject,  # the Workspace user we act as (domain-wide delegation)
            "scope": _CALENDAR_SCOPE,
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
            # Google's token-endpoint error body carries no secrets — only
            # {"error", "error_description"}. Log it so prod failures are
            # diagnosable instead of an opaque 400 -> 503. The subject is the
            # impersonated user, not a secret.
            logger.warning(
                "google_token_exchange_failed",
                extra={
                    "status": resp.status_code,
                    "google_error": resp.text,
                    "subject": subject,
                    "sa_client_email": self._settings.google_sa_client_email,
                },
            )
            raise IntegrationError("Google rejected the meeting authorization.")
        token = resp.json().get("access_token")
        if not isinstance(token, str):
            raise IntegrationError("Google did not return an access token.")
        return token

    async def _create_event(self, token: str, starter: str, attendees: list[str]) -> str:
        """Insert a Calendar event with a Meet link; Google emails the attendees."""
        start = datetime.now(UTC)
        end = start + timedelta(minutes=self._settings.quick_meet_duration_minutes)
        body = {
            "summary": f"Quick Meet — {starter}",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": [{"email": email} for email in attendees],
            "conferenceData": {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    _CALENDAR_EVENTS_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"conferenceDataVersion": 1, "sendUpdates": "all"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise IntegrationError("Could not reach Google Calendar.") from exc
        if resp.status_code >= 400:
            raise IntegrationError("Google Calendar could not create the meeting.")
        return self._extract_meet_link(resp.json())

    @staticmethod
    def _extract_meet_link(event: dict[str, Any]) -> str:
        link = event.get("hangoutLink")
        if isinstance(link, str) and link:
            return link
        conference = event.get("conferenceData")
        entry_points = conference.get("entryPoints", []) if isinstance(conference, dict) else []
        for entry in entry_points:
            uri = entry.get("uri")
            if entry.get("entryPointType") == "video" and isinstance(uri, str):
                return uri
        raise IntegrationError("Google Calendar did not return a meeting link.")

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
