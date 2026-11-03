"""Recognition business rules.

A manager marks something one of their reports did well; the whole org sees it
and can add to the thread.

The rules that carry weight:

* **You award your own reports.** HR and admin may award anyone. This is the same
  shape as every other permission here - the `manager_id` edge grants the scope,
  not the job title - and it keeps a badge meaningful: it comes from someone who
  saw the work.
* **Never yourself.** Self-awarded recognition is not recognition.
* **The feed is org-wide.** Recognition only the recipient can see is a private
  message. Colleagues commenting on it is the point of the programme.
* **No cadence limit.** Weekly and monthly are guidance on which badge fits, not
  a quota. Nobody should be unable to recognise good work because a counter said
  no.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.core.recognition_badges import BY_BADGE, label_for
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind
from app.models.recognition import Recognition, RecognitionBadge, RecognitionComment
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.recognition import RecognitionRepository
from app.schemas.auth import CurrentUser
from app.schemas.recognition import (
    RecognitionCommentCreate,
    RecognitionCommentRead,
    RecognitionCreate,
    RecognitionRead,
)
from app.services.email_service import EmailService
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

_LINK = "/dashboard/recognition"


class RecognitionService:
    def __init__(
        self,
        recognitions: RecognitionRepository,
        employees: EmployeeRepository,
        audit: AuditRepository,
        notifications: NotificationService,
        email: EmailService,
    ) -> None:
        self._recognitions = recognitions
        self._employees = employees
        self._audit = audit
        self._notifications = notifications
        self._email = email

    async def award(self, caller: CurrentUser, payload: RecognitionCreate) -> Recognition:
        recipient = await self._employees.get(payload.employee_id)
        if recipient is None or not recipient.is_active:
            raise NotFoundError()
        if recipient.id == caller.employee_id:
            raise ValidationError("You can't recognise yourself.")
        if not _may_award(caller, recipient):
            raise AuthorizationError()

        row = await self._recognitions.create(
            employee_id=recipient.id,
            awarded_by_id=caller.employee_id,
            badge=payload.badge,
            note=payload.note.strip(),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="recognition.award",
            target=f"recognition:{row.id}:{recipient.id}:{payload.badge.value}",
        )
        await self._announce(caller, row, recipient)
        return row

    async def _announce(self, caller: CurrentUser, row: Recognition, recipient: Employee) -> None:
        """In-app for the recipient, email for the recipient only.

        Ten badge types at a weekly cadence across the company would be a great
        deal of mail if everyone were copied, and mail everyone ignores defeats
        the purpose. The feed is where colleagues find out.
        """
        await self._notifications.notify(
            recipient_id=recipient.id,
            kind=NotificationKind.SYSTEM,
            title=f"You received {label_for(row.badge)}",
            body=row.note[:200],
            link=_LINK,
            entity_type="recognition",
            entity_id=row.id,
            actor_id=caller.employee_id,
        )
        if not recipient.work_email:
            return
        awarder = await self._employees.get(caller.employee_id)
        try:
            await self._email.send_recognition_awarded(
                to=recipient.work_email,
                recipient_name=recipient.full_name,
                awarded_by_name=awarder.full_name if awarder else "Your manager",
                badge_label=label_for(row.badge),
                note=row.note,
                link_path=_LINK,
            )
        except Exception:  # delivery must never undo the award
            logger.warning("recognition_email_failed", extra={"recognition_id": str(row.id)})

    async def feed(
        self,
        caller: CurrentUser,
        *,
        employee_id: uuid.UUID | None = None,
        badge: RecognitionBadge | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RecognitionRead]:
        """The org-wide feed, newest first, each card with its thread."""
        rows = await self._recognitions.feed(
            employee_id=employee_id, badge=badge, limit=limit, offset=offset
        )
        threads = await self._recognitions.comments_for([r.id for r in rows])
        people = await self._people_in(rows, threads)
        return [self._to_read(caller, row, threads.get(row.id, []), people) for row in rows]

    async def _people_in(
        self,
        rows: Sequence[Recognition],
        threads: dict[uuid.UUID, list[RecognitionComment]],
    ) -> dict[uuid.UUID, Employee]:
        """Everyone named anywhere on the page, in one lookup."""
        ids: set[uuid.UUID] = set()
        for row in rows:
            ids.add(row.employee_id)
            if row.awarded_by_id:
                ids.add(row.awarded_by_id)
        for thread in threads.values():
            ids.update(c.author_id for c in thread if c.author_id)
        return await self._employees.get_many(list(ids))

    def _to_read(
        self,
        caller: CurrentUser,
        row: Recognition,
        thread: list[RecognitionComment],
        people: dict[uuid.UUID, Employee],
    ) -> RecognitionRead:
        spec = BY_BADGE[row.badge]
        recipient = people.get(row.employee_id)
        awarder = people.get(row.awarded_by_id) if row.awarded_by_id else None
        return RecognitionRead.model_validate(row).model_copy(
            update={
                "employee_name": recipient.full_name if recipient else None,
                "employee_avatar_url": getattr(recipient, "avatar_url", None),
                "awarded_by_name": awarder.full_name if awarder else None,
                "emoji": spec.emoji,
                "badge_label": spec.label,
                "can_delete": _may_unaward(caller, row),
                "comments": [self._comment_read(caller, c, people) for c in thread],
            }
        )

    def _comment_read(
        self,
        caller: CurrentUser,
        comment: RecognitionComment,
        people: dict[uuid.UUID, Employee],
    ) -> RecognitionCommentRead:
        author = people.get(comment.author_id) if comment.author_id else None
        return RecognitionCommentRead.model_validate(comment).model_copy(
            update={
                "author_name": author.full_name if author else None,
                "author_avatar_url": getattr(author, "avatar_url", None),
                "can_delete": _may_delete_comment(caller, comment),
            }
        )

    async def comment(
        self, caller: CurrentUser, recognition_id: uuid.UUID, payload: RecognitionCommentCreate
    ) -> RecognitionComment:
        """Anyone may congratulate. The feed is org-wide, so the thread is too."""
        row = await self._recognitions.get(recognition_id)
        if row is None:
            raise NotFoundError()
        comment = await self._recognitions.add_comment(
            recognition_id=row.id, author_id=caller.employee_id, body=payload.body.strip()
        )
        if row.employee_id != caller.employee_id:
            await self._notifications.notify(
                recipient_id=row.employee_id,
                kind=NotificationKind.SYSTEM,
                title="New comment on your recognition",
                body=payload.body.strip()[:200],
                link=_LINK,
                entity_type="recognition",
                entity_id=row.id,
                actor_id=caller.employee_id,
            )
        return comment

    async def delete_comment(self, caller: CurrentUser, comment_id: uuid.UUID) -> None:
        comment = await self._recognitions.get_comment(comment_id)
        if comment is None:
            raise NotFoundError()
        if not _may_delete_comment(caller, comment):
            raise AuthorizationError()
        await self._recognitions.delete_comment(comment)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="recognition.comment_deleted",
            target=f"recognition_comment:{comment_id}",
        )

    async def unaward(self, caller: CurrentUser, recognition_id: uuid.UUID) -> None:
        """Take back a recognition given by mistake (the awarder, HR or admin)."""
        row = await self._recognitions.get(recognition_id)
        if row is None:
            raise NotFoundError()
        if not _may_unaward(caller, row):
            raise AuthorizationError()
        await self._audit.append(
            actor=str(caller.employee_id),
            action="recognition.unaward",
            target=f"recognition:{row.id}:{row.employee_id}:{row.badge.value}",
        )
        await self._recognitions.delete(row)


def _may_award(caller: CurrentUser, recipient: Employee) -> bool:
    """HR and admin may recognise anyone; everyone else their own reports.

    Being *a* manager is not the permission - being THEIR manager is. A badge
    from someone who never saw the work says nothing, and this mirrors how leave,
    regularization and out-of-office already decide scope.
    """
    if caller.employee_id == recipient.id:
        return False
    if caller.role in (Role.ADMIN, Role.HR):
        return True
    return recipient.manager_id == caller.employee_id


def _may_unaward(caller: CurrentUser, row: Recognition) -> bool:
    """The person who gave it, or HR/admin. Not the recipient - quietly deleting
    recognition you disagree with is not theirs to do, and never the other way:
    nobody should be able to erase someone else's record."""
    return caller.role in (Role.ADMIN, Role.HR) or row.awarded_by_id == caller.employee_id


def _may_delete_comment(caller: CurrentUser, comment: RecognitionComment) -> bool:
    """Your own comment, or HR/admin moderating."""
    return caller.role in (Role.ADMIN, Role.HR) or comment.author_id == caller.employee_id
