"""Tests for the Posti config and options flow."""
from unittest.mock import AsyncMock, patch

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.account.auth import (
    PostiAccountApiError,
    PostiAccountInvalidCredentials,
)
from custom_components.posti.config_flow import (
    normalize_tracking_code,
    normalize_username,
    valid_tracking_code,
)
from custom_components.posti.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_ID_TOKEN,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_ROLE_TOKENS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

TOKENS = {
    CONF_ID_TOKEN: "id-token",
    CONF_REFRESH_TOKEN: "refresh-token",
    CONF_ROLE_TOKENS: [{"type": "consumer", "token": "consumer-token"}],
}


def test_normalize_tracking_code_strips_and_uppercases():
    assert normalize_tracking_code("example 123-456") == "EXAMPLE123456"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_valid_tracking_code_accepts_any_non_empty_code():
    assert valid_tracking_code("EXAMPLE123456")
    assert not valid_tracking_code("")


def test_normalize_username_trims_and_lowercases():
    assert normalize_username(" Jane.Doe ") == "jane.doe"
    assert normalize_username(None) == ""


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


async def test_account_flow_logs_in_and_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "account"

    with patch(
        "custom_components.posti.config_flow.auth.async_login",
        new=AsyncMock(return_value=TOKENS),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "Jane.Doe", "password": "secret"}
        )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SOURCE] == SOURCE_ACCOUNT
    assert result["data"][CONF_USERNAME] == "jane.doe"
    assert result["data"][CONF_ID_TOKEN] == "id-token"
    assert CONF_PARCELS not in result["options"]


async def test_account_flow_invalid_credentials(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch(
        "custom_components.posti.config_flow.auth.async_login",
        new=AsyncMock(side_effect=PostiAccountInvalidCredentials("nope")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "jane", "password": "bad"}
        )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"


async def test_account_flow_connection_error(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch(
        "custom_components.posti.config_flow.auth.async_login",
        new=AsyncMock(side_effect=PostiAccountApiError("boom")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "jane", "password": "bad"}
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_account_flow_blank_fields():
    """Covered without hitting the network: blank input never calls login."""
    from custom_components.posti.config_flow import PostiConfigFlow

    flow = PostiConfigFlow()
    result = await flow.async_step_account({CONF_USERNAME: " ", "password": ""})
    assert result["errors"]["base"] == "invalid_auth"


async def test_reauth_flow_updates_tokens(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:jane.doe",
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "jane.doe", **TOKENS},
    )
    entry.add_to_hass(hass)

    new_tokens = {**TOKENS, CONF_ID_TOKEN: "new-id-token"}
    with patch(
        "custom_components.posti.config_flow.auth.async_login",
        new=AsyncMock(return_value=new_tokens),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ID_TOKEN] == "new-id-token"


async def test_reauth_flow_invalid_credentials(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:jane.doe",
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "jane.doe", **TOKENS},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.posti.config_flow.auth.async_login",
        new=AsyncMock(side_effect=PostiAccountInvalidCredentials("nope")),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "still-wrong"}
        )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"


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
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "jane.doe", **TOKENS},
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
        result["flow_id"], _settings_input(history=True, filter_type="parcels", amount=5)
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5


async def test_options_changes_settings_for_account(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings", expected_menu=["settings"])
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _settings_input(history=True)
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True


async def test_account_login_uses_its_own_detached_session(hass):
    seen = []

    async def _login(session, username, password):
        seen.append(session)
        return TOKENS

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    with patch("custom_components.posti.config_flow.auth.async_login", new=_login):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "jane", "password": "secret"}
        )
    assert result["type"] == "create_entry"
    shared = async_get_clientsession(hass)
    assert seen[0] is not shared
    assert seen[0].cookie_jar is not shared.cookie_jar
    assert seen[0].connector is None
