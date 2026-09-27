"""Posti parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import PostiAccountClient
from .account.coordinator import PostiAccountCoordinator
from .const import (
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_ROLE_TOKENS,
    CONF_SOURCE,
    DOMAIN,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .services import async_setup_services, async_unload_services
from .tracking.client import PostiTrackingClient
from .tracking.coordinator import PostiTrackingCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class PostiData:
    """Runtime data attached to the Posti config entry."""

    client: PostiTrackingClient | PostiAccountClient
    coordinator: PostiTrackingCoordinator | PostiAccountCoordinator


type PostiConfigEntry = ConfigEntry[PostiData]


async def async_setup_entry(hass: HomeAssistant, entry: PostiConfigEntry) -> bool:
    """Set up Posti from a config entry."""
    is_account = entry.data.get(CONF_SOURCE) == SOURCE_ACCOUNT
    if is_account:

        async def _async_store_tokens(tokens: dict[str, object]) -> None:
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ID_TOKEN: tokens[CONF_ID_TOKEN],
                    CONF_REFRESH_TOKEN: tokens[CONF_REFRESH_TOKEN],
                    CONF_ROLE_TOKENS: tokens[CONF_ROLE_TOKENS],
                },
            )

        client = PostiAccountClient(
            async_get_clientsession(hass),
            id_token=entry.data.get(CONF_ID_TOKEN),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            role_tokens=entry.data.get(CONF_ROLE_TOKENS),
            token_callback=_async_store_tokens,
        )
        coordinator: PostiTrackingCoordinator | PostiAccountCoordinator = (
            PostiAccountCoordinator(hass, client, entry)
        )
    else:
        client = PostiTrackingClient(async_get_clientsession(hass))
        coordinator = PostiTrackingCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady/ConfigEntryAuthFailed from a forwarded platform is
    # too late for HA to catch cleanly (it logs a warning and half-sets-up
    # the entry); doing the first refresh here lets a transient failure —
    # or an account needing reauth — fail the whole entry setup cleanly.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PostiData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, history) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation. This is also the resume
    # path after polling fully suspended (tracking source only) — adding a
    # parcel back triggers this refresh, which recomputes the tier and
    # re-arms scheduling.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if not is_account:
        async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: PostiConfigEntry
) -> None:
    """Apply changed options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: PostiConfigEntry) -> bool:
    """Unload a Posti config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services are shared across tracking hubs; only remove them once
    # the last *tracking* entry is gone, so an account entry unloading never
    # touches them and a second tracking hub keeps them registered.
    others_loaded = any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        and other.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if (
        entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING
        and not others_loaded
    ):
        async_unload_services(hass)
    return True
