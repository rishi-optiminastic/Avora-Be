"""Idle minutes come from the SHARE of idle samples, not a sum of the idle gauge.

Regression: `idle_seconds` is a gauge that ramps up while idle, so summing it
across a day's samples wildly overcounts — a short break could zero out an
otherwise-active day. We now use idle_samples / sample_count.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.repositories.activity import DailyAgg, idle_minutes


def _agg(sample_count: int, idle_samples: int) -> DailyAgg:
    now = datetime(2026, 6, 27, tzinfo=UTC)
    return DailyAgg(
        login_at=now, logout_at=now, sample_count=sample_count, idle_samples=idle_samples
    )


def test_active_all_day_with_short_break_stays_mostly_active() -> None:
    # 480 samples over the day, ~40 of them idle (a lunch break) → ~8% idle, not 100%.
    idle = idle_minutes(_agg(sample_count=480, idle_samples=40), worked_minutes=420)
    assert idle == 35  # 420 * 40/480
    assert 420 - idle == 385  # the day reads ~92% active, not 0%


def test_no_idle_samples_is_zero_idle() -> None:
    assert idle_minutes(_agg(sample_count=480, idle_samples=0), worked_minutes=420) == 0


def test_all_idle_caps_at_worked() -> None:
    assert idle_minutes(_agg(sample_count=100, idle_samples=100), worked_minutes=420) == 420


def test_no_samples_is_zero() -> None:
    assert idle_minutes(_agg(sample_count=0, idle_samples=0), worked_minutes=420) == 0
