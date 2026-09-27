"""Tests for the tracking-code coordinator: fetching, caching and events."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.posti.tracking.client import PostiTrackingApiError
from custom_components.posti.tracking.coordinator import PostiTrackingCoordinator

from .payloads import TRACKING_CODE, delivered_hit, unknown_status_hit

OTHER_CODE = "JJFI00000000000002"


def _parcels(*codes: str) -> list[dict]:
    return [{CONF_TRACKING_CODE: c} for c in codes]


def _entry_with(codes: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PARCELS: codes,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id="tracking",
    )


def _client(**overrides) -> AsyncMock:
    client = AsyncMock()
    client.reset_token = MagicMock()
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with(_parcels(TRACKING_CODE, OTHER_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.side_effect = lambda code: (
        unknown_status_hit(code) if code == TRACKING_CODE else delivered_hit(code)
    )
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == TRACKING_CODE
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None
    client.reset_token.assert_called_once()


async def test_update_not_found_shows_empty_placeholder(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.return_value = None
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == TRACKING_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_payload_on_error(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.return_value = delivered_hit()
    coordinator = PostiTrackingCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    client.async_get_parcel.side_effect = PostiTrackingApiError("HTTP 500")
    await coordinator._async_update_data()
    assert len(coordinator.delivered) == 1


async def test_update_raises_when_every_parcel_fails(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.side_effect = PostiTrackingApiError("HTTP 500")
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_reraises_unexpected_exceptions(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.side_effect = ValueError("boom")
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_update_skips_parcels_missing_a_code(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ""}, {CONF_TRACKING_CODE: TRACKING_CODE}])
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.return_value = delivered_hit()
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.return_value = delivered_hit()
    coordinator = PostiTrackingCoordinator(hass, client, entry)
    coordinator._raw_cache["GONE"] = {}

    await coordinator._async_update_data()

    assert "GONE" not in coordinator._raw_cache
    assert TRACKING_CODE in coordinator._raw_cache


async def test_429_raises_update_failed_with_backoff(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.side_effect = PostiTrackingApiError(
        "HTTP 429", status_code=429, retry_after=None
    )
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after


async def test_delivered_code_skipped_from_fetch(hass):
    entry = _entry_with(_parcels(TRACKING_CODE, OTHER_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.side_effect = lambda code: (
        unknown_status_hit(code) if code == TRACKING_CODE else delivered_hit(code)
    )
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert coordinator.delivered_codes == {OTHER_CODE}

    client.async_get_parcel.reset_mock()
    data = await coordinator._async_update_data()

    client.async_get_parcel.assert_called_once_with(TRACKING_CODE)
    assert any(p["barcode"] == OTHER_CODE for p in coordinator.delivered)
    assert data[0]["barcode"] == TRACKING_CODE


async def test_delivered_code_forgotten_when_untracked(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.return_value = delivered_hit()
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert coordinator.delivered_codes == {TRACKING_CODE}

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PARCELS: []})
    await coordinator._async_update_data()
    assert coordinator.delivered_codes == set()


async def test_first_refresh_fires_nothing(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_parcel.return_value = unknown_status_hit()
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    fired = []
    for suffix in ("parcel_registered", "parcel_status_changed", "parcel_delivered"):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    client = _client()
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e))

    client.async_get_parcel.return_value = unknown_status_hit()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = delivered_hit()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == TRACKING_CODE


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with(_parcels(TRACKING_CODE))
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    client = _client()
    coordinator = PostiTrackingCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    client.async_get_parcel.return_value = unknown_status_hit()
    await coordinator._async_update_data()
    other = unknown_status_hit()
    other["status"]["main"] = "SOMETHING_ELSE"
    client.async_get_parcel.return_value = other
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []  # both map to unknown -> no status change fired
    assert coordinator._device_id() == device.id
