"""Tests for the shared HA-bus event contract."""
from __future__ import annotations

from custom_components.posti.const import DOMAIN, ParcelStatus
from custom_components.posti.events import (
    fire_incoming_change_events,
    snapshot_delivery_times,
    snapshot_states,
)


def _parcel(barcode, status, planned_from=None, planned_to=None):
    return {
        "barcode": barcode,
        "status": status,
        "planned_from": planned_from,
        "planned_to": planned_to,
    }


def test_silent_on_first_refresh(hass):
    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    fire_incoming_change_events(
        hass, [_parcel("A", ParcelStatus.IN_TRANSIT)], None, None, "device1"
    )
    assert fired == []


async def test_fires_registered_for_new_non_delivered_parcel(hass):
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))
    fire_incoming_change_events(
        hass, [_parcel("A", ParcelStatus.IN_TRANSIT)], {}, {}, "device1"
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["device_id"] == "device1"


async def test_no_registered_event_for_new_but_already_delivered(hass):
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))
    fire_incoming_change_events(
        hass, [_parcel("A", ParcelStatus.DELIVERED)], {}, {}, "device1"
    )
    await hass.async_block_till_done()
    assert events == []


async def test_fires_status_changed(hass):
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))
    fire_incoming_change_events(
        hass,
        [_parcel("A", ParcelStatus.OUT_FOR_DELIVERY)],
        {"A": ParcelStatus.IN_TRANSIT},
        {},
        "device1",
    )
    await hass.async_block_till_done()
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_delivered_fires_delivered_not_status_changed(hass):
    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e))
    fire_incoming_change_events(
        hass,
        [_parcel("A", ParcelStatus.DELIVERED)],
        {"A": ParcelStatus.OUT_FOR_DELIVERY},
        {},
        "device1",
    )
    await hass.async_block_till_done()
    assert changed == []
    assert len(delivered) == 1


async def test_fires_delivery_time_changed(hass):
    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )
    fire_incoming_change_events(
        hass,
        [_parcel("A", ParcelStatus.IN_TRANSIT, planned_from="2026-01-02T00:00:00Z")],
        {"A": ParcelStatus.IN_TRANSIT},
        {"A": (None, None)},
        "device1",
    )
    await hass.async_block_till_done()
    assert events[0].data["new_planned_from"] == "2026-01-02T00:00:00Z"


async def test_losing_the_eta_is_silent(hass):
    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )
    fire_incoming_change_events(
        hass,
        [_parcel("A", ParcelStatus.IN_TRANSIT)],
        {"A": ParcelStatus.IN_TRANSIT},
        {"A": ("2026-01-01T00:00:00Z", None)},
        "device1",
    )
    await hass.async_block_till_done()
    assert events == []


def test_parcels_without_a_barcode_are_ignored(hass):
    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    fire_incoming_change_events(hass, [{"status": ParcelStatus.IN_TRANSIT}], {}, {}, None)
    assert fired == []


def test_snapshot_states_skips_missing_barcodes():
    parcels = [
        _parcel("A", ParcelStatus.IN_TRANSIT),
        {"status": ParcelStatus.IN_TRANSIT},
    ]
    assert snapshot_states(parcels) == {"A": ParcelStatus.IN_TRANSIT}


def test_snapshot_delivery_times_skips_missing_barcodes():
    parcels = [
        _parcel("A", ParcelStatus.IN_TRANSIT, planned_from="2026-01-01T00:00:00Z"),
        {"status": ParcelStatus.IN_TRANSIT},
    ]
    assert snapshot_delivery_times(parcels) == {"A": ("2026-01-01T00:00:00Z", None)}
