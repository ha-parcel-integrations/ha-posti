"""Canonical parcel shape and list helpers for the tracking-code source.

Every function here is pure — no I/O, no Home Assistant objects beyond the
config entry's options. The parcel-list helpers (``parse_iso``,
``sort_parcels_by_ts``, ``apply_delivered_filter``, ``tracking_url``) are
genuinely source-agnostic and are imported by ``account/parcels.py`` too,
rather than duplicated — everything *status*- or *field*-shaped stays local
to each source (see ``status.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

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

POSTI_ZONE = ZoneInfo("Europe/Helsinki")

# Only add a literal once a real parcel has shown it.
RETURN_DELIVERED = "RETURN_DELIVERED"
STATUS_MAP: dict[str, ParcelStatus] = {
    "DELIVERED": ParcelStatus.DELIVERED,
    RETURN_DELIVERED: ParcelStatus.RETURNING,
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
_delivery_time_types_logged: set[str] = set()


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
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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


def _pickup_point(events: list | None, point: Any) -> str | None:
    """Return where the parcel waits or waited.

    The newest ready-for-pickup event's ``city`` (Posti puts the pickup
    point's name there) wins: on a returned parcel ``pickupPoint`` was seen
    still naming a different, earlier point than the one the parcel actually
    waited at. Before that event, ``pickupPoint`` is all there is.
    """
    for event in events or []:
        if (
            isinstance(event, dict)
            and _event_key(event.get("eventDescription"))
            == "item is ready for a pick up"
        ):
            return event.get("city") or None
    address = point.get("address") if isinstance(point, dict) else None
    if isinstance(address, dict):
        return address.get("publicName") or None
    return None


def _receiver(delivery: dict) -> str | None:
    destination = delivery.get("destination")
    if not isinstance(destination, dict):
        return None
    name = destination.get("name")
    if isinstance(name, list):
        name = ", ".join(part for part in name if isinstance(part, str) and part)
    return name or None


def _planned(delivery: dict) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` from ``delivery.time``.

    Only ever seen as ``null``, so the window reading of ``timestamp`` /
    ``timestampLatest`` is a guess; each new ``type`` warns once. A lone
    ``timestamp`` runs to the end of its day.
    """
    window = delivery.get("time")
    if not isinstance(window, dict) or not window.get("timestamp"):
        return None, None
    kind = str(window.get("type"))
    if kind not in _delivery_time_types_logged:
        _delivery_time_types_logged.add(kind)
        _LOGGER.warning(
            "Posti sent a delivery time we have not seen before — help us confirm "
            "it. Open an issue and paste this line: %s\n  delivery.time.type=%s, "
            "has timestampLatest=%s",
            NEW_ISSUE_URL,
            kind,
            bool(window.get("timestampLatest")),
        )
    return planned_window(
        window["timestamp"], window.get("timestampLatest"), zone=POSTI_ZONE
    )


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def _quantity(quantity: Any, factors: dict[str, float]) -> float | None:
    """Convert a ``{"unit": ..., "value": "0.1"}`` measurement using ``factors``."""
    if not isinstance(quantity, dict):
        return None
    factor = factors.get(str(quantity.get("unit")).lower())
    raw_value = quantity.get("value")
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value * factor if factor is not None else None


def _weight_kg(measurements: dict) -> float | None:
    return _quantity(measurements.get("weight"), {"kg": 1, "g": 0.001})


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``).
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{round(length)} x {round(width)} x {round(height)} cm",
    }


def _dimensions_cm(measurements: dict) -> dict[str, Any] | None:
    factors = {"m": 100, "cm": 1, "mm": 0.1}
    axes = [
        _quantity(measurements.get(axis), factors)
        for axis in ("length", "width", "height")
    ]
    rounded = [round(axis, 1) for axis in axes if axis is not None]
    if len(rounded) != len(axes):
        return None
    return format_dimensions(*rounded)


def normalize_parcel(
    raw: dict, *, tracking_code: str, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict for one ``consumerSearchShipments`` hit.

    ``tracking_code`` is the tracked code itself — never read from ``raw``,
    since a not-yet-found lookup carries no echoed identifier at all. No
    field on this route names the sender.
    """
    status_block = raw.get("status") or {}
    status_code = status_block.get("main")
    status = map_parcel_status(status_code)
    # The contract counts a return as delivered only with evidence it is back at
    # the sender; RETURN_DELIVERED's wording is unconfirmed, so it stays returning.
    delivered = status is ParcelStatus.DELIVERED
    events = raw.get("events")
    measurements = raw.get("measurements") or {}
    raw_delivery = raw.get("delivery")
    delivery = raw_delivery if isinstance(raw_delivery, dict) else {}
    planned_from, planned_to = planned_for_status(status, *_planned(delivery))
    point = raw.get("pickupPoint")
    pickup = is_pickup(
        isinstance(point, dict) and bool(point.get("address")),
        status,
        (
            EVENT_STATUS_MAP.get(_event_key(event.get("eventDescription")))
            for event in events or []
            if isinstance(event, dict)
        ),
    )

    return {
        "carrier": "Posti",
        "barcode": tracking_code,
        "sender": None,
        "receiver": _receiver(delivery),
        "status": status,
        "raw_status": status_code,
        "delivered": delivered,
        "delivered_at": _delivered_at(events) if delivered else None,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": _pickup_point(events, point) if pickup else None,
        "url": tracking_url(tracking_code),
        "weight": _weight_kg(measurements),
        "dimensions": _dimensions_cm(measurements),
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
