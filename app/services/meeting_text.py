"""How a meeting request is described to a person.

Two helpers, shared by the request service and the scheduler worker, because
both send notifications about the same rows and the rules below are not the
kind of thing that should exist in two copies: one of them would eventually
start leaking a confidential title, or printing a meeting in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.time import as_utc
from app.models.meeting_request import MeetingCategory, MeetingRequest


def subject(row: MeetingRequest) -> str:
    """What a request may be CALLED in a notification.

    A Personal / Confidential request keeps its title out of every inbox
    preview: the person who raised it chose that category precisely so its
    subject would not be visible to anyone but the people deciding it.
    """
    if row.category is MeetingCategory.PERSONAL_CONFIDENTIAL:
        return "a confidential request"
    return row.title


def in_request_zone(moment: datetime | None, timezone: str) -> str:
    """A time in the request's own zone, for a person to read.

    Rendered in the zone the request was composed in rather than UTC: a
    notification that says 05:30 for an 11:00 meeting is worse than one that
    gives no time at all. An unknown zone name falls back to UTC rather than
    raising - a reminder that is an hour wrong still beats no reminder.

    `%-d` is deliberately not used: it is a glibc/BSD extension, and the day is
    interpolated instead so the same string is produced wherever this runs.
    """
    if moment is None:
        return ""
    zone: tzinfo
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    local = as_utc(moment).astimezone(zone)
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"


def when_label(row: MeetingRequest) -> str:
    """The agreed time on a request, or an empty string when it has none."""
    return in_request_zone(row.scheduled_start_at, row.timezone)
