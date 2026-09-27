"""Config flow for the Posti parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .account import auth
from .account.auth import PostiAccountApiError, PostiAccountInvalidCredentials
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_ID_TOKEN,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_ROLE_TOKENS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

_LOGGER = logging.getLogger(__name__)


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code upper-cased with separators stripped."""
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def valid_tracking_code(value: str) -> bool:
    """Accept every non-empty code — real formats vary too much to gate on."""
    return bool(value)


def normalize_username(value: str) -> str:
    """Return the OmaPosti username trimmed and lower-cased.

    The lower-cased username is used as the entry's stable identity — never a
    password, access token or unverified JWT claim.
    """
    return (value or "").strip().lower()


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _clean_tracking_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_tracking_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


async def _async_login(
    hass: HomeAssistant, username: str, password: str
) -> dict[str, Any]:
    """Sign in on a session of its own, so no other sign-in's cookies leak in."""
    session = async_create_clientsession(
        hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
    )
    try:
        return await auth.async_login(session, username, password)
    finally:
        session.detach()


def _default_options() -> dict[str, Any]:
    """Return the settings every source starts with."""
    return {
        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
    }


class PostiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Posti integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PostiOptionsFlowHandler:
        """Return the options flow handler."""
        return PostiOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the two genuinely separate Posti sources.

        The choice is stored as ``CONF_SOURCE`` and dispatched once at setup
        — never inferred from a token field.
        """
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_TRACKING, SOURCE_ACCOUNT]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single tracking-code hub.

        Tracking is keyless (no account, no postcode), so there is nothing to
        ask: the hub is created straight away and parcels are added
        afterwards via the options flow, the ``posti.track_parcel`` service
        or a dashboard button. One tracking hub only, unless a future
        confirmed public requirement introduces a real hub discriminator.
        """
        await self.async_set_unique_id(SOURCE_TRACKING)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Posti",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={CONF_PARCELS: [], **_default_options()},
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in once via the app-emulated PKCE flow; persist only the tokens."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = normalize_username(user_input[CONF_USERNAME])
            password = str(user_input[CONF_PASSWORD])
            if not username or not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    tokens = await _async_login(self.hass, username, password)
                except PostiAccountInvalidCredentials:
                    errors["base"] = "invalid_auth"
                except PostiAccountApiError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(f"account:{username}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Posti ({username})",
                        data={
                            CONF_SOURCE: SOURCE_ACCOUNT,
                            CONF_USERNAME: username,
                            CONF_ID_TOKEN: tokens[CONF_ID_TOKEN],
                            CONF_REFRESH_TOKEN: tokens[CONF_REFRESH_TOKEN],
                            CONF_ROLE_TOKENS: tokens[CONF_ROLE_TOKENS],
                        },
                        options=_default_options(),
                    )
        return self.async_show_form(
            step_id=SOURCE_ACCOUNT,
            data_schema=vol.Schema(
                {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Reauthenticate the fixed account; never silently change accounts."""
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Repeat PKCE login after a rejected refresh; only the password is asked."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if user_input is not None:
            try:
                tokens = await _async_login(
                    self.hass, entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except PostiAccountInvalidCredentials:
                errors["base"] = "invalid_auth"
            except PostiAccountApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(entry.unique_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_ID_TOKEN: tokens[CONF_ID_TOKEN],
                        CONF_REFRESH_TOKEN: tokens[CONF_REFRESH_TOKEN],
                        CONF_ROLE_TOKENS: tokens[CONF_ROLE_TOKENS],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


class PostiOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels separately from integration settings.

    An account entry has no manual parcel list — its menu is ``settings``
    only.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management only where there is a parcel list to manage."""
        menu_options = ["settings"]
        if self.config_entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING:
            menu_options.insert(0, "parcels")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = _clean_tracking_codes(user_input.get("tracking_codes"))
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
                    },
                )
        current_codes = [
            parcel[CONF_TRACKING_CODE] for parcel in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["days", "parcels"],
                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DELIVERED_FILTER_AMOUNT,
                default=current.get(
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
