"""Tests for Posti setup and unload, across both sources."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.account.errors import (
    PostiAccountApiError,
    PostiAccountReauthRequired,
)
from custom_components.posti.const import (
    CONF_DEVICE,
    CONF_ID_TOKEN,
    CONF_PARCELS,
    CONF_ROLE_TOKENS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.posti.tracking.client import PostiTrackingApiError

from .account.payloads import shipment
from .tracking.payloads import TRACKING_CODE, delivered_hit, unknown_status_hit

OTHER_CODE = "JJFI00000000000002"

ACCOUNT_TOKENS = {
    CONF_ID_TOKEN: "id-token",
    CONF_ROLE_TOKENS: [{"type": "consumer", "token": "consumer-token"}],
    CONF_DEVICE: {
        "device_id": "dev-1",
        "private_key": "pem",
        "salt": "c2FsdA==",
        "subject": "sub-1",
    },
}


def _tracking_entry(codes: list[str]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: c} for c in codes]},
    )


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:jane.doe",
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "jane.doe", **ACCOUNT_TOKENS},
        options={},
    )


async def test_tracking_setup_and_unload(hass):
    entry = _tracking_entry([TRACKING_CODE])
    entry.add_to_hass(hass)

    with patch(
        "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel",
        new=AsyncMock(return_value=delivered_hit()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    incoming = hass.states.get("sensor.posti_incoming_parcels")
    assert incoming is not None
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_tracking_setup_retries_when_first_refresh_fails(hass):
    entry = _tracking_entry([TRACKING_CODE])
    entry.add_to_hass(hass)

    with patch(
        "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel",
        new=AsyncMock(side_effect=PostiTrackingApiError("Posti unreachable")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _tracking_entry([TRACKING_CODE])
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=unknown_status_hit())
    with patch(
        "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel",
        new=mock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{TRACKING_CODE}"
        )

        mock.return_value = unknown_status_hit(OTHER_CODE)
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_PARCELS: [{CONF_TRACKING_CODE: OTHER_CODE}]},
        )
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{OTHER_CODE}"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{TRACKING_CODE}"
            )
            is None
        )


async def test_options_update_applies_live_without_reload(hass):
    entry = _tracking_entry([TRACKING_CODE])
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=delivered_hit())
    with patch(
        "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel",
        new=mock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        mock.side_effect = lambda code: delivered_hit(code)
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_PARCELS: [
                    {CONF_TRACKING_CODE: TRACKING_CODE},
                    {CONF_TRACKING_CODE: OTHER_CODE},
                ],
            },
        )
        await hass.async_block_till_done()

    incoming = hass.states.get("sensor.posti_incoming_parcels")
    assert incoming.state == "0"  # both delivered_hit()s map to DELIVERED


async def test_account_setup_and_unload_registers_no_tracking_services(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.posti.account.client.PostiAccountClient.async_get_shipments",
        new=AsyncMock(return_value=[shipment("IN_TRANSPORT")]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_account_entry_has_outgoing_sensors_and_tracking_does_not(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    # Keep the old delivered sample inside the retention filter.
    hass.config_entries.async_update_entry(
        entry,
        options={"delivered_filter_type": "parcels", "delivered_filter_amount": 100},
    )
    sent = shipment(
        "IN_TRANSPORT", tracking_number="SENT-1", shipment_number="S1", role="SENDER"
    )
    sent_done = shipment(
        "DELIVERED", tracking_number="SENT-2", shipment_number="S2", role="SENDER"
    )
    with patch(
        "custom_components.posti.account.client.PostiAccountClient.async_get_shipments",
        new=AsyncMock(return_value=[shipment("IN_TRANSPORT"), sent, sent_done]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)

    def state_of(unique_id):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        return hass.states.get(entity_id).state

    assert state_of(f"{entry.entry_id}_incoming_parcels") == "1"
    assert state_of(f"{entry.entry_id}_outgoing_parcels") == "1"
    assert state_of(f"{entry.entry_id}_outgoing_delivered_parcels") == "1"
    # A sent parcel gets no per-parcel sensor of its own.
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_SENT-1")
        is None
    )


async def test_tracking_entry_has_no_outgoing_sensors(hass):
    entry = _tracking_entry([TRACKING_CODE])
    entry.add_to_hass(hass)
    with patch(
        "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel",
        new=AsyncMock(return_value=delivered_hit()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    for suffix in ("outgoing_parcels", "outgoing_delivered_parcels"):
        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{suffix}")
            is None
        )


async def test_account_reauth_required_triggers_reauth_flow(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.posti.account.client.PostiAccountClient.async_get_shipments",
        new=AsyncMock(side_effect=PostiAccountReauthRequired("expired")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == "reauth" for flow in flows)


async def test_account_token_renewal_is_persisted(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.posti.account.client.tokens_mod.expires_within",
            return_value=True,
        ),
        patch(
            "custom_components.posti.account.client.device_mod.refresh",
            new=AsyncMock(return_value={"id_token": "rotated"}),
        ),
        patch(
            "custom_components.posti.account.client.PostiAccountClient._async_query",
            new=AsyncMock(return_value=[]),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.data[CONF_ID_TOKEN] == "rotated"
    assert entry.data[CONF_ROLE_TOKENS] == ACCOUNT_TOKENS[CONF_ROLE_TOKENS]
    assert entry.data[CONF_DEVICE] == ACCOUNT_TOKENS[CONF_DEVICE]
    assert "refresh_token" not in entry.data


async def test_password_era_entry_asks_for_a_new_login(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:jane.doe",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: "jane.doe",
            CONF_ID_TOKEN: "id-token",
            "refresh_token": "refresh-token",
            CONF_ROLE_TOKENS: [],
        },
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == "reauth" for flow in flows)


async def test_removing_an_account_entry_deactivates_its_device(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.posti.account.device.refresh",
            new=AsyncMock(return_value={"id_token": "fresh"}),
        ),
        patch(
            "custom_components.posti.account.device.deactivate", new=AsyncMock()
        ) as deactivate,
    ):
        await hass.config_entries.async_remove(entry.entry_id)

    deactivate.assert_awaited_once()
    assert deactivate.await_args.args[1].device_id == "dev-1"
    assert deactivate.await_args.args[2] == "fresh"


async def test_removing_an_entry_whose_device_is_gone_is_quiet(hass, caplog):
    entry = _account_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.posti.account.device.refresh",
            new=AsyncMock(side_effect=PostiAccountReauthRequired("gone")),
        ),
        patch(
            "custom_components.posti.account.device.deactivate", new=AsyncMock()
        ) as deactivate,
    ):
        await hass.config_entries.async_remove(entry.entry_id)

    deactivate.assert_not_awaited()
    assert "Could not remove" not in caplog.text


async def test_a_failed_deactivation_warns_with_the_entry_id_not_the_email(
    hass, caplog
):
    entry = _account_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.posti.account.device.refresh",
        new=AsyncMock(side_effect=PostiAccountApiError("down")),
    ):
        await hass.config_entries.async_remove(entry.entry_id)

    assert entry.entry_id in caplog.text
    assert "jane.doe" not in caplog.text


async def test_removing_a_tracking_entry_touches_no_account(hass):
    entry = _tracking_entry([])
    entry.add_to_hass(hass)
    with patch(
        "custom_components.posti.account.device.refresh", new=AsyncMock()
    ) as refresh:
        await hass.config_entries.async_remove(entry.entry_id)
    refresh.assert_not_awaited()


async def test_second_tracking_hub_does_not_disturb_account_services(hass):
    """Unloading an account entry never touches the tracking services.

    Both entries load from one ``async_setup`` call: HA sets up every
    registered entry of a domain together the first time that domain's
    component is set up.
    """
    tracking_entry = _tracking_entry([TRACKING_CODE])
    tracking_entry.add_to_hass(hass)
    account_entry = _account_entry()
    account_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel",
            new=AsyncMock(return_value=delivered_hit()),
        ),
        patch(
            "custom_components.posti.account.client.PostiAccountClient.async_get_shipments",
            new=AsyncMock(return_value=[]),
        ),
    ):
        assert await hass.config_entries.async_setup(tracking_entry.entry_id)
        await hass.async_block_till_done()

        assert hass.services.has_service(DOMAIN, "track_parcel")
        assert await hass.config_entries.async_unload(account_entry.entry_id)
        await hass.async_block_till_done()
        assert hass.services.has_service(DOMAIN, "track_parcel")
