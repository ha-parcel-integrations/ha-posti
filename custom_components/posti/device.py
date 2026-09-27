"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry — and because the account source only has to change this
file to name devices per account.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_SOURCE, CONF_USERNAME, DOMAIN, SOURCE_ACCOUNT

CONFIGURATION_URL = "https://www.posti.fi/"

ATTRIBUTION = "Data provided by Posti"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this Posti hub.

    An account entry's device is named after its username so multiple
    OmaPosti accounts stay distinguishable; the tracking hub is a single
    instance and needs no discriminator.
    """
    is_account = entry.data.get(CONF_SOURCE) == SOURCE_ACCOUNT
    username = entry.data.get(CONF_USERNAME)
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Posti ({username})" if is_account and username else "Posti",
        manufacturer="Posti",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
