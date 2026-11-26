"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest

from custom_components.posti.account.parcels import normalize_account_parcel
from custom_components.posti.const import ParcelStatus
from custom_components.posti.tracking.parcels import normalize_parcel, parse_iso

from .account.payloads import full_shipment
from .tracking.payloads import delivered_hit, returned_hit, unknown_status_hit

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]
CODE = "JJFI00000000000001"
POINT = {"address": {"publicName": "Example Store", "city": "ESPOO"}}


def _tracking(hit: dict) -> dict:
    return normalize_parcel(hit, tracking_code=CODE)


def _moment_hit() -> dict:
    hit = unknown_status_hit()
    hit["delivery"] = {"time": {"type": "X", "timestamp": "2099-05-03T08:00:00.000Z"}}
    return hit


def _date_hit() -> dict:
    hit = unknown_status_hit()
    hit["delivery"] = {"time": {"type": "X", "timestamp": "2099-05-03"}}
    return hit


def _window_hit() -> dict:
    hit = unknown_status_hit()
    hit["delivery"] = {
        "time": {
            "type": "X",
            "timestamp": "2099-05-03T08:00:00.000Z",
            "timestampLatest": "2099-05-03T10:00:00.000Z",
        }
    }
    return hit


def _collected_hit() -> dict:
    """A pickup-point parcel that was collected: the point's event is older."""
    hit = delivered_hit()
    hit["pickupPoint"] = POINT
    hit["events"].insert(
        1,
        {
            "timestamp": "2026-04-29T09:00:00.000Z",
            "eventDescription": "Item is ready for a pick up",
            "city": "Parcel locker, Example Store",
        },
    )
    return hit


def _collected_without_block() -> dict:
    hit = _collected_hit()
    del hit["pickupPoint"]
    return hit


def _account(phase: str, **extra) -> dict:
    return normalize_account_parcel(full_shipment(phase) | extra)


SAMPLES = {
    "tracking_delivered": lambda: _tracking(delivered_hit()),
    "tracking_returned": lambda: _tracking(returned_hit()),
    "tracking_unknown": lambda: _tracking(unknown_status_hit()),
    "tracking_moment": lambda: _tracking(_moment_hit()),
    "tracking_date": lambda: _tracking(_date_hit()),
    "tracking_window": lambda: _tracking(_window_hit()),
    "tracking_collected": lambda: _tracking(_collected_hit()),
    "account_active": lambda: _account("IN_DELIVERY"),
    "account_pickup": lambda: _account("READY_FOR_PICKUP"),
    "account_delivered": lambda: _account("DELIVERED"),
    "account_returning": lambda: _account("RETURN_IN_TRANSPORT"),
    "account_return_delivered": lambda: _account("RETURN_DELIVERED"),
    "account_bare": lambda: normalize_account_parcel({"shipmentId": "S1"}),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_rule(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = _tracking(_collected_hit())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Parcel locker, Example Store"


def test_pickup_is_sticky_from_the_events_alone():
    parcel = _tracking(_collected_without_block())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_a_home_delivery_is_not_pickup():
    assert _tracking(delivered_hit())["pickup"] is False


def test_a_returned_pickup_parcel_stays_returning_and_not_delivered():
    parcel = _tracking(returned_hit())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["pickup"] is True


def test_a_return_that_is_still_travelling_is_not_delivered():
    parcel = _account("RETURN_IN_TRANSPORT")
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_an_account_pickup_point_makes_the_route_pickup():
    parcel = _account("IN_TRANSPORT")
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Store"


def test_an_account_parcel_without_a_point_is_not_pickup():
    shipment = full_shipment("IN_TRANSPORT")
    del shipment["pickupPoint"]
    parcel = normalize_account_parcel(shipment)
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_window_keeps_both_ends():
    parcel = _tracking(_window_hit())
    assert parcel["planned_from"] == "2099-05-03T08:00:00.000Z"
    assert parcel["planned_to"] == "2099-05-03T10:00:00.000Z"


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = _tracking(_moment_hit())
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.utcoffset().total_seconds() == 3 * 3600
    assert end.date() == parse_iso(parcel["planned_from"]).astimezone(end.tzinfo).date()


def test_a_date_covers_the_whole_day():
    parcel = _tracking(_date_hit())
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
