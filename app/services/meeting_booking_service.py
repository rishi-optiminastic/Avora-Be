"""Founder Meetings - putting an approved request on the CEO's calendar.

Kept apart from the approval state machine on purpose. This module is the only
thing that talks to Google, so the state machine stays testable without a
network and a calendar failure has exactly one place to be handled.

The handling is the interesting part. A booking failure must NOT roll the
approval back: both approvers said yes, and that judgement is durable work. So
`book` never raises on an upstream failure - it records SCHEDULING_FAILED with
Google's own reason and returns, leaving a request the Founder's Office can
retry. Raising would discard the decision along with the failed write, which is
the one outcome nobody can recover from.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.time import as_utc
from app.models.meeting_event import EventSyncStatus, MeetingEvent
from app.models.meeting_request import MeetingRequest, MeetingSlot, MeetingStatus, SlotStatus
from app.models.meeting_settings import MeetingSettings
from app.repositories.employee import EmployeeRepository
from app.repositories.meeting_request import MeetingRequestRepository
from app.repositories.meeting_settings import MeetingSettingsRepository
from app.services.google_calendar import GoogleCalendarClient, IntegrationError

logger = get_logger("app.meeting_booking")

# Marks the Google event as ours, so Phase 3 reconciliation can match an event
# back to its request without relying on the summary text.
_REQUEST_ID_KEY = "avoraMeetingRequestId"


class MeetingBookingService:
    def __init__(
        self,
        settings: Settings,
        requests: MeetingRequestRepository,
        employees: EmployeeRepository,
        meeting_settings: MeetingSettingsRepository,
    ) -> None:
        self._settings = settings
        self._requests = requests
        self._employees = employees
        self._meeting_settings = meeting_settings
        self._calendar = GoogleCalendarClient(settings)

    @property
    def calendar_configured(self) -> bool:
        return self._calendar.configured

    async def book(
        self,
        request: MeetingRequest,
        slot: MeetingSlot,
        *,
        actor_id: uuid.UUID,
    ) -> MeetingRequest:
        """Write the event, or record why it could not be written."""
        config = await self._meeting_settings.get_or_create()
        event = await self._claim(request)
        if event is None:
            raise ConflictError("That meeting is already on the calendar.")

        starts_at = as_utc(slot.starts_at)
        ends_at = starts_at + timedelta(minutes=slot.duration_minutes)
        subject, calendar_id = await self._target_calendar(config)

        request.status = MeetingStatus.SCHEDULING
        event.google_calendar_id = calendar_id
        event.starts_at = starts_at
        event.ends_at = ends_at
        event.attempt_count += 1
        event.last_attempt_at = datetime.now(UTC)
        await self._requests.flush()

        if not self._calendar.configured:
            return await self._record_failure(
                request,
                event,
                "Google is not configured: add the service-account credentials.",
                actor_id=actor_id,
            )

        body = await self._event_body(request, config, starts_at, ends_at)
        try:
            created = await self._calendar.insert_event(
                subject=subject, calendar_id=calendar_id, body=body
            )
        except IntegrationError as exc:
            return await self._record_failure(request, event, exc.message, actor_id=actor_id)

        return await self._record_success(request, event, slot, created, actor_id=actor_id)

    async def unbook(self, request: MeetingRequest, *, actor_id: uuid.UUID) -> None:
        """Remove a confirmed meeting from the calendar and tell the attendees.

        Failure here DOES propagate, unlike `book`. A cancellation that silently
        fails leaves the meeting on the CEO's day while Avora shows it gone,
        which is worse than an error the canceller can retry.
        """
        event = await self._requests.get_event(request.id)
        if event is None or not event.google_event_id:
            return
        calendar_id = event.google_calendar_id or "primary"
        # Impersonate the calendar the event is ON, not whoever is CEO today.
        # A change of CEO must not strand meetings already booked on the
        # previous one's calendar: the new CEO has no rights there, so the
        # delete would fail and the cancellation would be refused.
        if "@" in calendar_id:
            subject = calendar_id
        else:
            config = await self._meeting_settings.get_or_create()
            subject, _ = await self._target_calendar(config)
        await self._calendar.delete_event(
            subject=subject,
            calendar_id=calendar_id,
            event_id=event.google_event_id,
        )
        event.sync_status = EventSyncStatus.CANCELLED
        await self._requests.log(
            request_id=request.id,
            actor_id=actor_id,
            action="calendar.cancelled",
            detail="The calendar event was deleted and attendees were notified.",
        )
        await self._requests.flush()

    # ---- internals -------------------------------------------------------- #
    async def _claim(self, request: MeetingRequest) -> MeetingEvent | None:
        """The one event row for this request, or None if it is already booked.

        A retry after SCHEDULING_FAILED reuses the existing row rather than
        inserting a second one; the unique constraint would refuse it anyway,
        and that refusal IS the concurrency guard (two approvals racing produce
        one event, not two).
        """
        existing = await self._requests.get_event(request.id)
        if existing is not None:
            if existing.sync_status in (EventSyncStatus.CREATED, EventSyncStatus.UPDATED):
                return None
            return existing
        try:
            return await self._requests.claim_event(request.id, calendar_id="")
        except IntegrityError:
            # Lost the race. The winner is mid-write, so this caller must not
            # also book - reporting a conflict is the correct outcome.
            logger.info("meeting_event_claim_lost", extra={"request_id": str(request.id)})
            return None

    async def _target_calendar(self, config: MeetingSettings) -> tuple[str, str]:
        """(impersonated subject, calendar id) for the CEO's calendar.

        We act AS the CEO so the event is organised by them rather than appearing
        as somebody else's invitation, and we write to the calendar named in
        settings - never the Founder's Office's own, which is the easiest way to
        put a meeting on the wrong person's day.
        """
        if config.ceo_employee_id is None:
            raise ValidationError("No CEO is configured for Founder Meetings yet.")
        ceo = await self._employees.get(config.ceo_employee_id)
        if ceo is None:
            raise ValidationError("The configured CEO no longer has an employee record.")
        # The CEO's own address rather than "primary": `primary` is relative
        # to whoever is being impersonated, so an event recorded against it
        # cannot be found again once the impersonated user changes. An
        # explicit address stays resolvable, which is what cancelling needs.
        return ceo.work_email, (config.ceo_calendar_id or ceo.work_email)

    async def _event_body(
        self,
        request: MeetingRequest,
        config: MeetingSettings,
        starts_at: datetime,
        ends_at: datetime,
    ) -> dict[str, object]:
        people = await self._attendee_emails(request)
        zone = request.timezone or config.timezone
        return {
            "summary": request.title,
            "description": self._description(request),
            "start": {"dateTime": starts_at.isoformat(), "timeZone": zone},
            "end": {"dateTime": ends_at.isoformat(), "timeZone": zone},
            "attendees": [{"email": email} for email in people],
            "extendedProperties": {"private": {_REQUEST_ID_KEY: str(request.id)}},
        }

    async def _attendee_emails(self, request: MeetingRequest) -> list[str]:
        """The requester plus everyone they asked for, de-duplicated.

        The CEO is not listed: we are impersonating them, so they are the
        organiser and adding them again would invite them to their own meeting.
        """
        wanted = [request.requester_id, *(p.employee_id for p in request.participants)]
        found = await self._employees.get_many(list(dict.fromkeys(wanted)))
        emails: list[str] = []
        for employee_id in dict.fromkeys(wanted):
            employee = found.get(employee_id)
            if employee is not None and employee.work_email:
                emails.append(employee.work_email)
        return list(dict.fromkeys(emails))

    @staticmethod
    def _description(request: MeetingRequest) -> str:
        """What the CEO reads on the calendar entry.

        The agenda belongs here, unlike in a notification: everyone who can see
        this event is already in the meeting, whereas an inbox preview reaches a
        lock screen.
        """
        parts = [f"Purpose: {request.purpose}", "", "Agenda:", request.agenda]
        if request.expected_outcome:
            parts += ["", f"Expected outcome: {request.expected_outcome}"]
        parts += ["", "Raised through Avora Founder Meetings."]
        return "\n".join(parts)

    async def _record_success(
        self,
        request: MeetingRequest,
        event: MeetingEvent,
        slot: MeetingSlot,
        created: dict[str, object],
        *,
        actor_id: uuid.UUID,
    ) -> MeetingRequest:
        event.google_event_id = _text(created.get("id"))
        event.html_link = _text(created.get("htmlLink"))
        event.meet_url = self._calendar.extract_meet_link(created)
        event.ical_uid = _text(created.get("iCalUID"))
        event.etag = _text(created.get("etag"))
        event.sync_status = EventSyncStatus.CREATED
        event.last_error = None

        slot.status = SlotStatus.ACCEPTED
        await self._requests.close_open_slots(request, keep_id=slot.id)
        request.status = MeetingStatus.CONFIRMED
        request.scheduled_start_at = event.starts_at
        request.scheduled_end_at = event.ends_at
        await self._requests.log(
            request_id=request.id,
            actor_id=actor_id,
            action="calendar.created",
            detail="Invitations were sent to the requester and participants.",
        )
        await self._requests.flush()
        return request

    async def _record_failure(
        self,
        request: MeetingRequest,
        event: MeetingEvent,
        reason: str,
        *,
        actor_id: uuid.UUID,
    ) -> MeetingRequest:
        event.sync_status = EventSyncStatus.FAILED
        event.last_error = reason[:500]
        request.status = MeetingStatus.SCHEDULING_FAILED
        await self._requests.log(
            request_id=request.id,
            actor_id=actor_id,
            action="calendar.failed",
            detail=reason[:1000],
        )
        await self._requests.flush()
        logger.warning(
            "meeting_booking_failed",
            extra={"request_id": str(request.id), "attempt": event.attempt_count},
        )
        return request


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
