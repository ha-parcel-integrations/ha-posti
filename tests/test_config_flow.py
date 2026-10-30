"""Tests for the Posti config and options flow."""

import base64
import json
from unittest.mock import AsyncMock, patch

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.account.device import Device
from custom_components.posti.account.errors import (
    PostiAccountApiError,
    PostiAccountInvalidReturnAddress,
    PostiAccountLoginRejected,
)
from custom_components.posti.account.login import StrongLogin
from custom_components.posti.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.posti.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DEVICE,
    CONF_ID_TOKEN,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_ROLE_TOKENS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

ROLE_TOKENS = [{"type": "consumer", "token": "consumer-token"}]
RETURN = "https://todentaminen.posti.fi/uas/return/abc?code=one&state=two"
METHOD = "telia.ftn.oidc.op.1"
BANK_ADDRESS = "https://tunnistus.telia.fi/idp/auth?request=abc"


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


NEW_DEVICE = Device("dev-new", "key", "salt", "sub-1")
NEW_TOKENS = {
    CONF_ID_TOKEN: _jwt({"sub": "sub-1", "email": "jane@example.com"}),
    CONF_ROLE_TOKENS: ROLE_TOKENS,
}
OLD_TOKENS = {CONF_ID_TOKEN: "id-token", CONF_ROLE_TOKENS: ROLE_TOKENS}


def _login_patches(**overrides):
    """Patch the four network calls of the account login."""
    targets = {
        "start_login": AsyncMock(
            return_value=StrongLogin("verifier", {METHOD: "/uas/authn/abc/select"})
        ),
        "bank_address": AsyncMock(return_value=BANK_ADDRESS),
        "finish_login": AsyncMock(return_value={"id_token": "x"}),
    }
    targets.update(overrides)
    patches = [
        patch(f"custom_components.posti.account.login.{name}", new=mock)
        for name, mock in targets.items()
    ]
    patches.append(
        patch(
            "custom_components.posti.account.device.provision",
            new=overrides.get(
                "provision", AsyncMock(return_value=(NEW_DEVICE, NEW_TOKENS))
            ),
        )
    )
    return patches, targets


class _Patched:
    def __init__(self, **overrides):
        self.patches, self.mocks = _login_patches(**overrides)

    def __enter__(self):
        for patcher in self.patches:
            patcher.start()
        return self.mocks

    def __exit__(self, *exc):
        for patcher in self.patches:
            patcher.stop()


async def _to_authenticate(hass, flow_id):
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "account"}
    )
    assert result["step_id"] == "account"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"method": METHOD}
    )


def test_normalize_tracking_code_strips_and_uppercases():
    assert normalize_tracking_code("example 123-456") == "EXAMPLE123456"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_valid_tracking_code_accepts_any_non_empty_code():
    assert valid_tracking_code("EXAMPLE123456")
    assert not valid_tracking_code("")


