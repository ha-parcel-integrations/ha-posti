"""Tests for the OmaPosti account source's canonical parcel mapping."""
from __future__ import annotations

from custom_components.posti.account import parcels
from custom_components.posti.const import ParcelStatus

from .payloads import SHIPMENT_NUMBER, TRACKING_NUMBER, shipment


def test_maps_every_documented_phase():
    expected = {
        "WAITING": ParcelStatus.REGISTERED,
        "RECEIVED": ParcelStatus.IN_TRANSIT,
        "IN_TRANSPORT": ParcelStatus.IN_TRANSIT,
        "IN_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
        "READY_FOR_PICKUP": ParcelStatus.AT_PICKUP_POINT,
        "DELIVERED": ParcelStatus.DELIVERED,
        "RETURNED_TO_SENDER": ParcelStatus.RETURNING,
    }
    for phase, status in expected.items():
        parcel = parcels.normalize_account_parcel(shipment(phase))
        assert parcel["status"] is status, phase


def test_unmapped_phase_warns_once_and_reports_unknown(caplog):
    parcels._unmapped_statuses_logged.clear()
    parcel = parcels.normalize_account_parcel(shipment("CANCELLED"))
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert "CANCELLED" in caplog.text

    caplog.clear()
    parcels.normalize_account_parcel(shipment("CANCELLED"))
    assert "CANCELLED" not in caplog.text


def test_barcode_prefers_tracking_number():
    parcel = parcels.normalize_account_parcel(shipment())
    assert parcel["barcode"] == TRACKING_NUMBER
    assert parcel["url"] == f"https://www.posti.fi/seuranta/{TRACKING_NUMBER}"


def test_barcode_falls_back_to_shipment_number():
    parcel = parcels.normalize_account_parcel(shipment(tracking_number=None))
    assert parcel["barcode"] == SHIPMENT_NUMBER


def test_ready_for_pickup_sets_pickup_flag():
    parcel = parcels.normalize_account_parcel(shipment("READY_FOR_PICKUP"))
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_no_field_populates_eta_weight_dimensions_or_delivered_at():
    parcel = parcels.normalize_account_parcel(shipment("DELIVERED"))
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_history_is_none_when_option_is_off():
    parcel = parcels.normalize_account_parcel(shipment())
    assert parcel["history"] is None


def test_history_is_sorted_oldest_first_with_stable_tiebreak():
    raw = shipment()
    history = parcels.build_history(raw["events"])
    assert [entry["timestamp"] for entry in history] == [
        "2026-05-01T09:00:00.000Z",
        "2026-05-02T10:00:00.000Z",
    ]
    assert all(entry["status"] is None for entry in history)
    assert history[0]["raw_status"] == "Shipment announced"


def test_history_sorts_out_of_order_events_by_timestamp():
    from .payloads import event

    raw = shipment()
    raw["events"] = [
        event("2026-05-02T10:00:00.000Z", "later"),
        event("2026-05-01T09:00:00.000Z", "earlier"),
    ]
    history = parcels.build_history(raw["events"])
    assert [entry["raw_status"] for entry in history] == ["earlier", "later"]


def test_history_skips_events_with_no_timestamp_or_non_dict():
    raw = shipment()
    raw["events"].append({"timestamp": None})
    raw["events"].append("not-a-dict")
    history = parcels.build_history(raw["events"])
    assert len(history) == 2


def test_history_ignores_events_without_a_description_object():
    raw = shipment()
    raw["events"][0]["eventDescription"] = None
    history = parcels.build_history(raw["events"])
    assert history[0]["raw_status"] is None
