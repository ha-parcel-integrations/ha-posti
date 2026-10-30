"""Tests for the OmaPosti account-inbox coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.account.coordinator import PostiAccountCoordinator
from custom_components.posti.account.errors import (
    PostiAccountApiError,
    PostiAccountReauthRequired,
)
from custom_components.posti.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    ParcelStatus,
)

from .payloads import TRACKING_NUMBER, shipment


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id="account:jane",
    )


async def test_update_splits_active_and_delivered(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipments.return_value = [
        shipment("IN_TRANSPORT"),
        shipment("DELIVERED", tracking_number="OTHER", shipment_number="S2"),
    ]
    coordinator = PostiAccountCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == TRACKING_NUMBER
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None
    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.delivered_codes == set()


async def test_reauth_required_raises_config_entry_auth_failed(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipments.side_effect = PostiAccountReauthRequired("expired")
    coordinator = PostiAccountCoordinator(hass, client, entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_generic_api_error_raises_update_failed(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipments.side_effect = PostiAccountApiError("boom")
    coordinator = PostiAccountCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_first_refresh_fires_nothing(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipments.return_value = [shipment("IN_TRANSPORT")]
    coordinator = PostiAccountCoordinator(hass, client, entry)

    fired = []
    for suffix in ("parcel_registered", "parcel_status_changed", "parcel_delivered"):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_status_change_fires_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PostiAccountCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    client.async_get_shipments.return_value = [shipment("WAITING")]
    await coordinator._async_update_data()
    client.async_get_shipments.return_value = [shipment("IN_TRANSPORT")]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1


def _sent(phase="IN_TRANSPORT", number="SENT-1"):
    return shipment(
        phase, tracking_number=number, shipment_number=number, role="SENDER"
    )


async def test_sent_parcels_are_split_from_incoming(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_shipments.return_value = [
        shipment("IN_TRANSPORT"),
        _sent("IN_TRANSPORT", "SENT-1"),
        _sent("DELIVERED", "SENT-2"),
        _sent("RETURN_IN_TRANSPORT", "SENT-3"),
    ]
    coordinator = PostiAccountCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert {p["barcode"] for p in data} == {TRACKING_NUMBER, "SENT-3"}
    assert [p["barcode"] for p in coordinator.outgoing] == ["SENT-1"]
    assert [p["barcode"] for p in coordinator.outgoing_delivered] == ["SENT-2"]
    assert coordinator.delivered == []


async def test_sent_parcels_fire_only_the_outgoing_pair(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PostiAccountCoordinator(hass, client, entry)

    seen: dict[str, list] = {}
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    ):
        seen[suffix] = []
        hass.bus.async_listen(
            f"{DOMAIN}_{suffix}", lambda e, key=suffix: seen[key].append(e)
        )

    # The first refresh is silent, and so is a sent parcel appearing later.
    client.async_get_shipments.return_value = [_sent("WAITING", "SENT-1")]
    await coordinator._async_update_data()
    client.async_get_shipments.return_value = [
        _sent("WAITING", "SENT-1"),
        _sent("WAITING", "SENT-2"),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert all(not events for events in seen.values())

    client.async_get_shipments.return_value = [
        _sent("IN_TRANSPORT", "SENT-1"),
        _sent("WAITING", "SENT-2"),
    ]
    await coordinator._async_update_data()
    client.async_get_shipments.return_value = [
        _sent("DELIVERED", "SENT-1"),
        _sent("WAITING", "SENT-2"),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    changed = seen["outgoing_parcel_status_changed"]
    assert len(changed) == 1
    assert changed[0].data["old_status"] == ParcelStatus.REGISTERED
    assert changed[0].data["new_status"] == ParcelStatus.IN_TRANSIT
    assert changed[0].data["device_id"] is None or changed[0].data["device_id"]
    assert len(seen["outgoing_parcel_delivered"]) == 1
    assert seen["parcel_registered"] == []
    assert seen["parcel_status_changed"] == []
    assert seen["parcel_delivered"] == []


async def test_a_sent_parcel_coming_back_is_incoming_news(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = PostiAccountCoordinator(hass, client, entry)
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    client.async_get_shipments.return_value = []
    await coordinator._async_update_data()
    client.async_get_shipments.return_value = [_sent("RETURN_IN_TRANSPORT")]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_a_parcel_the_delivered_list_leaves_out_still_fires_delivered(hass):
    """The retention option limits the delivered sensor, never the events."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_DELIVERED_FILTER_AMOUNT: 1}
    )
    client = AsyncMock()
    coordinator = PostiAccountCoordinator(hass, client, entry)
    delivered = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))

    def inbox(phase):
        return [
            shipment(phase),
            shipment(phase, tracking_number="OTHER", shipment_number="S2"),
        ]

    client.async_get_shipments.return_value = inbox("IN_TRANSPORT")
    await coordinator._async_update_data()
    client.async_get_shipments.return_value = inbox("DELIVERED")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert sorted(e.data["barcode"] for e in delivered) == sorted(
        [TRACKING_NUMBER, "OTHER"]
    )
    assert len(coordinator.delivered) == 1
