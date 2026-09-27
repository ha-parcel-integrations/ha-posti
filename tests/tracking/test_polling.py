"""Tests for the tracking coordinator's dynamic-polling helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.posti.const import ParcelStatus
from custom_components.posti.tracking.coordinator import (
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 15, hour, minute, tzinfo=timezone.utc)


def test_in_quiet_window():
    assert _in_quiet_window(_dt(2)) is True
    assert _in_quiet_window(_dt(6)) is False
    assert _in_quiet_window(_dt(12)) is False


def test_next_anchor_before_six():
    assert _next_anchor(_dt(3)) == _dt(6)


def test_next_anchor_after_six_is_midnight_tomorrow():
    anchor = _next_anchor(_dt(12))
    assert anchor.hour == 0
    assert anchor.day == 16


def test_hottest_tier_none_without_active_parcels():
    assert _hottest_tier_minutes([], _dt(12)) is None


def test_hottest_tier_mid_when_nothing_out_for_delivery():
    parcels = [{"status": ParcelStatus.IN_TRANSIT, "planned_from": None}]
    assert _hottest_tier_minutes(parcels, _dt(12)) == MID_INTERVAL_MINUTES


def test_hottest_tier_hot_without_planned_from():
    parcels = [{"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": None}]
    assert _hottest_tier_minutes(parcels, _dt(12)) == HOT_INTERVAL_MINUTES


def test_hottest_tier_hot_with_unparseable_planned_from():
    parcels = [{"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": "garbage"}]
    assert _hottest_tier_minutes(parcels, _dt(12)) == HOT_INTERVAL_MINUTES


def test_hottest_tier_hot_within_lookahead():
    parcels = [
        {
            "status": ParcelStatus.OUT_FOR_DELIVERY,
            "planned_from": _dt(12, 30).isoformat(),
        }
    ]
    assert _hottest_tier_minutes(parcels, _dt(12)) == HOT_INTERVAL_MINUTES


def test_hottest_tier_mid_when_out_for_delivery_is_far_away():
    parcels = [
        {
            "status": ParcelStatus.OUT_FOR_DELIVERY,
            "planned_from": _dt(20).isoformat(),
        }
    ]
    assert _hottest_tier_minutes(parcels, _dt(12)) == MID_INTERVAL_MINUTES


def test_next_update_interval_none_tier_suspends():
    assert _next_update_interval(_dt(12), None, "entry1") is None


def test_next_update_interval_in_quiet_window_jumps_to_anchor():
    result = _next_update_interval(_dt(2), HOT_INTERVAL_MINUTES, "entry1")
    assert result == _next_anchor(_dt(2)) - _dt(2)


def test_next_update_interval_candidate_falling_into_quiet_window():
    # Just before midnight with a long tier: the candidate lands after 00:00.
    result = _next_update_interval(_dt(23, 58), MID_INTERVAL_MINUTES, "entry1")
    assert result == _next_anchor(_dt(23, 58)) - _dt(23, 58)


def test_next_update_interval_normal_case_includes_stagger():
    now = _dt(12)
    result = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry1")
    stagger = timedelta(minutes=_stagger_minutes("entry1"))
    assert result == timedelta(minutes=HOT_INTERVAL_MINUTES) + stagger


def test_stagger_minutes_is_deterministic():
    assert _stagger_minutes("same") == _stagger_minutes("same")
