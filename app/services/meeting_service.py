"""Quick Meet - one click creates a Google Calendar event with a Meet link,
emails the invite to the default attendees, posts it to Slack, and returns the
link to join.

The Google plumbing (service-account domain-wide delegation, the RS256
assertion, the token exchange, the PEM normalisation) now lives in
`app.services.google_calendar`, shared with Founder Meetings so there is one
tested path to Calendar rather than two drifting copies. This module keeps what
is specific to Quick Meet: a 30-minute event starting now, the caller's saved
default invitees, and the Slack post.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.quick_meet import QuickMeetRepository
from app.schemas.auth import CurrentUser
from app.schemas.meeting import QuickMeetDefaultsRead, QuickMeetingRead
from app.services.google_calendar import GoogleCalendarClient, IntegrationError

__all__ = ["IntegrationError", "MeetingService"]

logger = get_logger("app.meeting")


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
        self._calendar = GoogleCalendarClient(settings)

    async def start_quick(self, caller: CurrentUser) -> QuickMeetingRead:
        if not self._settings.quick_meet_configured:
            raise IntegrationError("Quick Meet isn't set up yet - add the Google service account.")

        employee = await self._employees.get(caller.employee_id)
        if employee is None:
            raise IntegrationError("Your employee record could not be found.")
        subject = self._settings.google_meet_impersonate_subject or employee.work_email

        # The caller's own saved default invitees; don't re-invite the organizer
        # (the calendar owner is already a participant).
        defaults = await self._quick_meet.get_for_owner(caller.employee_id)
        saved = defaults.invitee_emails if defaults else []
        attendees = [e for e in saved if e.lower() != subject.lower()]

        meet_url = await self._create_event(subject, employee.full_name, attendees)
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

    async def _create_event(self, subject: str, starter: str, attendees: list[str]) -> str:
        """Insert a Calendar event with a Meet link; Google emails the attendees."""
        start = datetime.now(UTC)
        end = start + timedelta(minutes=self._settings.quick_meet_duration_minutes)
        event = await self._calendar.insert_event(
            subject=subject,
            calendar_id="primary",
            body={
                "summary": f"Quick Meet - {starter}",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "attendees": [{"email": email} for email in attendees],
            },
        )
        link = self._calendar.extract_meet_link(event)
        if link is None:
            # Quick Meet's entire output IS the link, so an event without one is
            # a failure here even though it is legitimate elsewhere.
            raise IntegrationError("Google Calendar did not return a meeting link.")
        return link

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
