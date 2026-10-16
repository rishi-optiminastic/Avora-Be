"""Pure capture-health rules: is a device's agent actually capturing?

No I/O here, so the verdict is identical wherever it is computed (the health
report, the activity ingest stale check, the admin read). The dashboard shows
what this decides; it never re-derives it (Security rule 5.1).

A device NEEDS ATTENTION when any of:
  * a capability is broken: screen capture failing/denied, window info
    empty/denied, browser URL empty/denied ("unsupported" and "unknown" are fine:
    some browsers expose no URL, and "unknown" is a probe that has not run yet);
  * its last report is older than STALE_REPORT_AFTER while it is still sending
    activity (the agent is alive but its health loop is not);
  * the offline queue's oldest item is older than QUEUE_BACKLOG_AFTER (captures
    are piling up on the laptop instead of reaching us).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.schemas.device import HealthStatus

STALE_REPORT_AFTER = timedelta(minutes=30)
QUEUE_BACKLOG_AFTER = timedelta(hours=1)

# (field, bad state) -> (issue code, plain-language summary for admins).
_CAPABILITY_ISSUES: dict[tuple[str, str], tuple[str, str]] = {
    ("screen_capture", "failing"): (
        "screen_capture_failing",
        "Screenshots are failing",
    ),
    ("screen_capture", "permission_denied"): (
        "screen_capture_permission_denied",
        "Screen recording permission is off",
    ),
    ("window_info", "empty"): (
        "window_info_empty",
        "Window titles are coming back empty",
    ),
    ("window_info", "permission_denied"): (
        "window_info_permission_denied",
        "Accessibility permission is off, so window titles are hidden",
    ),
    ("browser_url", "empty"): (
        "browser_url_empty",
        "Browser addresses are coming back empty",
    ),
    ("browser_url", "permission_denied"): (
        "browser_url_permission_denied",
        "Browser automation permission is off, so site addresses are hidden",
    ),
}
_STALE_ISSUE = ("report_stale", "Agent is running but stopped reporting its health")
_BACKLOG_ISSUE = ("queue_backlog", "Captures are stuck in the offline queue")
_CAPABILITY_FIELDS = ("screen_capture", "window_info", "browser_url")


class HealthSnapshot(Protocol):
    """The device columns the rules read (the ORM row satisfies this)."""

    @property
    def health_screen_capture(self) -> str | None: ...
    @property
    def health_window_info(self) -> str | None: ...
    @property
    def health_browser_url(self) -> str | None: ...
    @property
    def health_oldest_queued_age_seconds(self) -> int | None: ...
    @property
    def health_reported_at(self) -> datetime | None: ...
    @property
    def last_seen_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class HealthIssue:
    code: str
    summary: str


@dataclass(frozen=True)
class HealthAssessment:
    status: HealthStatus
    issues: tuple[HealthIssue, ...]

    @property
    def needs_attention(self) -> bool:
        return self.status == "needs_attention"


def _aware(value: datetime) -> datetime:
    """SQLite (tests) hands back naive datetimes; every stored time is UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _capability_issues(device: HealthSnapshot) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    for field in _CAPABILITY_FIELDS:
        state = getattr(device, f"health_{field}")
        found = _CAPABILITY_ISSUES.get((field, state or ""))
        if found is not None:
            issues.append(HealthIssue(*found))
    return issues


def is_report_stale(device: HealthSnapshot, now: datetime) -> bool:
    """Reported before, nothing for STALE_REPORT_AFTER, yet activity keeps
    arriving inside that same window."""
    reported_at = device.health_reported_at
    if reported_at is None or now - _aware(reported_at) <= STALE_REPORT_AFTER:
        return False
    seen = device.last_seen_at
    return seen is not None and now - _aware(seen) <= STALE_REPORT_AFTER


def _queue_backlogged(device: HealthSnapshot) -> bool:
    age = device.health_oldest_queued_age_seconds
    return age is not None and age > QUEUE_BACKLOG_AFTER.total_seconds()


def assess(device: HealthSnapshot, now: datetime) -> HealthAssessment:
    """The server's verdict on one device's capture health at `now`."""
    if device.health_reported_at is None:
        return HealthAssessment(status="no_report", issues=())
    issues = _capability_issues(device)
    if is_report_stale(device, now):
        issues.append(HealthIssue(*_STALE_ISSUE))
    if _queue_backlogged(device):
        issues.append(HealthIssue(*_BACKLOG_ISSUE))
    if not issues:
        return HealthAssessment(status="healthy", issues=())
    return HealthAssessment(status="needs_attention", issues=tuple(issues))


def owner_fix_hint(issues: tuple[HealthIssue, ...]) -> str:
    """One plain-language sentence telling the device owner what to do."""
    codes = {issue.code for issue in issues}
    if codes & {"screen_capture_failing", "screen_capture_permission_denied"}:
        return (
            "Avora could not take screenshots on your laptop. Allow screen recording "
            "for Avora in your system settings, restart the Avora agent, or contact IT."
        )
    if any(code.endswith("permission_denied") for code in codes):
        return (
            "Avora is missing a permission on your laptop. Allow it for Avora in your "
            "system settings, restart the Avora agent, or contact IT."
        )
    return (
        "The Avora agent on your laptop is not working properly. Restart the Avora "
        "agent or contact IT."
    )
