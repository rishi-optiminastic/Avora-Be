"""The published badge list: emoji, name, and when each one fits.

Kept in code, not the database. These are an agreed vocabulary with meanings the
company has written down - not org data somebody edits in a settings screen - and
the dashboard needs the same labels the award form offers, so a single source
here beats duplicating the strings in the frontend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models.recognition import RecognitionBadge, RecognitionCadence, RecognitionSource


@dataclass(frozen=True)
class BadgeSpec:
    badge: RecognitionBadge
    emoji: str
    label: str
    cadence: RecognitionCadence
    when_to_use: str
    #: What it is worth when the recipient's own line awards it. A peer award
    #: on the same badge is worth `PEER_SHARE` of this.
    points: int


# A peer award is worth half. Peers see the work most closely and should be
# able to recognise it, but a badge that costs the giver nothing and carries
# the same weight as one from the person accountable for the work would make
# points mean only "how many friends do you have".
PEER_SHARE = 0.5

# How many peer awards one person may give per calendar month.
#
# Points buy real things, so the supply has to be bounded or the catalogue
# prices itself out in a week. A manager recognising their own reports is not
# limited - that is the job, and it is already visible to the whole org.
PEER_AWARDS_PER_MONTH = 5


def month_start(now: datetime | None = None) -> datetime:
    """The first of the current calendar month, UTC.

    Calendar rather than rolling, so everyone's allowance refills on the same
    visible date and nobody has to work out when their own window reopens.

    UTC rather than the org's timezone, deliberately: the allowance is a guard
    rail, not a deadline. The only consequence is that it refills at 05:30 IST
    on the 1st rather than midnight, which costs nobody anything - where the
    org's clock genuinely matters (a probation window, a payroll month) the
    code reads it, and this is not that.

    Defined here, beside the number it bounds, because two services ask the
    question and two implementations of "when does the month start" is one
    that will eventually disagree with the other.
    """
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime | None = None) -> datetime:
    """Midnight on the Monday of the current week, UTC.

    Monday because the badge list itself is written around a working week
    ("Win of the Week"), and a week that turns over on Sunday would put
    Monday's win in the previous one.
    """
    moment = now or datetime.now(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


BADGES: tuple[BadgeSpec, ...] = (
    BadgeSpec(
        RecognitionBadge.CONSISTENCY_STAR,
        "🔥",
        "Consistency Star",
        RecognitionCadence.MONTHLY,
        "Consistently delivering quality work, meeting goals, and demonstrating "
        "reliability throughout the month.",
        50,
    ),
    BadgeSpec(
        RecognitionBadge.LEARNING_WIN,
        "🌱",
        "Learning Win",
        RecognitionCadence.MONTHLY,
        "Learned and applied a new skill that benefits their work.",
        40,
    ),
    BadgeSpec(
        RecognitionBadge.CULTURE_CHAMPION,
        "❤️",
        "Culture Champion",
        RecognitionCadence.MONTHLY,
        "A positive contribution to workplace culture.",
        40,
    ),
    BadgeSpec(
        RecognitionBadge.MOST_IMPROVED,
        "📈",
        "Most Improved",
        RecognitionCadence.MONTHLY,
        "Significant improvement in performance, skills, or contribution over the month.",
        50,
    ),
    BadgeSpec(
        RecognitionBadge.CLIENT_APPRECIATION,
        "🙌",
        "Client Appreciation",
        RecognitionCadence.MONTHLY,
        "A client specifically appreciated their contribution.",
        60,
    ),
    BadgeSpec(
        RecognitionBadge.WIN_OF_THE_WEEK,
        "⭐",
        "Win of the Week",
        RecognitionCadence.WEEKLY,
        "A meaningful achievement or successful weekly milestone.",
        20,
    ),
    BadgeSpec(
        RecognitionBadge.ABOVE_AND_BEYOND,
        "🚀",
        "Above & Beyond",
        RecognitionCadence.WEEKLY,
        "Went beyond the assigned responsibilities or took exceptional ownership.",
        30,
    ),
    BadgeSpec(
        RecognitionBadge.TEAM_PLAYER,
        "🤝",
        "Team Player",
        RecognitionCadence.WEEKLY,
        "Supported another employee or team.",
        20,
    ),
    BadgeSpec(
        RecognitionBadge.IDEA_OF_THE_WEEK,
        "💡",
        "Idea of the Week",
        RecognitionCadence.WEEKLY,
        "A creative idea, process improvement, or a smarter way of working.",
        25,
    ),
    BadgeSpec(
        RecognitionBadge.TARGET_CRUSHER,
        "🎯",
        "Target Crusher",
        RecognitionCadence.WEEKLY,
        "Exceeded a measurable target or delivered exceptional results.",
        30,
    ),
)

BY_BADGE: dict[RecognitionBadge, BadgeSpec] = {spec.badge: spec for spec in BADGES}


def points_for(badge: RecognitionBadge, source: RecognitionSource) -> int:
    """What one award is worth. Snapshot onto the row - never read back later.

    Rounded DOWN so a peer award can never be worth more than half, and never
    below 1: a badge worth nothing is not a reward, it is an insult.
    """
    full = BY_BADGE[badge].points
    if source is RecognitionSource.MANAGER:
        return full
    return max(1, int(full * PEER_SHARE))


def label_for(badge: RecognitionBadge) -> str:
    """ "🔥 Consistency Star" — for an email subject or a notification line."""
    spec = BY_BADGE[badge]
    return f"{spec.emoji} {spec.label}"
