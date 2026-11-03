"""The published badge list: emoji, name, and when each one fits.

Kept in code, not the database. These are an agreed vocabulary with meanings the
company has written down - not org data somebody edits in a settings screen - and
the dashboard needs the same labels the award form offers, so a single source
here beats duplicating the strings in the frontend.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.recognition import RecognitionBadge, RecognitionCadence


@dataclass(frozen=True)
class BadgeSpec:
    badge: RecognitionBadge
    emoji: str
    label: str
    cadence: RecognitionCadence
    when_to_use: str


BADGES: tuple[BadgeSpec, ...] = (
    BadgeSpec(
        RecognitionBadge.CONSISTENCY_STAR,
        "🔥",
        "Consistency Star",
        RecognitionCadence.MONTHLY,
        "Consistently delivering quality work, meeting goals, and demonstrating "
        "reliability throughout the month.",
    ),
    BadgeSpec(
        RecognitionBadge.LEARNING_WIN,
        "🌱",
        "Learning Win",
        RecognitionCadence.MONTHLY,
        "Learned and applied a new skill that benefits their work.",
    ),
    BadgeSpec(
        RecognitionBadge.CULTURE_CHAMPION,
        "❤️",
        "Culture Champion",
        RecognitionCadence.MONTHLY,
        "A positive contribution to workplace culture.",
    ),
    BadgeSpec(
        RecognitionBadge.MOST_IMPROVED,
        "📈",
        "Most Improved",
        RecognitionCadence.MONTHLY,
        "Significant improvement in performance, skills, or contribution over the month.",
    ),
    BadgeSpec(
        RecognitionBadge.CLIENT_APPRECIATION,
        "🙌",
        "Client Appreciation",
        RecognitionCadence.MONTHLY,
        "A client specifically appreciated their contribution.",
    ),
    BadgeSpec(
        RecognitionBadge.WIN_OF_THE_WEEK,
        "⭐",
        "Win of the Week",
        RecognitionCadence.WEEKLY,
        "A meaningful achievement or successful weekly milestone.",
    ),
    BadgeSpec(
        RecognitionBadge.ABOVE_AND_BEYOND,
        "🚀",
        "Above & Beyond",
        RecognitionCadence.WEEKLY,
        "Went beyond the assigned responsibilities or took exceptional ownership.",
    ),
    BadgeSpec(
        RecognitionBadge.TEAM_PLAYER,
        "🤝",
        "Team Player",
        RecognitionCadence.WEEKLY,
        "Supported another employee or team.",
    ),
    BadgeSpec(
        RecognitionBadge.IDEA_OF_THE_WEEK,
        "💡",
        "Idea of the Week",
        RecognitionCadence.WEEKLY,
        "A creative idea, process improvement, or a smarter way of working.",
    ),
    BadgeSpec(
        RecognitionBadge.TARGET_CRUSHER,
        "🎯",
        "Target Crusher",
        RecognitionCadence.WEEKLY,
        "Exceeded a measurable target or delivered exceptional results.",
    ),
)

BY_BADGE: dict[RecognitionBadge, BadgeSpec] = {spec.badge: spec for spec in BADGES}


def label_for(badge: RecognitionBadge) -> str:
    """ "🔥 Consistency Star" — for an email subject or a notification line."""
    spec = BY_BADGE[badge]
    return f"{spec.emoji} {spec.label}"
