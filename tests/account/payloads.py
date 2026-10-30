"""Sample account shipment payloads shared by the test modules."""

from __future__ import annotations

TRACKING_NUMBER = "JJFI00000000000009"
SHIPMENT_NUMBER = "SHIPMENT-9"


def event(timestamp: str, text: str, city: str | None = None) -> dict:
    return {"timestamp": timestamp, "eventDescription": text, "city": city}


def shipment(
    phase: str = "IN_TRANSPORT",
    *,
    tracking_number: str | None = TRACKING_NUMBER,
    shipment_number: str = SHIPMENT_NUMBER,
    role: str = "RECEIVER",
) -> dict:
    return {
        "shipmentId": shipment_number,
        "displayId": tracking_number,
        "userRole": role,
        "status": {"main": phase},
        "events": [
            event("2026-05-01T09:00:00.000Z", "Shipment announced"),
            event("2026-05-02T10:00:00.000Z", "At the terminal", "Helsinki"),
        ],
    }


def full_shipment(phase: str = "IN_TRANSPORT") -> dict:
    """A shipment with every field the account query selects."""
    return shipment(phase) | {
        "displayName": "Example parcel",
        "delivery": {
            "time": {"timestamp": "2026-05-03T12:00:00.000Z"},
            "destination": {"name": ["Example Recipient"], "city": "ESPOO"},
        },
        "pickupPoint": {
            "address": {"publicName": "Example Store", "city": "ESPOO"},
        },
        "measurements": {"weight": {"value": "1.25"}},
    }
