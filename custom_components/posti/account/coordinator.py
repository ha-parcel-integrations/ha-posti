"""Coordinator for the OmaPosti account-inbox source.

One batched inbox read per cycle — there is no per-parcel fetch to skip, so
polling never suspends and ``delivered_codes`` stays empty by design.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import (
    CONF_INCLUDE_HISTORY,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    ParcelStatus,
)
from ..events import (
    fire_incoming_change_events,
    snapshot_delivery_times,
    snapshot_states,
)
from ..tracking.parcels import apply_delivered_filter, sort_parcels_by_ts
from .auth import PostiAccountApiError, PostiAccountReauthRequired
from .client import PostiAccountClient
from .parcels import normalize_account_parcel

_LOGGER = logging.getLogger(__name__)


class PostiAccountCoordinator(DataUpdateCoordinator[list[dict]]):
    """Refresh the full OmaPosti inbox at a fixed cadence."""

    def __init__(
        self, hass: HomeAssistant, client: PostiAccountClient, entry: ConfigEntry
    ) -> None:
        """Initialise a continuously-polled account inbox coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} account",
            update_interval=timedelta(minutes=MID_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        # None on the first refresh deliberately suppresses historical events.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None
        # Fixed cadence — an account inbox is one batched call with nothing
        # to tier on, unlike the per-parcel tracking coordinator.
        self._current_tier_minutes: int | None = MID_INTERVAL_MINUTES

    @property
    def current_tier_minutes(self) -> int | None:
        """Fixed account-polling cadence (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Account polls are batched; nothing is ever skipped from the fetch."""
        return set()

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    async def _async_update_data(self) -> list[dict]:
        """Fetch the account inbox and split into active vs delivered."""
        try:
            shipments = await self._client.async_get_shipments()
        except PostiAccountReauthRequired as err:
            raise ConfigEntryAuthFailed(
                "OmaPosti sign-in expired; please reauthenticate"
            ) from err
        except PostiAccountApiError as err:
            raise UpdateFailed("Unable to update the OmaPosti account inbox") from err

        include_history = self._include_history
        parcels = [
            normalize_account_parcel(raw, include_history=include_history)
            for raw in shipments
        ]
        active = [parcel for parcel in parcels if not parcel["delivered"]]
        delivered = [parcel for parcel in parcels if parcel["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        incoming = normalized_active + self.delivered
        device_id = self._device_id()
        fire_incoming_change_events(
            self.hass, incoming, self._known_state, self._known_delivery_times, device_id
        )
        self._known_state = snapshot_states(incoming)
        self._known_delivery_times = snapshot_delivery_times(incoming)

        self.last_success_time = datetime.now(timezone.utc)
        return normalized_active