async def test_user_flow_shows_menu(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["menu_options"] == [SOURCE_TRACKING, SOURCE_ACCOUNT]


async def test_tracking_flow_creates_hub_without_input(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Posti"
    assert result["data"][CONF_SOURCE] == SOURCE_TRACKING
    assert result["options"][CONF_PARCELS] == []


async def test_second_tracking_hub_rejected(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id="tracking", data={CONF_SOURCE: SOURCE_TRACKING}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_account_flow_creates_an_entry_with_a_trusted_device(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _Patched() as mocks:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "account"}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "account"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": METHOD}
        )
        assert result["step_id"] == "authenticate"
        assert result["description_placeholders"]["address"] == BANK_ADDRESS
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "Posti (jane@example.com)"
    data = result["data"]
    assert data[CONF_SOURCE] == SOURCE_ACCOUNT
    assert data[CONF_ID_TOKEN] == NEW_TOKENS[CONF_ID_TOKEN]
    assert data[CONF_DEVICE] == NEW_DEVICE.as_data()
    assert "refresh_token" not in data
    assert CONF_PARCELS not in result["options"]
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "account:sub-1"
    mocks["finish_login"].assert_awaited_once()
    assert mocks["finish_login"].await_args.args[2] == RETURN


async def test_account_flow_cannot_start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _Patched(start_login=AsyncMock(side_effect=PostiAccountApiError("boom"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "account"}
        )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"


async def test_account_flow_wrong_address_can_be_corrected(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    finish = AsyncMock(
        side_effect=[PostiAccountInvalidReturnAddress("no"), {"id_token": "x"}]
    )
    with _Patched(finish_login=finish) as mocks:
        result = await _to_authenticate(hass, result["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": "https://evil.example/"}
        )
        assert result["step_id"] == "authenticate"
        assert result["errors"]["base"] == "invalid_return_address"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )
    assert result["type"] == "create_entry"
    # The bank address is shown again, not fetched again.
    mocks["bank_address"].assert_awaited_once()


async def test_account_flow_rejected_authentication(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _Patched(finish_login=AsyncMock(side_effect=PostiAccountLoginRejected("x"))):
        result = await _to_authenticate(hass, result["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"


async def test_account_flow_connection_error_while_finishing(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _Patched(finish_login=AsyncMock(side_effect=PostiAccountApiError("boom"))):
        result = await _to_authenticate(hass, result["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_account_flow_bank_page_failure_returns_to_the_start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _Patched(bank_address=AsyncMock(side_effect=PostiAccountApiError("x"))):
        result = await _to_authenticate(hass, result["flow_id"])
    assert result["step_id"] == "account"
    assert result["errors"]["base"] == "cannot_connect"


async def test_account_flow_second_login_of_the_same_account_aborts_and_drops_device(
    hass,
):
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:sub-1",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with (
        _Patched(),
        patch(
            "custom_components.posti.account.device.deactivate", new=AsyncMock()
        ) as deactivate,
    ):
        result = await _to_authenticate(hass, result["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    deactivate.assert_awaited_once()


async def test_reauth_of_a_password_era_entry_adds_a_device(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:jane.doe",
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "jane.doe", **OLD_TOKENS},
    )
    entry.add_to_hass(hass)

    with _Patched():
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "account"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": METHOD}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_DEVICE] == NEW_DEVICE.as_data()
    assert entry.data[CONF_ID_TOKEN] == NEW_TOKENS[CONF_ID_TOKEN]
    assert entry.unique_id == "account:sub-1"


async def test_reauth_with_another_account_is_refused_and_drops_device(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:other",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_DEVICE: {**NEW_DEVICE.as_data(), "subject": "other"},
            **OLD_TOKENS,
        },
    )
    entry.add_to_hass(hass)

    with (
        _Patched(),
        patch(
            "custom_components.posti.account.device.deactivate", new=AsyncMock()
        ) as deactivate,
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": METHOD}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "unique_id_mismatch"
    deactivate.assert_awaited_once()
    assert entry.data[CONF_DEVICE]["subject"] == "other"


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="tracking",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: parcels},
    )


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:jane.doe",
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "jane.doe", **OLD_TOKENS},
        options={},
    )


def _settings_input(*, history=False, filter_type="days", amount=7) -> dict:
    return {
        CONF_DELIVERED_FILTER_TYPE: filter_type,
        CONF_DELIVERED_FILTER_AMOUNT: amount,
        CONF_INCLUDE_HISTORY: history,
    }


async def _open_options_step(hass, entry, step_id: str, *, expected_menu):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == expected_menu
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_tracking_options_menu_offers_parcels_and_settings(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["parcels", "settings"]


async def test_account_options_menu_offers_settings_only(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["settings"]


async def test_options_add_parcel(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(
        hass, entry, "parcels", expected_menu=["parcels", "settings"]
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["example123456"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EXAMPLE123456"}]


async def test_options_de_duplicates_tracking_codes(hass):
    entry = _hub([{CONF_TRACKING_CODE: "EXAMPLE111111"}])
    entry.add_to_hass(hass)
    result = await _open_options_step(
        hass, entry, "parcels", expected_menu=["parcels", "settings"]
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["EXAMPLE111111", "example111111"]}
    )
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EXAMPLE111111"}]


async def test_options_changes_history_and_delivered_for_tracking(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(
        hass, entry, "settings", expected_menu=["parcels", "settings"]
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _settings_input(history=True, filter_type="parcels", amount=5),
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5


async def test_options_changes_settings_for_account(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await _open_options_step(
        hass, entry, "settings", expected_menu=["settings"]
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _settings_input(history=True)
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True


async def test_account_login_uses_its_own_detached_session(hass):
    seen = []

    async def _start(session, locale):
        seen.append(session)
        return StrongLogin("verifier", {METHOD: "/uas/authn/abc/select"})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _Patched(start_login=_start):
        result = await _to_authenticate(hass, result["flow_id"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"return_address": RETURN}
        )
    assert result["type"] == "create_entry"
    shared = async_get_clientsession(hass)
    assert seen[0] is not shared
    assert seen[0].cookie_jar is not shared.cookie_jar
    assert seen[0].connector is None


async def test_an_abandoned_account_login_releases_its_session(hass):
    seen = []

    async def _start(session, locale):
        seen.append(session)
        return StrongLogin("verifier", {METHOD: "/uas/authn/abc/select"})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with _Patched(start_login=_start):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "account"}
        )
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()
    assert seen[0].connector is None
