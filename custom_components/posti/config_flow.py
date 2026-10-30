"""Config flow for the Posti parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
import voluptuous as vol

from .account import (
    device as account_device,
    login as account_login,
    tokens as account_tokens,
)
from .account.errors import (
    PostiAccountApiError,
    PostiAccountInvalidReturnAddress,
    PostiAccountLoginRejected,
)
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DEVICE,
    CONF_ID_TOKEN,
    CONF_INCLUDE_HISTORY,
    CONF_METHOD,
    CONF_PARCELS,
    CONF_RETURN_ADDRESS,
    CONF_ROLE_TOKENS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    METHOD_ORDER,
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

    _session: aiohttp.ClientSession | None = None
    _login: account_login.StrongLogin | None = None
    _method: str = ""
    _address: str | None = None

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

    @callback
    def async_remove(self) -> None:
        """Release the login session if the flow ends before it finishes."""
        self._close_session()

    def _close_session(self) -> None:
        session = getattr(self, "_session", None)
        if session is not None:
            session.detach()
            self._session = None

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a strong-authentication login and offer the methods Posti has."""
        if not user_input:
            try:
                await self._async_start_login()
            except PostiAccountApiError as err:
                _LOGGER.warning("OmaPosti login could not be started: %s", err)
                return self.async_show_form(
                    step_id=SOURCE_ACCOUNT, errors={"base": "cannot_connect"}
                )
            return self.async_show_form(
                step_id=SOURCE_ACCOUNT,
                data_schema=vol.Schema(
                    {vol.Required(CONF_METHOD): self._method_selector()}
                ),
            )
        self._method = user_input[CONF_METHOD]
        self._address = None
        return await self.async_step_authenticate()

    async def _async_start_login(self) -> None:
        """Begin a login on a session of its own, so no other login's cookies leak in."""
        self._close_session()
        self._session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
        )
        self._login = await account_login.start_login(
            self._session, self.hass.config.language
        )

    def _method_selector(self) -> selector.SelectSelector:
        assert self._login is not None
        ids = list(self._login.methods)

        def rank(method: str) -> int:
            provider = method.split(".")[-2] if method.count(".") >= 2 else method
            return (
                METHOD_ORDER.index(provider)
                if provider in METHOD_ORDER
                else len(METHOD_ORDER)
            )

        ids.sort(key=rank)
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=m, label=m.split(".")[-2])
                    for m in ids
                ],
                mode=selector.SelectSelectorMode.LIST,
            )
        )

    async def async_step_authenticate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show where to authenticate, then finish with the address it ends on."""
        assert self._session is not None
        assert self._login is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._async_finish(user_input[CONF_RETURN_ADDRESS])
            except PostiAccountInvalidReturnAddress:
                errors["base"] = "invalid_return_address"
            except PostiAccountLoginRejected:
                errors["base"] = "invalid_auth"
            except PostiAccountApiError as err:
                _LOGGER.warning("OmaPosti login failed: %s", err)
                errors["base"] = "cannot_connect"
        if self._address is None:
            try:
                self._address = await account_login.bank_address(
                    self._session, self._login, self._method
                )
            except PostiAccountApiError as err:
                _LOGGER.warning("OmaPosti login could not continue: %s", err)
                return self.async_show_form(
                    step_id=SOURCE_ACCOUNT, errors={"base": "cannot_connect"}
                )
        return self.async_show_form(
            step_id="authenticate",
            data_schema=vol.Schema({vol.Required(CONF_RETURN_ADDRESS): str}),
            description_placeholders={"address": self._address},
            errors=errors,
        )

    async def _async_finish(self, return_address: str) -> ConfigFlowResult:
        """Complete the login, register the trusted device and save the entry."""
        assert self._session is not None
        assert self._login is not None
        tokens = await account_login.finish_login(
            self._session, self._login, return_address
        )
        device, tokens = await account_device.provision(self._session, tokens)
        self._close_session()
        email = (account_tokens.claims(tokens["id_token"]) or {}).get("email")
        data = {
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ID_TOKEN: tokens[CONF_ID_TOKEN],
            CONF_ROLE_TOKENS: tokens.get(CONF_ROLE_TOKENS) or [],
            CONF_DEVICE: device.as_data(),
        }
        if isinstance(email, str) and email:
            data[CONF_USERNAME] = email
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            known = (entry.data.get(CONF_DEVICE) or {}).get("subject")
            if known and known != device.subject:
                # The device was registered on the wrong account; take it off again.
                await self._async_drop_device(device, tokens)
                return self.async_abort(reason="unique_id_mismatch")
            return self.async_update_reload_and_abort(
                entry,
                unique_id=f"account:{device.subject}",
                data_updates=data,
            )
        await self.async_set_unique_id(f"account:{device.subject}")
        if self._async_current_entries_have_unique_id():
            await self._async_drop_device(device, tokens)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Posti ({email})" if isinstance(email, str) and email else "Posti",
            data=data,
            options=_default_options(),
        )

    def _async_current_entries_have_unique_id(self) -> bool:
        return any(
            entry.unique_id == self.unique_id
            for entry in self._async_current_entries(include_ignore=False)
        )

    async def _async_drop_device(
        self, device: account_device.Device, tokens: dict[str, Any]
    ) -> None:
        """Take a just-registered device off the account again."""
        try:
            await account_device.deactivate(
                async_get_clientsession(self.hass), device, tokens["id_token"]
            )
        except PostiAccountApiError as err:
            _LOGGER.warning("Could not remove the unused OmaPosti device: %s", err)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Sign in again; never silently change accounts."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain the new login, then run the same steps as a first setup."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={
                    "account": self._get_reauth_entry().data.get(CONF_USERNAME, "")
                },
            )
        return await self.async_step_account()


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
