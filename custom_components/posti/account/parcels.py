"""Canonical parcel shape for the OmaPosti account-inbox source.

Every mapping here is a hypothesis, not a confirmed shape: no real account
response has confirmed the envelope, event ordering, field optionality or
any of the seven ``shipmentPhase`` literals. Ship behind the one-shot warning
net until a real account confirms it — do not promote this module to 1.0
semantics on the strength of the tracking route's own gate.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..const import HISTORY_MAX_EVENTS, ParcelStatus
from ..status import NEW_ISSUE_URL
from ..tracking.parcels import parse_iso, tracking_url

_LOGGER = logging.getLogger(__name__)

# Unconfirmed hypotheses: none of these seven literals has been seen on a
# real account response. Never share this map with the tracking route's
# ``status.main`` vocabulary — the two are shaped differently and any overlap
# in spelling (``DELIVERED``) is coincidence, not evidence.
STATUS_MAP: dict[str, ParcelStatus] = {
    "WAITING": ParcelStatus.REGISTERED,
    "RECEIVED": ParcelStatus.IN_TRANSIT,
    "IN_TRANSPORT": ParcelStatus.IN_TRANSIT,
    "IN_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "READY_FOR_PICKUP": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED": ParcelStatus.DELIVERED,
    "RETURNED_TO_SENDER": ParcelStatus.RETURNING,
}

_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unrecognised ``shipmentPhase`` once per HA session.

    Worth reporting even though the map is already a hypothesis: a phase
    outside these seven means OmaPosti uses one nobody has observed at all,
    not just one this integration mapped wrong.
    """
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Posti account shipmentPhase — help us map it. Open an "
        "issue and paste this line: %s\n  shipmentPhase=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a ``shipmentPhase`` literal to a canonical :class:`ParcelStatus`."""
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def _barcode(raw: dict[str, Any]) -> str | None:
    """Prefer the first tracking number; fall back to the shipment number.

    A code-less account shipment uses ``shipmentNumber`` as the canonical
    barcode fallback.
    """
    tracking_numbers = raw.get("trackingNumbers")
    if isinstance(tracking_numbers, list) and tracking_numbers:
        first = tracking_numbers[0]
        if isinstance(first, str) and first:
            return first
    shipment_number = raw.get("shipmentNumber")
    return shipment_number if isinstance(shipment_number, str) and shipment_number else None


def _event_description(event: dict[str, Any]) -> str | None:
    """Pick the English description, falling back to whatever came back."""
    description = event.get("eventDescription")
    if not isinstance(description, dict):
        return None
    return description.get("value")


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the account's ``events``.

    The order is source-derived (oldest-first) rather than confirmed, so
    events are sorted on their own parsed timestamp with the original array
    order kept as the tie-breaker (a stable sort achieves this for free) —
    not simply trusted or reversed. Each event carries no status code of its
    own, only free text, so ``status`` is always ``None``.
    """
    parseable: list[tuple[datetime, dict]] = []
    for event in events or []:
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
                    "status": None,
                    "raw_status": _event_description(event),
                },
            )
        )
    parseable.sort(key=lambda item: item[0])
    return [entry for _, entry in parseable][-max_events:]


def normalize_account_parcel(raw: dict[str, Any], *, include_history: bool = False) -> dict[str, Any]:
    """Return the canonical shape, leaving every unproven account field empty.

    ``delivered_at``, ``planned_from``/``planned_to``, ``pickup_point``,
    ``weight`` and ``dimensions`` all stay ``None`` — the minimal query this
    source runs selects none of the fields that would populate them, and none
    may be inferred from a plausible name.
    """
    status_code = raw.get("shipmentPhase")
    status = map_parcel_status(status_code)
    barcode = _barcode(raw)

    return {
        "carrier": "Posti",
        "barcode": barcode,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": status_code,
        "delivered": status is ParcelStatus.DELIVERED,
        "delivered_at": None,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": None,
        "url": tracking_url(barcode),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw.get("events")) if include_history else None,
        "raw": raw,
    }
