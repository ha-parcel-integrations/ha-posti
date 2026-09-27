"""Coordinator for the Posti tracking-code source.

Fetching and event firing only — the parcel mapping lives in ``parcels.py``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from ..events import (
    fire_incoming_change_events,
    snapshot_delivery_times,
    snapshot_states,
)
from .client import PostiTrackingApiError, PostiTrackingClient
from .parcels import apply_delivered_filter, normalize_parcel, sort_parcels_by_ts

_LOGGER = logging.getLogger(__name__)

# Base for the 429 backoff when the carrier's response carries no
# ``Retry-After`` of its own: ``BACKOFF_BASE_SECONDS * 2**consecutive_429``,
# capped at ``BACKOFF_CAP_SECONDS``.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    midnight_tomorrow = (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    return midnight_tomorrow


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the code-based model. ``None`` means "stop polling entirely"."""
    if not active_parcels:
        return None

    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``."""
    if tier_minutes is None:
        return None

    if _in_quiet_window(now):
        return _next_anchor(now) - now

    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


class PostiTrackingCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls each tracked code and publishes the canonical parcel lists."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PostiTrackingClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        self._raw_cache: dict[str, dict] = {}
        self._delivered_codes: set[str] = set()
        self._consecutive_429 = 0
        self._current_tier_minutes: int | None = None
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Tracking codes currently skipped from the fetch (diagnostics only)."""
        return self._delivered_codes

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

    def _tracked(self) -> list[str]:
        """Return the configured tracking codes."""
        return [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        ]

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    async def _async_update_data(self) -> list[dict]:
        """Fetch every tracked code and split into active vs delivered."""
        codes = self._tracked()

        tracked_codes = set(codes)
        self._raw_cache = {
            code: raw for code, raw in self._raw_cache.items() if code in tracked_codes
        }
        self._delivered_codes &= tracked_codes

        codes_to_fetch = [code for code in codes if code not in self._delivered_codes]

        # One anonymous token pair per refresh, shared by every fetch below —
        # never cached across refreshes.
        self._client.reset_token()

        results = await asyncio.gather(
            *(self._client.async_get_parcel(code) for code in codes_to_fetch),
            return_exceptions=True,
        )

        raws_by_code: dict[str, dict] = {}
        errors = 0
        retry_afters: list[float] = []
        saw_429 = False
        for code, result in zip(codes_to_fetch, results):
            if isinstance(result, BaseException):
                if not isinstance(result, (PostiTrackingApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                if isinstance(result, PostiTrackingApiError) and result.status_code == 429:
                    saw_429 = True
                    if result.retry_after is not None:
                        retry_afters.append(result.retry_after)
                _LOGGER.warning("Posti tracking fetch failed for %s: %s", code, result)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws_by_code[code] = cached
                continue

            if result is None:
                # Not found (yet), or already known — keep prior data if we
                # have it, otherwise show an empty placeholder so the parcel
                # still appears with the code as its barcode.
                raws_by_code[code] = self._raw_cache.get(code) or {}
                continue

            self._raw_cache[code] = result
            raws_by_code[code] = result

        for code in self._delivered_codes:
            cached = self._raw_cache.get(code)
            if cached is not None:
                raws_by_code[code] = cached

        if saw_429:
            self._consecutive_429 += 1
            retry_after = (
                max(retry_afters)
                if retry_afters
                else min(
                    BACKOFF_BASE_SECONDS * 2**self._consecutive_429,
                    BACKOFF_CAP_SECONDS,
                )
            )
            raise UpdateFailed("Posti rate-limited (429)", retry_after=retry_after)
        self._consecutive_429 = 0

        if codes_to_fetch and errors == len(codes_to_fetch) and not raws_by_code:
            raise UpdateFailed("Posti unreachable for all tracked parcels")

        include_history = self._include_history
        entries = [
            (code, normalize_parcel(raw, tracking_code=code, include_history=include_history))
            for code, raw in raws_by_code.items()
        ]
        active = [parcel for _, parcel in entries if not parcel["delivered"]]
        delivered = [parcel for _, parcel in entries if parcel["delivered"]]
        self._delivered_codes = {code for code, parcel in entries if parcel["delivered"]}

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

        if not codes_to_fetch or errors < len(codes_to_fetch):
            self.last_success_time = datetime.now(timezone.utc)

        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(normalized_active, now)
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
        return normalized_active
