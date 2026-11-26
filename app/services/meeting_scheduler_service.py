"""The three things nobody is awake to do for a Founder Meeting.

Each tick does three independent jobs, in this order and for this reason:

 1. RETRY what the calendar refused. These are meetings both approvers already
    said yes to, and they are the only state in the feature that recovers from
    nothing - no invitation exists to remind anybody they were supposed to
    happen. Doing this first means a booking repaired on this tick can still be
    reminded about on the same one.
 2. REMIND the people in a meeting that is about to start.
 3. CLOSE meetings whose end time has passed.

Isolation is per ROW, not per job. One meeting whose Google call fails must not
stop the next one being retried, or the meeting after it being marked complete;
that is what the per-row wrapping below buys.

It deliberately does NOT claim to isolate the three jobs from each other. They
share one session, so a database error in the first leaves the transaction
needing a rollback and the other two would fail anyway - catching it would
collect three errors instead of one and then raise on commit. A tick that
breaks that badly is the worker loop's problem, and it keeps running.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.models.meeting_request import MeetingRequest, MeetingStatus
from app.models.meeting_settings import MeetingSettings
from app.models.notification import NotificationKind
from app.repositories.meeting_request import MeetingRequestRepository
from app.repositories.meeting_settings import MeetingSettingsRepository
from app.services import meeting_policy, meeting_text
from app.services.meeting_booking_service import MeetingBookingService
from app.services.notification_service import NotificationService

logger = get_logger("app.meeting_scheduler")

_ENTITY = "meeting_request"

#: How many rows one tick will touch per job. A cap rather than a full sweep so
#: a backlog is cleared over several ticks instead of one tick holding a
#: transaction open across hundreds of Google calls.
BATCH = 50

#: Reminders are sent once, but a request can be reminded about, cancelled and
#: raised again; the dedupe window stops a second notification arriving for the
#: same meeting within the hour if anything else re-triggers one.
_REMINDER_DEDUPE = timedelta(hours=1)


@dataclass
class TickOutcome:
    """What one sweep did. Logged, and asserted on in the tests."""

    retried: int = 0
    rebooked: int = 0
    reminded: int = 0
    completed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def did_anything(self) -> bool:
        return bool(self.retried or self.reminded or self.completed or self.failures)


class MeetingSchedulerService:
    def __init__(
        self,
        requests: MeetingRequestRepository,
        meeting_settings: MeetingSettingsRepository,
        booking: MeetingBookingService,
        notifications: NotificationService,
    ) -> None:
        self._requests = requests
        self._settings = meeting_settings
        self._booking = booking
        self._notifications = notifications

    async def run_tick(self, now: datetime | None = None) -> TickOutcome:
        """One sweep. Never raises for a single row's sake."""
        moment = now or datetime.now(UTC)
        config = await self._settings.get_or_create()
        outcome = TickOutcome()
        await self._retry_failed(outcome)
        await self._send_reminders(outcome, config, moment)
        await self._close_past(outcome, moment)
        return outcome

    # ---- 1. retry -------------------------------------------------------- #
    async def _retry_failed(self, outcome: TickOutcome) -> None:
        for row in await self._requests.stuck_scheduling(limit=BATCH):
            slot = meeting_policy.default_slot(row)
            if slot is None or meeting_policy.refusal_reason(slot) is not None:
                # Nothing to retry against. A request in this state needs a
                # human, and the Founder's Office sees it on their queue.
                continue
            outcome.retried += 1
            try:
                # `actor_id=None`: no human did this, and the activity log
                # should not claim one did.
                await self._booking.book(row, slot, actor_id=None)
            except Exception as exc:
                # `book` records its own failures and returns; reaching here
                # means something else broke. Class name only - an exception
                # can carry row data.
                outcome.failures.append(f"retry:{type(exc).__name__}")
                continue
            if row.status is MeetingStatus.CONFIRMED:
                outcome.rebooked += 1
                await self._announce_recovery(row)

    async def _announce_recovery(self, row: MeetingRequest) -> None:
        """Tell the requester it finally landed.

        They were told it failed, so they are owed the correction. The
        Founder's Office is not told: the request simply leaves their queue,
        which is the whole signal they need.
        """
        await self._notifications.notify(
            recipient_id=row.requester_id,
            kind=NotificationKind.MEETING_CONFIRMED,
            title=f"Now booked: {meeting_text.subject(row)}",
            body=f"{meeting_text.when_label(row)}. The invitation is on its way.",
            entity_type=_ENTITY,
            entity_id=row.id,
        )

    # ---- 2. remind ------------------------------------------------------- #
    async def _send_reminders(
        self, outcome: TickOutcome, config: MeetingSettings, now: datetime
    ) -> None:
        lead = max(0, config.reminder_lead_minutes)
        if lead == 0:
            # Explicitly turned off, rather than "every confirmed meeting is
            # due a reminder right now".
            return
        until = now + timedelta(minutes=lead)
        for row in await self._requests.due_for_reminder(now=now, until=until, limit=BATCH):
            try:
                await self._remind_everyone(row)
            except Exception as exc:
                outcome.failures.append(f"remind:{type(exc).__name__}")
                continue
            # Stamped whatever the notification layer did. Delivery is
            # best-effort by design, and a failed insert must not leave the row
            # in the window to be retried every tick for the next half hour.
            row.reminder_sent_at = now
            outcome.reminded += 1
        await self._requests.flush()

    async def _remind_everyone(self, row: MeetingRequest) -> None:
        when = meeting_text.when_label(row)
        event = await self._requests.get_event(row.id)
        # The joining link if there is one. Not the agenda: a lock-screen
        # preview is the one place a confidential subject would leak.
        body = (
            f"Starts {when}."
            if event is None or not event.meet_url
            else (f"Starts {when}. {event.meet_url}")
        )
        for recipient in self._everyone_in(row):
            await self._notifications.notify(
                recipient_id=recipient,
                kind=NotificationKind.MEETING_REMINDER,
                title=f"Soon: {meeting_text.subject(row)}",
                body=body,
                entity_type=_ENTITY,
                entity_id=row.id,
                dedupe_within=_REMINDER_DEDUPE,
            )

    @staticmethod
    def _everyone_in(row: MeetingRequest) -> list[uuid.UUID]:
        """The requester and the participants, each once.

        The CEO is deliberately left out: they hold the calendar invitation,
        and Google is already reminding them. A second nudge from Avora for
        every meeting in their day would be the thing that makes them turn
        notifications off.
        """
        seen = [row.requester_id]
        for participant in row.participants:
            if participant.employee_id not in seen:
                seen.append(participant.employee_id)
        return seen

    # ---- 3. close -------------------------------------------------------- #
    async def _close_past(self, outcome: TickOutcome, now: datetime) -> None:
        for row in await self._requests.due_for_completion(before=now, limit=BATCH):
            row.status = MeetingStatus.COMPLETED
            row.completed_at = now
            await self._requests.log(
                request_id=row.id,
                action="completed",
                detail="The meeting's end time passed.",
            )
            outcome.completed += 1
        await self._requests.flush()
