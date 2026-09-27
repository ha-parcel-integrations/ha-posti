"""Canonical parcel shape and list helpers for the tracking-code source.

Every function here is pure — no I/O, no Home Assistant objects beyond the
config entry's options. The parcel-list helpers (``parse_iso``,
``sort_parcels_by_ts``, ``apply_delivered_filter``, ``tracking_url``) are
genuinely source-agnostic and are imported by ``account/parcels.py`` too,
rather than duplicated — everything *status*- or *field*-shaped stays local
to each source (see ``status.py``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)
from ..status import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

# Only add a literal once a real parcel has shown it. ``RETURN_DELIVERED`` is
# the return arriving back at the sender — for the recipient that is a
# return, not a delivery.
STATUS_MAP: dict[str, ParcelStatus] = {
    "DELIVERED": ParcelStatus.DELIVERED,
    "RETURN_DELIVERED": ParcelStatus.RETURNING,
}

# Keyed on the ``locale: "en"`` text the client pins, normalised by
# ``_event_key``. ``None`` marks a known event that carries no parcel state
# (notifications, signature notes), so it never triggers the warning.
EVENT_STATUS_MAP: dict[str, ParcelStatus | None] = {
    "we have received information about an upcoming delivery from the sender": ParcelStatus.REGISTERED,
    "item has been registered": ParcelStatus.REGISTERED,
    "the item is in sorting": ParcelStatus.IN_TRANSIT,
    "the item is in transport": ParcelStatus.IN_TRANSIT,
    "item accepted from transport": ParcelStatus.IN_TRANSIT,
    "item is in delivery transportation": ParcelStatus.OUT_FOR_DELIVERY,
    "item has been released for delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "item is ready for a pick up": ParcelStatus.AT_PICKUP_POINT,
    "the item has been delivered": ParcelStatus.DELIVERED,
    "item has not been collected.returned to sender": ParcelStatus.RETURNING,
    "the item has been returned to the sender": ParcelStatus.RETURNING,
    "we sent the recipient an email about the item": None,
    "we sent the recipient an omaposti notification about the item": None,
    "we have tried to reach the recipient again by email": None,
    "we have tried to reach the recipient again by sms": None,
    "no digital signature was received": None,
}

_unmapped_statuses_logged: set[str] = set()
_unmapped_events_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped ``status.main`` literal once per HA session."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Posti tracking status.main — help us map it. Open an "
        "issue and paste this line: %s\n  status.main=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a ``status.main`` literal to a canonical :class:`ParcelStatus`.

    ``None`` (no hit yet) reports ``unknown`` silently; an unrecognised
    literal reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def _event_key(description: str | None) -> str:
    return " ".join((description or "").split()).rstrip(".").casefold()


def map_event_status(description: str | None) -> ParcelStatus | None:
    """Map one event's text to a canonical status, or ``None``.

    Unrecognised text warns once so new wording can be added to the map.
    """
    key = _event_key(description)
    if not key:
        return None
    if key in EVENT_STATUS_MAP:
        return EVENT_STATUS_MAP[key]
    if key not in _unmapped_events_logged:
        _unmapped_events_logged.add(key)
        _LOGGER.warning(
            "Unrecognised Posti tracking event — help us map it. Open an "
            "issue and paste this line: %s\n  eventDescription=%s → history "
            "status left empty",
            NEW_ISSUE_URL,
            description,
        )
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the tracking route's ``events``.

    ``events[]`` arrives newest-first and events can share a timestamp, so
    the input is reversed before the stable sort to keep their true order.
    """
    parseable: list[tuple[datetime, dict]] = []
    for event in reversed(events or []):
        if not isinstance(event, dict):
            continue
        timestamp = event.get("timestamp")
        if not timestamp:
            continue
        parsed = parse_iso(timestamp)
        if parsed is None:
            continue
        parseable.append(
            (
                parsed,
                {
                    "timestamp": timestamp,
                    "status": map_event_status(event.get("eventDescription")),
                    "raw_status": event.get("eventDescription"),
                },
            )
        )
    parseable.sort(key=lambda item: item[0])
    return [entry for _, entry in parseable][-max_events:]


def tracking_url(code: str | None) -> str | None:
    """Return the public posti.fi tracking page for a code."""
    if not code:
        return None
    return TRACKING_URL.format(code=quote(code, safe=""))


def _delivered_at(events: list | None) -> str | None:
    """Return the newest event's timestamp — the only ``delivered_at`` source.

    The public route has no delivery-time field at all, so this derivation
    is the entire mechanism, applied **only** when ``status.main ==
    DELIVERED`` and never by matching event text — a single confirmed sample
    carried three distinct delivery-shaped phrases, so text matching would be
    unreliable even though the timestamp itself checked out.
    """
    if not events:
        return None
    newest = events[0]
    if not isinstance(newest, dict):
        return None
    return newest.get("timestamp")


def _pickup_point(events: list | None) -> str | None:
    """Return where the parcel waited: the newest ready-for-pickup event's ``city``.

    On that event Posti puts the pickup point's name in ``city``.
    """
    for event in events or []:
        if (
            isinstance(event, dict)
            and _event_key(event.get("eventDescription")) == "item is ready for a pick up"
        ):
            return event.get("city") or None
    return None


def normalize_parcel(
    raw: dict, *, tracking_code: str, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict for one ``consumerSearchShipments`` hit.

    ``tracking_code`` is the tracked code itself — never read from ``raw``,
    since a not-yet-found lookup carries no echoed identifier at all. No
    field on this route gives a delivery time, an ETA window, a
    sender/receiver name, a product name, a weight or a dimension — every one
    of those stays ``None`` structurally, not by omission.
    """
    status_block = raw.get("status") or {}
    status_code = status_block.get("main")
    status = map_parcel_status(status_code)
    delivered = status is ParcelStatus.DELIVERED
    events = raw.get("events")

    return {
        "carrier": "Posti",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": status_code,
        "delivered": delivered,
        "delivered_at": _delivered_at(events) if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": _pickup_point(events),
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing
    or unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
