"""Diagnostics support for the Posti parcel tracker integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import PostiConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel — over-redacting is
# cheap.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # tracking-route payload fields
    "displayId",
    "eventDescription",
    "reasonDescription",
    "city",
    "shipmentId",
    "displayName",
    "references",
    "reference",
    "trackingNumber",
    "street",
    "streetAddress",
    "specificLocation",
    "postcode",
    "publicName",
    "pupCode",
    "pickupDetails",
    "sendingDetails",
    "pinCode",
    "lockerId",
    "errandCode",
    # account-route payload fields
    "trackingNumbers",
    "shipmentNumber",
    "savedDateTime",
    "eventLocation",
    "country",
    "description",
    "value",
    "lockerAddress",
    "lockerCode",
    "location",
    "estimatedDeliveryTime",
    "pickup_point",
    # account credentials and identity — never leave HA
    "username",
    "password",
    "id_token",
    "refresh_token",
    "role_tokens",
    "device",
    "device_id",
    "private_key",
    "salt",
    "subject",
    "access_token",
    "return_address",
    "Authorization",
    "X-Posti-Token",
    "X-Omaposti-Roles",
    "code",
    "code_verifier",
    "code_challenge",
    "state",
    "email",
    "name",
    "phone",
    "address",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PostiConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Posti config entry."""
    coordinator = entry.runtime_data.coordinator
    outgoing = getattr(coordinator, "outgoing", [])
    outgoing_delivered = getattr(coordinator, "outgoing_delivered", [])

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "outgoing_active": len(outgoing),
            "outgoing_delivered": len(outgoing_delivered),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
        "outgoing": async_redact_data(outgoing, TO_REDACT),
        "delivered_outgoing": async_redact_data(outgoing_delivered, TO_REDACT),
    }
