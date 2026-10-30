"""Tests for the OmaPosti account parcel normaliser."""

from __future__ import annotations

import logging

import pytest

from custom_components.posti.account import parcels
from custom_components.posti.account.parcels import (
    is_incoming,
    map_parcel_status,
    normalize_account_parcel,
)
from custom_components.posti.const import ParcelStatus
from custom_components.posti.tracking import parcels as tracking_parcels

from .payloads import SHIPMENT_NUMBER, TRACKING_NUMBER, event, full_shipment, shipment


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ORDER_RECEIVED", ParcelStatus.REGISTERED),
        ("WAITING", ParcelStatus.REGISTERED),
        ("RECEIVED", ParcelStatus.IN_TRANSIT),
        ("IN_TRANSPORT", ParcelStatus.IN_TRANSIT),
        ("IN_DELIVERY", ParcelStatus.OUT_FOR_DELIVERY),
        ("READY_FOR_PICKUP", ParcelStatus.AT_PICKUP_POINT),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("RETURN_DELIVERED", ParcelStatus.RETURNING),
    ],
)
def test_status_map(code, expected):
    assert map_parcel_status(code, "RECEIVER") is expected


def test_status_literals_agree_with_the_tracking_route():
    # Both routes read ``status.main``; the literals they share must not drift.
    for code in ("DELIVERED", "RETURN_DELIVERED"):
        assert parcels.STATUS_MAP[code] is tracking_parcels.STATUS_MAP[code]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("RETURN_WAITING", ParcelStatus.REGISTERED),
        ("RETURN_IN_TRANSPORT", ParcelStatus.IN_TRANSIT),
        ("RETURN_READY_FOR_PICKUP", ParcelStatus.AT_PICKUP_POINT),
        ("RETURN_DELIVERED", ParcelStatus.RETURNING),
    ],
)
def test_a_sent_parcel_coming_back_goes_through_the_stages(code, expected):
    assert map_parcel_status(code, "SENDER") is expected


def test_a_received_parcel_on_its_way_back_is_returning_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        status = map_parcel_status("RETURN_IN_TRANSPORT", "RECEIVER")
    assert status is ParcelStatus.RETURNING
    assert "RETURN_IN_TRANSPORT" in caplog.text


def test_unknown_literal_is_unknown_and_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status("MYSTERY", None) is ParcelStatus.UNKNOWN
        assert map_parcel_status("MYSTERY", None) is ParcelStatus.UNKNOWN
    assert caplog.text.count("MYSTERY") == 1


def test_missing_status_is_unknown_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status(None) is ParcelStatus.UNKNOWN
    assert caplog.text == ""


def test_sent_parcels_are_not_incoming_unless_they_come_back():
    assert is_incoming(shipment("IN_TRANSPORT", role="RECEIVER"))
    assert is_incoming(shipment("IN_TRANSPORT", role=None))
    assert not is_incoming(shipment("IN_TRANSPORT", role="SENDER"))
    assert not is_incoming(shipment("DELIVERED", role="SENDER"))
    assert is_incoming(shipment("RETURN_IN_TRANSPORT", role="SENDER"))


def test_barcode_prefers_display_id_then_shipment_id():
    assert normalize_account_parcel(shipment())["barcode"] == TRACKING_NUMBER
    parcel = normalize_account_parcel(shipment(tracking_number=None))
    assert parcel["barcode"] == SHIPMENT_NUMBER


def test_full_shipment_is_normalised():
    parcel = normalize_account_parcel(full_shipment("READY_FOR_PICKUP"))
    assert parcel["carrier"] == "Posti"
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Store"
    assert parcel["receiver"] == "Example Recipient"
    assert parcel["planned_from"] == "2026-05-03T12:00:00.000Z"
    assert parcel["planned_to"] is None
    assert parcel["weight"] == 1.25
    assert parcel["dimensions"] is None
    assert parcel["history"] is None
    assert parcel["raw"]["shipmentId"] == SHIPMENT_NUMBER


def test_delivered_uses_the_newest_event_and_drops_the_planned_time():
    parcel = normalize_account_parcel(full_shipment("DELIVERED"))
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-05-02T10:00:00.000Z"
    assert parcel["planned_from"] is None


def test_delivered_at_does_not_trust_list_order():
    raw = shipment("DELIVERED")
    raw["events"] = list(reversed(raw["events"]))
    assert normalize_account_parcel(raw)["delivered_at"] == "2026-05-02T10:00:00.000Z"


def test_history_is_sorted_on_timestamp_and_skips_unparseable_events():
    raw = shipment()
    raw["events"] = [
        event("2026-05-02T10:00:00.000Z", "second"),
        event("2026-05-01T09:00:00.000Z", "first"),
        event("not a time", "bad"),
        {"eventDescription": "no time"},
        "junk",
    ]
    history = normalize_account_parcel(raw, include_history=True)["history"]
    assert [item["raw_status"] for item in history] == ["first", "second"]
    assert all(item["status"] is None for item in history)


def test_absent_and_malformed_fields_do_not_raise():
    raw = {
        "displayId": "X",
        "delivery": {"time": {"timestamp": "garbage"}, "destination": None},
        "pickupPoint": {"address": None},
        "measurements": {"weight": {"value": "heavy"}},
        "status": None,
    }
    parcel = normalize_account_parcel(raw)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["planned_from"] is None
    assert parcel["receiver"] is None
    assert parcel["pickup_point"] is None
    assert parcel["weight"] is None


def test_incoming_parcel_names_the_other_party_as_sender():
    raw = full_shipment("IN_TRANSPORT") | {"displayName": "Example Shop Oy"}
    parcel = normalize_account_parcel(raw)
    assert parcel["sender"] == "Example Shop Oy"
    assert parcel["receiver"] == "Example Recipient"


def test_sent_parcel_names_the_other_party_as_receiver():
    raw = full_shipment("IN_TRANSPORT") | {
        "userRole": "SENDER",
        "displayName": "Example Friend",
    }
    parcel = normalize_account_parcel(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] == "Example Friend"


def test_a_sent_parcel_coming_back_names_the_other_party_as_sender():
    raw = shipment("RETURN_IN_TRANSPORT", role="SENDER") | {
        "displayName": "Example Friend"
    }
    parcel = normalize_account_parcel(raw)
    assert parcel["sender"] == "Example Friend"
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
