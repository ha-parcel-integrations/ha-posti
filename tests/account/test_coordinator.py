"""Tests for the OmaPosti account-inbox coordinator."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.account.auth import (
    PostiAccountApiError,
    PostiAccountReauthRequired,
)
from custom_components.posti.account.coordinator import PostiAccountCoordinator
from custom_components.posti.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    MID_INTERVAL_MINUTES,
)

from .payloads import TRACKING_NUMBER, shipment


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 100},
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
