"""Events: what is happening, who it is for, and the form that collects answers.

HR and admins create and run them (`can_manage_forms` - the same people who
build forms, because an event is mostly a reason to send one). Everyone else
sees the published events they are in the audience for.

Deliberately NOT built: a separate RSVP table. Attaching a form gives sign-ups,
feedback and anything else with one set of answers in one place, which is the
whole point of having a form builder.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.event import Event, EventStatus
from app.models.form import AssignmentStatus, Form, FormStatus
from app.models.notification import NotificationKind, NotificationLevel
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.event import EventRepository
from app.repositories.form import FormAssignmentRepository, FormRepository
from app.schemas.audience import AudienceKind
from app.schemas.auth import CurrentUser
from app.schemas.event import EventRead, EventWrite
from app.schemas.form import AssignmentCreate
from app.services.audience import AudienceResolver
from app.services.form_assignment_service import FormAssignmentService
from app.services.form_views import can_manage_forms
from app.services.notification_service import NotificationService

logger = get_logger("app.events")

_EVENTS_LINK = "/dashboard/events"


class EventService:
    def __init__(
        self,
        events: EventRepository,
        forms: FormRepository,
        assignments: FormAssignmentRepository,
        employees: EmployeeRepository,
        notifications: NotificationService,
        audit: AuditRepository,
        form_assignments: FormAssignmentService,
    ) -> None:
        self._events = events
        self._forms = forms
        self._assignments = assignments
        self._employees = employees
        self._notifications = notifications
        self._audit = audit
        self._form_assignments = form_assignments
        self._audience = AudienceResolver(employees)

    # --- reading ---------------------------------------------------------- #
    async def list_for(self, caller: CurrentUser) -> list[EventRead]:
        """Everything for an organiser; the published audience view otherwise."""
        if can_manage_forms(caller):
            rows = await self._events.list_all()
        else:
            me = await self._employees.get(caller.employee_id)
            rows = await self._events.list_visible_to(
                caller.employee_id, me.department if me is not None else None
            )
        return [await self._read(event, caller) for event in rows]

    async def get(self, caller: CurrentUser, event_id: uuid.UUID) -> EventRead:
        event = await self._events.get(event_id)
        if event is None or not await self._may_see(caller, event):
            # 404 over 403: a draft event's existence is not public (rule 7).
            raise NotFoundError()
        return await self._read(event, caller)

    # --- writing ---------------------------------------------------------- #
    async def create(self, caller: CurrentUser, payload: EventWrite) -> EventRead:
        self._guard(caller)
        await self._check_audience(payload)
        form = await self._check_form(payload.form_id)
        event = Event(
            title=payload.title.strip(),
            description=(payload.description or None),
            location=(payload.location or None),
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            all_day=payload.all_day,
            status=EventStatus.DRAFT,
            audience_kind=payload.audience.kind,
            audience_departments=payload.audience.stored_departments(),
            audience_employee_ids=payload.audience.stored_employee_ids(),
            form_id=form.id if form is not None else None,
            created_by=caller.employee_id,
        )
        self._events.add(event)
        await self._events.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="event.create",
            target=f"event:{event.id}",
        )
        return await self._read(event, caller)

    async def update(
        self, caller: CurrentUser, event_id: uuid.UUID, payload: EventWrite
    ) -> EventRead:
        self._guard(caller)
        event = await self._events.get(event_id)
        if event is None:
            raise NotFoundError()
        if event.status is EventStatus.CANCELLED:
            raise ConflictError("A cancelled event can no longer be edited.")
        await self._check_audience(payload)
        form = await self._check_form(payload.form_id)
        if event.form_sent_at is not None and (form.id if form else None) != event.form_id:
            # The old form is already in people's lists. Swapping it now would
            # leave them holding a form for an event that no longer uses it,
            # with no way to tell which answers belong to what.
            raise ConflictError("The form has already been sent; it can no longer be swapped.")

        event.title = payload.title.strip()
        event.description = payload.description or None
        event.location = payload.location or None
        event.starts_at = payload.starts_at
        event.ends_at = payload.ends_at
        event.all_day = payload.all_day
        event.audience_kind = payload.audience.kind
        event.audience_departments = payload.audience.stored_departments()
        event.audience_employee_ids = payload.audience.stored_employee_ids()
        event.form_id = form.id if form is not None else None
        await self._events.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="event.update",
            target=f"event:{event.id}",
        )
        return await self._read(event, caller)

    async def publish(self, caller: CurrentUser, event_id: uuid.UUID) -> EventRead:
        """Make it live, tell the audience, and send the form if there is one.

        Publishing twice is harmless: `form_sent_at` stops the form going out
        again, and the notification loop skips anyone already assigned.
        """
        self._guard(caller)
        event = await self._events.get(event_id)
        if event is None:
            raise NotFoundError()
        if event.status is EventStatus.CANCELLED:
            raise ConflictError("A cancelled event cannot be published.")

        first_time = event.status is not EventStatus.PUBLISHED
        event.status = EventStatus.PUBLISHED
        if event.published_at is None:
            event.published_at = datetime.now(UTC)
        await self._events.flush()

        if event.form_id is not None and event.form_sent_at is None:
            await self._send_event_form(caller, event)
        if first_time:
            await self._announce(caller, event)

        await self._audit.append(
            actor=str(caller.employee_id),
            action="event.publish",
            target=f"event:{event.id}",
        )
        return await self._read(event, caller)

    async def cancel(self, caller: CurrentUser, event_id: uuid.UUID) -> EventRead:
        """Call it off. The event stays visible to the audience, marked
        cancelled - people who planned around it need to see that it is off,
        not simply find it gone."""
        self._guard(caller)
        event = await self._events.get(event_id)
        if event is None:
            raise NotFoundError()
        if event.status is EventStatus.CANCELLED:
            return await self._read(event, caller)
        was_live = event.status is EventStatus.PUBLISHED
        event.status = EventStatus.CANCELLED
        event.cancelled_at = datetime.now(UTC)
        await self._events.flush()
        if was_live:
            await self._announce(caller, event, cancelled=True)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="event.cancel",
            target=f"event:{event.id}",
        )
        return await self._read(event, caller)

    async def delete(self, caller: CurrentUser, event_id: uuid.UUID) -> None:
        """Only a draft. Once an event has been published people have seen it,
        and the honest way to take it back is to cancel it."""
        self._guard(caller)
        event = await self._events.get(event_id)
        if event is None:
            raise NotFoundError()
        if event.status is not EventStatus.DRAFT:
            raise ConflictError("Published events are cancelled, not deleted.")
        await self._events.delete(event)
        await self._events.flush()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="event.delete",
            target=f"event:{event_id}",
        )

    # --- helpers ---------------------------------------------------------- #
    @staticmethod
    def _guard(caller: CurrentUser) -> None:
        if not can_manage_forms(caller):
            raise AuthorizationError("Only HR or an admin can run events.")

    async def _check_audience(self, payload: EventWrite) -> None:
        """Resolve it once at write time so an impossible audience is a 422 now
        rather than a mystery empty invite later."""
        await self._audience.resolve(payload.audience)

    async def _check_form(self, form_id: uuid.UUID | None) -> Form | None:
        if form_id is None:
            return None
        form = await self._forms.get(form_id)
        if form is None:
            raise ValidationError("That form does not exist.")
        if form.status is not FormStatus.PUBLISHED:
            raise ValidationError("Publish the form before attaching it to an event.")
        return form

    async def _send_event_form(self, caller: CurrentUser, event: Event) -> None:
        """Hand the event's audience its form, through the ordinary send path.

        Reused rather than reimplemented so an event form behaves exactly like
        any other: same duplicate skipping, same due date, same scheduler
        telling people afterwards.
        """
        if event.form_id is None:
            return
        spec = AudienceResolver.from_stored(
            event.audience_kind, event.audience_departments, event.audience_employee_ids
        )
        await self._form_assignments.assign(caller, event.form_id, AssignmentCreate(audience=spec))
        event.form_sent_at = datetime.now(UTC)
        await self._events.flush()

    async def _announce(
        self, caller: CurrentUser, event: Event, *, cancelled: bool = False
    ) -> None:
        """Tell the audience in-app. Never email: an event is not urgent enough
        to land in everyone's inbox, and the form (if there is one) sends its
        own mail through the forms scheduler."""
        spec = AudienceResolver.from_stored(
            event.audience_kind, event.audience_departments, event.audience_employee_ids
        )
        try:
            people = await self._audience.resolve(spec)
        except ValidationError:
            # An audience that has gone empty (a renamed department) must not
            # stop the event going live - the organiser can see the count.
            logger.warning("event_audience_empty", extra={"event": str(event.id)})
            return
        when = event.starts_at.strftime("%d %b")
        title = f"Event cancelled: {event.title}" if cancelled else f"New event: {event.title}"
        body = when if cancelled else f"{when}{f' · {event.location}' if event.location else ''}"
        for person in people:
            if person.id == caller.employee_id:
                continue
            await self._notifications.notify(
                recipient_id=person.id,
                kind=NotificationKind.EVENT,
                title=title,
                body=body,
                level=NotificationLevel.WARNING if cancelled else NotificationLevel.INFO,
                link=_EVENTS_LINK,
                entity_type="event",
                entity_id=event.id,
                actor_id=caller.employee_id,
            )

    async def _may_see(self, caller: CurrentUser, event: Event) -> bool:
        if can_manage_forms(caller) or event.created_by == caller.employee_id:
            return True
        if event.status is EventStatus.DRAFT:
            return False
        me = await self._employees.get(caller.employee_id)
        department = (me.department or "").strip().casefold() if me is not None else ""
        if event.audience_kind is AudienceKind.EVERYONE:
            return True
        if event.audience_kind is AudienceKind.DEPARTMENTS:
            return department in {
                d.strip().casefold() for d in event.audience_departments if d.strip()
            }
        return str(caller.employee_id) in set(event.audience_employee_ids)

    async def _read(self, event: Event, caller: CurrentUser) -> EventRead:
        spec = AudienceResolver.from_stored(
            event.audience_kind, event.audience_departments, event.audience_employee_ids
        )
        manages = can_manage_forms(caller)
        form = await self._forms.get(event.form_id) if event.form_id is not None else None
        submitted = pending = None
        if manages and event.form_id is not None:
            counts = await self._forms.assignment_counts([event.form_id])
            submitted = counts.get((event.form_id, AssignmentStatus.SUBMITTED), 0)
            pending = counts.get((event.form_id, AssignmentStatus.PENDING), 0)
        creator = (
            await self._employees.get(event.created_by) if event.created_by is not None else None
        )
        return EventRead(
            id=event.id,
            title=event.title,
            description=event.description,
            location=event.location,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            all_day=event.all_day,
            status=event.status,
            audience=await self._audience.describe(spec),
            form_id=event.form_id,
            form_title=form.title if form is not None else None,
            form_sent_at=event.form_sent_at,
            form_submitted=submitted,
            form_pending=pending,
            my_assignment_id=await self._my_assignment(event, caller),
            created_by=event.created_by,
            created_by_name=creator.full_name if creator is not None else None,
            published_at=event.published_at,
            cancelled_at=event.cancelled_at,
            created_at=event.created_at,
            can_manage=manages,
        )

    async def _my_assignment(self, event: Event, caller: CurrentUser) -> uuid.UUID | None:
        """The caller's own assignment for the event form, so the event page
        can link straight to it instead of sending them to hunt in Forms."""
        if event.form_id is None:
            return None
        mine: Sequence[uuid.UUID] = await self._assignments.assignment_ids_for(
            event.form_id, caller.employee_id
        )
        return mine[0] if mine else None
