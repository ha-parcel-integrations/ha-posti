"""Canonical parcel shape for the OmaPosti account source.

Only ``DELIVERED`` and ``RETURN_DELIVERED`` have been seen on a real parcel
(on the tracking route, which shares the ``status.main`` field). Every other
literal and the account-only fields are source-derived, so the module ships
behind the one-shot warning net until a real account confirms them.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from ..const import HISTORY_MAX_EVENTS, ParcelStatus
from ..status import NEW_ISSUE_URL
from ..tracking.parcels import parse_iso, tracking_url

_LOGGER = logging.getLogger(__name__)

ROLE_RECEIVER = "RECEIVER"
ROLE_SENDER = "SENDER"

# A separate map from the tracking route's: this one knows the shipment's
# direction, and the tracking route maps event text besides ``status.main``.
STATUS_MAP: dict[str, ParcelStatus] = {
    "ORDER_RECEIVED": ParcelStatus.REGISTERED,
    "WAITING": ParcelStatus.REGISTERED,
    "RECEIVED": ParcelStatus.IN_TRANSIT,
    "IN_TRANSPORT": ParcelStatus.IN_TRANSIT,
    "IN_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "READY_FOR_PICKUP": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED": ParcelStatus.DELIVERED,
    "RETURN_DELIVERED": ParcelStatus.RETURNING,
}
# A sent shipment that comes back to the user goes through the stages again;
# a received one on its way back to its sender is simply returning.
RETURN_TO_USER_MAP: dict[str, ParcelStatus] = {
    "RETURN_WAITING": ParcelStatus.REGISTERED,
    "RETURN_IN_TRANSPORT": ParcelStatus.IN_TRANSIT,
    "RETURN_READY_FOR_PICKUP": ParcelStatus.AT_PICKUP_POINT,
}
RETURN_PREFIX = "RETURN_"

_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unrecognised ``status.main`` once per HA session."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Posti account status.main — help us map it. Open an "
        "issue and paste this line: %s\n  status.main=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None, role: str | None = None) -> ParcelStatus:
    """Map a ``status.main`` literal to a canonical :class:`ParcelStatus`."""
    if not code:
        return ParcelStatus.UNKNOWN
    if role == ROLE_SENDER and code in RETURN_TO_USER_MAP:
        return RETURN_TO_USER_MAP[code]
    mapped = STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    if code.startswith(RETURN_PREFIX) and role != ROLE_SENDER:
        # A received parcel on its way back is returning, whatever the stage.
        _warn_unmapped_status(code)
        return ParcelStatus.RETURNING
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def is_incoming(raw: dict[str, Any]) -> bool:
    """Whether a shipment comes to the user.

    One the user sent counts as outgoing, unless it is on its way back. A role
    nobody has seen yet counts as incoming, so a new value can never make
    parcels disappear.
    """
    if raw.get("userRole") != ROLE_SENDER:
        return True
    status = raw.get("status")
    code = status.get("main") if isinstance(status, dict) else None
    return isinstance(code, str) and code.startswith(RETURN_PREFIX)


def _barcode(raw: dict[str, Any]) -> str | None:
    for key in ("displayId", "shipmentId"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _join(value: Any) -> str | None:
    if isinstance(value, list):
        value = ", ".join(part for part in value if isinstance(part, str) and part)
    return value if isinstance(value, str) and value else None


def _delivery(raw: dict[str, Any]) -> dict[str, Any]:
    delivery = raw.get("delivery")
    return delivery if isinstance(delivery, dict) else {}


def _destination_name(raw: dict[str, Any]) -> str | None:
    destination = _delivery(raw).get("destination")
    return _join(destination.get("name")) if isinstance(destination, dict) else None


def _planned_from(raw: dict[str, Any]) -> str | None:
    time = _delivery(raw).get("time")
    stamp = time.get("timestamp") if isinstance(time, dict) else None
    return stamp if isinstance(stamp, str) and parse_iso(stamp) else None


def _pickup_point(raw: dict[str, Any]) -> str | None:
    point = raw.get("pickupPoint")
    address = point.get("address") if isinstance(point, dict) else None
    if not isinstance(address, dict):
        return None
    return _join(address.get("publicName")) or _join(address.get("city"))


def _weight_kg(raw: dict[str, Any]) -> float | None:
    measurements = raw.get("measurements")
    weight = measurements.get("weight") if isinstance(measurements, dict) else None
    value = weight.get("value") if isinstance(weight, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _delivered_at(events: list | None) -> str | None:
    """Return the newest event's timestamp, whatever the list order."""
    newest: tuple[datetime, str] | None = None
    for event in events or []:
        if not isinstance(event, dict):
            continue
        parsed = parse_iso(event.get("timestamp"))
        if parsed is not None and (newest is None or parsed > newest[0]):
            newest = (parsed, event["timestamp"])
    return newest[1] if newest else None


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the shipment's ``events``.

    Sorted on each event's own timestamp (equal ones keep Posti's order), not
    on its place in the list. An event carries text only, so ``status`` is
    always ``None``.
    """
    parseable: list[tuple[datetime, dict]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = event.get("timestamp")
        parsed = parse_iso(timestamp) if timestamp else None
        if parsed is None:
            continue
        text = event.get("eventDescription")
        parseable.append(
            (
                parsed,
                {
                    "timestamp": timestamp,
                    "status": None,
                    "raw_status": text if isinstance(text, str) and text else None,
                },
            )
        )
    parseable.sort(key=lambda item: item[0])
    return [entry for _, entry in parseable][-max_events:]


def normalize_account_parcel(
    raw: dict[str, Any], *, include_history: bool = False
) -> dict[str, Any]:
    """Return the canonical shape for one account shipment."""
    status_block = raw.get("status")
    status_code = status_block.get("main") if isinstance(status_block, dict) else None
    status = map_parcel_status(status_code, raw.get("userRole"))
    barcode = _barcode(raw)
    delivered = status is ParcelStatus.DELIVERED
    events = raw.get("events")
    # displayName is the other party: the sender of a received shipment, the
    # recipient of a sent one. A sent one's destination name only repeats it.
    other = _join(raw.get("displayName"))
    outgoing = not is_incoming(raw)

    return {
        "carrier": "Posti",
        "barcode": barcode,
        "sender": None if outgoing else other,
        "receiver": other if outgoing else _destination_name(raw),
        "status": status,
        "raw_status": status_code,
        "delivered": delivered,
        "delivered_at": _delivered_at(events) if delivered else None,
        "planned_from": None if delivered else _planned_from(raw),
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": _pickup_point(raw),
        "url": tracking_url(barcode),
        "weight": _weight_kg(raw),
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }
