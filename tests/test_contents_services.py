"""Tests for update_contents, the track_parcel field and the store lifecycle."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components import posti
from custom_components.posti.account.coordinator import PostiAccountCoordinator
from custom_components.posti.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
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
from custom_components.posti.contents import CONTENTS_STORE_MAX_CODES
from custom_components.posti.diagnostics import async_get_config_entry_diagnostics
from custom_components.posti.tracking.coordinator import PostiTrackingCoordinator

from .account.payloads import shipment
from .tracking.payloads import delivered_hit, unknown_status_hit

IN_CODE = "JJFI00000000000011"
OUT_CODE = "JJFI00000000000022"
DONE_CODE = "JJFI00000000000033"
OTHER_CODE = "JJFI00000000000044"
UNLISTED = "JJFI00000000000055"
SHOES = "Running shoes"

KEEP_ALL = {CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 100}
PARCEL = "custom_components.posti.tracking.client.PostiTrackingClient.async_get_parcel"
SHIPMENTS = (
    "custom_components.posti.account.client.PostiAccountClient.async_get_shipments"
)
TRACKING_REFRESH = f"{PostiTrackingCoordinator.__module__}.PostiTrackingCoordinator"
ACCOUNT_REFRESH = f"{PostiAccountCoordinator.__module__}.PostiAccountCoordinator"


async def _hit(code):
    return unknown_status_hit(code)


def _hub(parcels: list[str], unique_id: str = "tracking") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in parcels],
            **KEEP_ALL,
        },
    )


def _account(username: str = "jane.doe") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"account:{username}",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: username,
            CONF_ID_TOKEN: "id-token",
            CONF_ROLE_TOKENS: [{"type": "consumer", "token": "consumer-token"}],
            CONF_DEVICE: {
                "device_id": "dev-1",
                "private_key": "pem",
                "salt": "c2FsdA==",
                "subject": "sub-1",
            },
        },
        options=KEEP_ALL,
    )


def _rows():
    return [
        shipment("IN_TRANSPORT", tracking_number=IN_CODE, shipment_number="S1"),
        shipment(
            "IN_TRANSPORT",
            tracking_number=OUT_CODE,
            shipment_number="S2",
            role="SENDER",
        ),
        shipment("DELIVERED", tracking_number=DONE_CODE, shipment_number="S3"),
    ]


async def _setup(hass, *entries: MockConfigEntry) -> None:
    for entry in entries:
        entry.add_to_hass(hass)
        with (
            patch(PARCEL, side_effect=_hit),
            patch(SHIPMENTS, AsyncMock(return_value=_rows())),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()


async def _call(hass, service: str, data: dict) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str):
    coordinator = entry.runtime_data.coordinator
    parcels = [
        *coordinator.data,
        *coordinator.delivered,
        *getattr(coordinator, "outgoing", []),
        *getattr(coordinator, "outgoing_delivered", []),
    ]
    return {p["barcode"]: p for p in parcels}[code]["contents"]


def _handler(hass, service: str):
    return hass.services.async_services()[DOMAIN][service].job.target


# --- hub -------------------------------------------------------------------


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    assert _contents(entry, IN_CODE) is None


async def test_update_contents_sets_replaces_and_clears_on_the_hub(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: IN_CODE, "contents": " Running  shoes "},
    )
    assert _contents(entry, IN_CODE) == SHOES

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": "Socks"}
    )
    assert _contents(entry, IN_CODE) == "Socks"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": "  "}
    )
    assert _contents(entry, IN_CODE) is None

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    )
    await _call(hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE})
    assert _contents(entry, IN_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: f" {IN_CODE.lower()} ", "contents": SHOES},
    )
    assert _contents(entry, IN_CODE) == SHOES


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass, _hub([IN_CODE]))
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: "  "})


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: UNLISTED, "contents": SHOES}
    )
    assert entry.runtime_data.coordinator.contents_store.get(UNLISTED) == SHOES
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: IN_CODE}]


async def test_update_contents_never_polls_and_notifies_once_per_change(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    with (
        patch(f"{TRACKING_REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(PARCEL, new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(DOMAIN, "update_contents", data, blocking=True)
        assert listeners.call_count == 1
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: IN_CODE}, blocking=True
        )
        assert listeners.call_count == 2
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: IN_CODE}, blocking=True
        )
        assert listeners.call_count == 2
    refresh.assert_not_called()
    fetch.assert_not_called()
    assert _contents(entry, IN_CODE) is None


async def test_an_unchanged_value_writes_nothing(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    store = entry.runtime_data.coordinator.contents_store
    data = {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    await _call(hass, "update_contents", data)
    with patch.object(store._store, "async_delay_save") as save:
        await _call(hass, "update_contents", data)
    save.assert_not_called()


async def test_track_parcel_with_contents_stores_it_and_still_refreshes(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    with patch(PARCEL, side_effect=_hit) as fetch:
        await _call(
            hass, "track_parcel", {CONF_TRACKING_CODE: OTHER_CODE, "contents": SHOES}
        )
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()
    assert OTHER_CODE in [c.args[0] for c in fetch.call_args_list]
    assert _contents(entry, OTHER_CODE) == SHOES


async def test_track_parcel_on_a_tracked_code_reapplies_without_polling(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    with (
        patch(f"{TRACKING_REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(PARCEL, new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        await _call(hass, "track_parcel", data)
        await _call(hass, "track_parcel", data)
    assert listeners.call_count == 1
    refresh.assert_not_called()
    fetch.assert_not_called()
    assert _contents(entry, IN_CODE) == SHOES


async def test_track_parcel_without_contents_leaves_the_store_alone(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    )
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: IN_CODE})
    assert _contents(entry, IN_CODE) == SHOES


async def test_untrack_drops_the_contents(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    )
    with patch(PARCEL, side_effect=_hit):
        await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: IN_CODE})
    assert entry.runtime_data.coordinator.contents_store.get(IN_CODE) is None


async def test_the_store_keeps_200_codes_and_drops_the_oldest(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    store = entry.runtime_data.coordinator.contents_store
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: f"CODE{number}", "contents": SHOES},
        )
    assert store.get("CODE0") is None
    assert store.get("CODE1") == SHOES
    assert store.get(f"CODE{CONTENTS_STORE_MAX_CODES}") == SHOES


async def test_the_poll_overlays_the_stored_value(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    )
    with patch(PARCEL, side_effect=_hit):
        await entry.runtime_data.coordinator.async_refresh()
    assert _contents(entry, IN_CODE) == SHOES


async def test_the_poll_overlays_a_delivered_parcel(hass):
    entry = _hub([DONE_CODE])
    entry.add_to_hass(hass)
    with patch(PARCEL, new=AsyncMock(return_value=delivered_hit(DONE_CODE))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert _contents(entry, DONE_CODE) is None

    coordinator.contents_store.async_set(DONE_CODE, SHOES)
    coordinator.async_reapply_contents()
    assert coordinator.delivered[0]["contents"] == SHOES
    coordinator.contents_store.async_set(DONE_CODE, None)
    coordinator.async_reapply_contents()
    assert coordinator.delivered[0]["contents"] is None


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    hass_storage[f"{DOMAIN}.contents.{{id}}"] = {}
    entry = _hub([IN_CODE])
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.contents.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.contents.{entry.entry_id}",
        "data": {"by_code": {IN_CODE: SHOES}},
    }
    await _setup(hass, entry)
    assert _contents(entry, IN_CODE) == SHOES


async def test_an_entry_without_a_store_file_works(hass, hass_storage):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    assert entry.runtime_data.coordinator.contents_store.get(IN_CODE) is None
    assert _contents(entry, IN_CODE) is None


async def test_removing_the_entry_removes_its_store(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    with patch("custom_components.posti.ContentsStore.async_remove") as remove:
        await posti.async_remove_entry(hass, entry)
    remove.assert_awaited_once()


async def test_a_hub_that_is_not_loaded_is_refused(hass):
    entry = _hub([IN_CODE])
    await _setup(hass, entry)
    track, untrack = _handler(hass, "track_parcel"), _handler(hass, "untrack_parcel")
    await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED

    contents_call = type(
        "Call", (), {"data": {CONF_TRACKING_CODE: OTHER_CODE, "contents": SHOES}}
    )()
    with pytest.raises(ServiceValidationError):
        await track(contents_call)
    # Untracking never needs the store, so an unloaded hub is simply edited.
    await untrack(type("Call", (), {"data": {CONF_TRACKING_CODE: IN_CODE}})())
    assert entry.options[CONF_PARCELS] == []


# --- account and routing ---------------------------------------------------


async def test_update_contents_on_an_account_reaches_every_list_without_poll(hass):
    account = _account()
    await _setup(hass, account)
    coordinator = account.runtime_data.coordinator
    assert isinstance(coordinator, PostiAccountCoordinator)
    with (
        patch(f"{ACCOUNT_REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(SHIPMENTS, new=AsyncMock()) as inbox,
        patch(PARCEL, new=AsyncMock()) as fetch,
        patch.object(
            coordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as listeners,
    ):
        # a single loaded entry is targeted directly, even for an unlisted code
        await _call(
            hass, "update_contents", {CONF_TRACKING_CODE: UNLISTED, "contents": "A"}
        )
        assert listeners.call_count == 1
        for code in (IN_CODE, OUT_CODE, DONE_CODE):
            await _call(
                hass, "update_contents", {CONF_TRACKING_CODE: code, "contents": SHOES}
            )
        assert listeners.call_count == 4
        await _call(
            hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
        )
        assert listeners.call_count == 4
    refresh.assert_not_called()
    inbox.assert_not_called()
    fetch.assert_not_called()
    for code in (IN_CODE, OUT_CODE, DONE_CODE):
        assert _contents(account, code) == SHOES
    assert coordinator.contents_store.get(UNLISTED) == "A"


async def test_the_account_poll_overlays_the_stored_value_on_both_directions(hass):
    account = _account()
    await _setup(hass, account)
    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: OUT_CODE, "contents": SHOES}
    )
    with patch(SHIPMENTS, AsyncMock(return_value=_rows())):
        await account.runtime_data.coordinator.async_refresh()
    assert _contents(account, OUT_CODE) == SHOES
    assert _contents(account, IN_CODE) is None


async def test_a_parcel_without_a_barcode_gets_no_contents(hass):
    account = _account()
    account.add_to_hass(hass)
    bare = shipment("IN_TRANSPORT", tracking_number=None, shipment_number="S9")
    bare.pop("displayId")
    bare.pop("shipmentId")
    with patch(SHIPMENTS, AsyncMock(return_value=[bare])):
        assert await hass.config_entries.async_setup(account.entry_id)
        await hass.async_block_till_done()
    coordinator = account.runtime_data.coordinator
    assert coordinator.data[0]["contents"] is None
    assert coordinator.known_codes == frozenset()


async def test_known_codes_list_what_each_entry_lists(hass):
    hub, account = _hub([OTHER_CODE]), _account()
    await _setup(hass, hub, account)
    assert hub.runtime_data.coordinator.known_codes == {OTHER_CODE}
    assert account.runtime_data.coordinator.known_codes == {
        IN_CODE,
        OUT_CODE,
        DONE_CODE,
    }


async def test_two_entries_listing_the_same_code_both_get_the_value(hass):
    hub, account = _hub([IN_CODE]), _account()
    await _setup(hass, hub, account)
    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    )
    assert _contents(hub, IN_CODE) == SHOES
    assert _contents(account, IN_CODE) == SHOES


async def test_a_code_no_entry_lists_needs_an_entry(hass):
    hub, account = _hub([IN_CODE]), _account()
    await _setup(hass, hub, account)
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "update_contents", {CONF_TRACKING_CODE: UNLISTED, "contents": SHOES}
        )
    assert err.value.translation_key == "update_contents_entry_required"


async def test_config_entry_id_targets_exactly_that_entry(hass):
    hub, account = _hub([IN_CODE]), _account()
    await _setup(hass, hub, account)
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNLISTED,
            "contents": SHOES,
            "config_entry_id": account.entry_id,
        },
    )
    assert account.runtime_data.coordinator.contents_store.get(UNLISTED) == SHOES
    assert hub.runtime_data.coordinator.contents_store.get(UNLISTED) is None

    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: IN_CODE,
            "contents": "Socks",
            "config_entry_id": hub.entry_id,
        },
    )
    assert _contents(hub, IN_CODE) == "Socks"
    assert _contents(account, IN_CODE) is None


async def test_an_unknown_config_entry_id_is_rejected(hass):
    await _setup(hass, _hub([IN_CODE]))
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES, "config_entry_id": "no"},
        )


async def test_an_unloaded_entry_is_never_targeted(hass):
    hub, account, spare = _hub([IN_CODE]), _account(), _account("two.doe")
    await _setup(hass, hub, account, spare)
    assert await hass.config_entries.async_unload(spare.entry_id)

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: IN_CODE, "contents": SHOES}
    )
    assert _contents(hub, IN_CODE) == SHOES
    assert _contents(account, IN_CODE) == SHOES

    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {
                CONF_TRACKING_CODE: IN_CODE,
                "contents": SHOES,
                "config_entry_id": spare.entry_id,
            },
        )


async def test_nothing_loaded_means_no_target(hass):
    account, hub = _account(), _hub([IN_CODE])
    await _setup(hass, account, hub)
    handler = _handler(hass, "update_contents")
    await hass.config_entries.async_unload(account.entry_id)
    await hass.config_entries.async_unload(hub.entry_id)
    call = type("Call", (), {"data": {CONF_TRACKING_CODE: IN_CODE}})()
    with pytest.raises(ServiceValidationError):
        await handler(call)


async def test_services_follow_the_entries_that_need_them(hass):
    hub, account = _hub([IN_CODE]), _account()
    await _setup(hass, hub, account)
    assert hass.services.has_service(DOMAIN, "track_parcel")

    await hass.config_entries.async_unload(hub.entry_id)
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "update_contents")

    await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_an_account_alone_registers_only_update_contents(hass):
    await _setup(hass, _account())
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")


# --- diagnostics -----------------------------------------------------------


async def test_diagnostics_redact_the_contents_on_both_entry_types(hass):
    hub, account = _hub([IN_CODE]), _account()
    await _setup(hass, hub, account)
    for code in (IN_CODE, OUT_CODE):
        await _call(
            hass, "update_contents", {CONF_TRACKING_CODE: code, "contents": SHOES}
        )
    hub_diag = await async_get_config_entry_diagnostics(hass, hub)
    assert hub_diag["incoming"][0]["contents"] == "**REDACTED**"
    account_diag = await async_get_config_entry_diagnostics(hass, account)
    assert account_diag["incoming"][0]["contents"] == "**REDACTED**"
    assert account_diag["outgoing"][0]["contents"] == "**REDACTED**"
