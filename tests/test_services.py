"""Tests for the Posti services (track_parcel / untrack_parcel)."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.const import (
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
)
from custom_components.posti.services import async_setup_services

from .tracking.payloads import delivered_hit

_SAMPLE = delivered_hit()

_PATCH_TARGET = "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel"


async def _setup(hass, parcels: list[dict] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: parcels or []},
    )
    entry.add_to_hass(hass)
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_SAMPLE)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_track_parcel_adds_to_options(hass):
    entry = await _setup(hass)
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_SAMPLE)):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "JJFI00000000000099"}, blocking=True
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "JJFI00000000000099"}]


async def test_track_parcel_normalizes_code(hass):
    entry = await _setup(hass)
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_SAMPLE)):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "jjfi-0000000000 0099"}, blocking=True
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "JJFI00000000000099"}]


async def test_track_parcel_rejects_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: ""}, blocking=True
        )


async def test_track_parcel_duplicate_is_noop(hass):
    entry = await _setup(hass)
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_SAMPLE)):
        for _ in range(2):
            await hass.services.async_call(
                DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "JJFI00000000000099"}, blocking=True
            )
            await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_untrack_parcel_removes_from_options(hass):
    entry = await _setup(hass, parcels=[{CONF_TRACKING_CODE: "JJFI00000000000099"}])
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_SAMPLE)):
        await hass.services.async_call(
            DOMAIN, "untrack_parcel", {CONF_TRACKING_CODE: "JJFI00000000000099"}, blocking=True
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == []


async def test_untrack_unknown_code_is_noop(hass):
    entry = await _setup(hass, parcels=[{CONF_TRACKING_CODE: "JJFI00000000000099"}])
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_SAMPLE)):
        await hass.services.async_call(
            DOMAIN, "untrack_parcel", {CONF_TRACKING_CODE: "NOPE"}, blocking=True
        )
        await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_services_not_registered_without_a_tracking_hub(hass):
    """An account-only install must not gain manual tracking services."""
    from custom_components.posti.const import (
        CONF_ID_TOKEN,
        CONF_REFRESH_TOKEN,
        CONF_ROLE_TOKENS,
        CONF_USERNAME,
        SOURCE_ACCOUNT,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:jane.doe",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: "jane.doe",
            CONF_ID_TOKEN: "id",
            CONF_REFRESH_TOKEN: "refresh",
            CONF_ROLE_TOKENS: [{"type": "consumer", "token": "x"}],
        },
        options={},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.posti.account.client.PostiAccountClient.async_get_shipments",
        new=AsyncMock(return_value=[]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_track_parcel_rejects_a_code_of_only_separators(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: " - - "}, blocking=True
        )


async def test_setup_services_is_idempotent_and_needs_a_tracking_hub(hass):
    async_setup_services(hass)
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "untrack_parcel", {CONF_TRACKING_CODE: "JJFI00000000000099"}, blocking=True
        )
