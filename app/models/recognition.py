"""Recognition — a manager marking something an employee did well.

A badge on its own says very little, so every award carries a note saying what
earned it. The whole org can see the result and comment on it: recognition that
only the recipient sees is just a private message, and the point of the
programme is that colleagues can add to it.

The ten badges are fixed in code rather than seeded as rows. They are a published
list with agreed meanings, not org data someone edits - and a typo in a seeded
row would quietly create an eleventh badge nobody can award.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RecognitionCadence(StrEnum):
    """Which list a badge belongs to. Guidance on when it fits, not a limit:
    nobody should be blocked from recognising good work by a counter."""

    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RecognitionSource(StrEnum):
    """Who gave it, in terms of standing rather than job title.

    MANAGER is the recipient's own reporting line, HR or an admin - someone
    accountable for the work. PEER is anyone else in the org. They are worth
    different points on purpose: opening awarding to everyone is what makes the
    programme usable at all, but a badge from the person who signs off your
    work should not be worth the same as one from the desk next to you.
    """

    MANAGER = "manager"
    PEER = "peer"


class RecognitionBadge(StrEnum):
    # Monthly
    CONSISTENCY_STAR = "consistency_star"
    LEARNING_WIN = "learning_win"
    CULTURE_CHAMPION = "culture_champion"
    MOST_IMPROVED = "most_improved"
    CLIENT_APPRECIATION = "client_appreciation"
    # Weekly
    WIN_OF_THE_WEEK = "win_of_the_week"
    ABOVE_AND_BEYOND = "above_and_beyond"
    TEAM_PLAYER = "team_player"
    IDEA_OF_THE_WEEK = "idea_of_the_week"
    TARGET_CRUSHER = "target_crusher"


class Recognition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "recognitions"
    __table_args__ = (
        Index("ix_recognitions_employee_created", "employee_id", "created_at"),
        Index("ix_recognitions_badge", "badge"),
    )

    # Who was recognised.
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    # Who awarded it. SET NULL on offboard: the recognition outlives the manager
    # who gave it, and deleting their account must not erase someone's record.
    awarded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    badge: Mapped[RecognitionBadge] = mapped_column()
    # What earned it. Required - "you got a badge" with no reason is hollow, and
    # it is the part colleagues actually respond to.
    note: Mapped[str] = mapped_column(String(1000))

    # Whether the awarder was the recipient's line or a colleague, decided at
    # award time. Stored rather than re-derived, because `manager_id` moves:
    # a reorg must not retroactively turn a manager's badge into a peer one.
    source: Mapped[RecognitionSource] = mapped_column(default=RecognitionSource.MANAGER)
    # What this award was worth, SNAPSHOT at award time.
    #
    # Never recomputed from the badge. The point values are editable policy and
    # people spend these on real things - revaluing history would move somebody's
    # balance under them, and a balance that changes without a transaction is
    # the one thing a rewards programme cannot afford.
    points: Mapped[int] = mapped_column(Integer, default=0)


class RecognitionComment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A colleague congratulating or adding to a recognition."""

    __tablename__ = "recognition_comments"
    __table_args__ = (
        Index("ix_recognition_comments_recognition_created", "recognition_id", "created_at"),
    )

    recognition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recognitions.id", ondelete="CASCADE"), index=True
    )
    # SET NULL on offboard so the thread still reads, with the author unknown.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    body: Mapped[str] = mapped_column(String(1000))
