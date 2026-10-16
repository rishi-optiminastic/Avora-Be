"""Pure capture-health rules (no database)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.services.device_health_rules import assess, owner_fix_hint

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@dataclass
class _Snap:
    health_screen_capture: str | None = "ok"
    health_window_info: str | None = "ok"
    health_browser_url: str | None = "ok"
    health_oldest_queued_age_seconds: int | None = None
    health_reported_at: datetime | None = NOW
    last_seen_at: datetime | None = NOW


def test_never_reported_is_no_report() -> None:
    assert assess(_Snap(health_reported_at=None), NOW).status == "no_report"


def test_all_ok_is_healthy() -> None:
    verdict = assess(_Snap(), NOW)
    assert verdict.status == "healthy" and verdict.issues == ()


def test_each_broken_capability_is_an_issue() -> None:
    snap = _Snap(
        health_screen_capture="failing",
        health_window_info="permission_denied",
        health_browser_url="empty",
    )
    codes = [i.code for i in assess(snap, NOW).issues]
    assert codes == [
        "screen_capture_failing",
        "window_info_permission_denied",
        "browser_url_empty",
    ]


def test_stale_needs_recent_activity() -> None:
    old = NOW - timedelta(minutes=45)
    # Old report, but the device is also silent: it is off, not broken.
    assert assess(_Snap(health_reported_at=old, last_seen_at=old), NOW).status == "healthy"
    # Old report while activity keeps arriving: the health loop is dead.
    verdict = assess(_Snap(health_reported_at=old, last_seen_at=NOW), NOW)
    assert [i.code for i in verdict.issues] == ["report_stale"]


def test_stale_boundary_is_thirty_minutes() -> None:
    edge = NOW - timedelta(minutes=30)
    assert assess(_Snap(health_reported_at=edge), NOW).status == "healthy"


def test_naive_datetimes_are_treated_as_utc() -> None:
    old = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    verdict = assess(_Snap(health_reported_at=old, last_seen_at=NOW.replace(tzinfo=None)), NOW)
    assert verdict.needs_attention


def test_owner_hint_names_screenshots_for_capture_issues() -> None:
    snap = _Snap(health_screen_capture="failing")
    assert "screenshots" in owner_fix_hint(assess(snap, NOW).issues)
    generic = owner_fix_hint(assess(_Snap(health_oldest_queued_age_seconds=7200), NOW).issues)
    assert "Restart the Avora agent" in generic
