"""Sample account-inbox shipment payloads shared by the test modules."""
from __future__ import annotations

TRACKING_NUMBER = "JJFI00000000000009"
SHIPMENT_NUMBER = "SHIPMENT-9"


def description(value: str, lang: str = "en") -> dict:
    return {"lang": lang, "value": value}


def event(timestamp: str, text: str, city: str | None = None) -> dict:
    return {
        "timestamp": timestamp,
        "eventDescription": description(text),
        "eventLocation": {"city": city, "country": "FI"} if city else None,
    }


def shipment(
    phase: str = "IN_TRANSPORT",
    *,
    tracking_number: str | None = TRACKING_NUMBER,
    shipment_number: str = SHIPMENT_NUMBER,
) -> dict:
    return {
        "shipmentNumber": shipment_number,
        "trackingNumbers": [tracking_number] if tracking_number else [],
        "shipmentPhase": phase,
        "savedDateTime": "2026-05-01T09:00:00.000Z",
        "events": [
            event("2026-05-01T09:00:00.000Z", "Shipment announced"),
            event("2026-05-02T10:00:00.000Z", "At the terminal", "Helsinki"),
        ],
    }
