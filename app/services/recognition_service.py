"""Recognition business rules.

A manager marks something one of their reports did well; the whole org sees it
and can add to the thread.

The rules that carry weight:

* **Anyone may recognise anyone.** Awarding used to be limited to a recipient's
  own reporting line plus HR and admin. On a 29-person org that was 8 possible
  givers and 21 people with no way to recognise anybody, and the programme was
  used once in its lifetime. The people who see the work most closely are
  usually not the ones who sign it off.
* **But not all awards weigh the same.** An award from the recipient's line
  (their manager, HR, an admin) is MANAGER and carries the badge's full points;
  anyone else is PEER and carries half. Both are real recognition; only one is
  from the person accountable for the work.
* **Peer awards are rationed, manager ones are not.** `PEER_AWARDS_PER_MONTH`
  per giver per calendar month. Points buy real things, so the supply has to be
  bounded or the catalogue prices itself out in a week. A manager recognising
  their own reports is the job and is already visible to the whole org.
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
from app.core.recognition_badges import (
    BY_BADGE,
    PEER_AWARDS_PER_MONTH,
    label_for,
    month_start,
    points_for,
)
from app.models.employee import Employee, Role
from app.models.notification import NotificationKind
from app.models.recognition import (
    Recognition,
    RecognitionBadge,
    RecognitionComment,
    RecognitionSource,
)
from app.repositories.audit import AuditRepository
from app.repositories.employee import EmployeeRepository
from app.repositories.recognition import RecognitionRepository
from app.repositories.reward import RewardRepository
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
        rewards: RewardRepository,
    ) -> None:
        self._recognitions = recognitions
        self._employees = employees
        self._audit = audit
        self._notifications = notifications
        self._email = email
        # Required, not optional. `unaward` has to know what the recipient has
        # already committed before it takes points away, and a default of None
        # would have made that guard silently skip - the one failure mode that
        # leaves somebody owing Avora points they were told they had.
        self._rewards = rewards

    async def award(self, caller: CurrentUser, payload: RecognitionCreate) -> Recognition:
        recipient = await self._employees.get(payload.employee_id)
        if recipient is None or not recipient.is_active:
            raise NotFoundError()
        if recipient.id == caller.employee_id:
            raise ValidationError("You can't recognise yourself.")
        source = _award_source(caller, recipient)
        if source is RecognitionSource.PEER:
            await self._check_peer_allowance(caller.employee_id)

        row = await self._recognitions.create(
            employee_id=recipient.id,
            awarded_by_id=caller.employee_id,
            badge=payload.badge,
            note=payload.note.strip(),
            source=source,
            # Snapshot. Never read back from the badge: these are spent on real
            # things, and repricing a badge must not move a balance behind
            # somebody's back.
            points=points_for(payload.badge, source),
        )
        await self._audit.append(
            actor=str(caller.employee_id),
            action="recognition.award",
            target=f"recognition:{row.id}:{recipient.id}:{payload.badge.value}",
        )
        await self._announce(caller, row, recipient)
        return row

    async def _check_peer_allowance(self, awarder_id: uuid.UUID) -> None:
        """Refuse a peer award once this month's allowance is used up.

        Counted from the first of the CALENDAR month rather than a rolling 30
        days, so everyone's allowance refills on the same visible date and
        nobody has to work out when their window reopens.
        """
        given = await self._recognitions.peer_awards_given_since(awarder_id, month_start())
        if given >= PEER_AWARDS_PER_MONTH:
            raise ValidationError(
                f"You have used all {PEER_AWARDS_PER_MONTH} of your peer "
                "recognitions this month. They refill on the 1st."
            )

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
        await self._check_not_already_spent(row)
        await self._audit.append(
            actor=str(caller.employee_id),
            action="recognition.unaward",
            target=f"recognition:{row.id}:{row.employee_id}:{row.badge.value}",
        )
        await self._recognitions.delete(row)

    async def _check_not_already_spent(self, row: Recognition) -> None:
        """Refuse to take back points the recipient has already committed.

        Recognition used to be free to delete because it was only a badge.
        It now carries points that buy real things, so removing one has to
        answer for the balance: taking back 50 points somebody has already
        put towards a reward would leave them owing Avora money they were
        told they had.

        The check is on what is LEFT, not on this award specifically - points
        are fungible, and there is no way to say which 50 were spent.
        """
        earned = await self._recognitions.points_earned(row.employee_id)
        spent = await self._rewards.points_spent_by(row.employee_id)
        if earned - row.points < spent:
            raise ValidationError(
                "They have already put these points towards a reward. Decline "
                "or cancel that redemption first, then take this back."
            )


def _award_source(caller: CurrentUser, recipient: Employee) -> RecognitionSource:
    """How much standing this award carries - not whether it is allowed.

    Everyone may recognise everyone; what differs is the weight. MANAGER means
    the caller is accountable for this person's work: their reporting manager,
    HR, or an admin. Anyone else is a PEER.

    Being *a* manager is not what counts - being THEIR manager is, the same
    `manager_id` edge that decides scope for leave, regularization and
    out-of-office.
    """
    if caller.role in (Role.ADMIN, Role.HR) or recipient.manager_id == caller.employee_id:
        return RecognitionSource.MANAGER
    return RecognitionSource.PEER


def _may_unaward(caller: CurrentUser, row: Recognition) -> bool:
    """The person who gave it, or HR/admin. Not the recipient - quietly deleting
    recognition you disagree with is not theirs to do, and never the other way:
    nobody should be able to erase someone else's record."""
    return caller.role in (Role.ADMIN, Role.HR) or row.awarded_by_id == caller.employee_id


def _may_delete_comment(caller: CurrentUser, comment: RecognitionComment) -> bool:
    """Your own comment, or HR/admin moderating."""
    return caller.role in (Role.ADMIN, Role.HR) or comment.author_id == caller.employee_id
