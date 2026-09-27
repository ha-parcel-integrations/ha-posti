"""Sample tracking-route GraphQL hits shared by the test modules."""
from __future__ import annotations

TRACKING_CODE = "JJFI00000000000001"


def event(timestamp: str, description: str, city: str | None = None) -> dict:
    """One ``events[]`` entry, newest-first order expected by the caller."""
    return {"timestamp": timestamp, "eventDescription": description, "city": city}


def delivered_hit(code: str = TRACKING_CODE) -> dict:
    """A representative confirmed-delivered ``consumerSearchShipments`` hit."""
    return {
        "displayId": code,
        "shipmentType": "LETTER",
        "status": {"main": "DELIVERED", "subStatus": []},
        "events": [
            event("2026-04-29T13:12:42.000Z", "The item has been delivered"),
            event("2026-04-29T08:46:00.000Z", "Item has been released for delivery"),
            event("2026-04-28T15:52:17.000Z", "The item is in sorting", "HELSINKI"),
            event("2026-04-27T23:03:58.000Z", "Item has been registered"),
        ],
    }


def unknown_status_hit(code: str = TRACKING_CODE) -> dict:
    """A hit whose ``status.main`` has never been mapped."""
    return {
        "displayId": code,
        "shipmentType": "PARCEL",
        "status": {"main": "IN_TRANSPORT", "subStatus": []},
        "events": [event("2026-04-27T23:03:58.000Z", "Item has been registered")],
    }


def returned_hit(code: str = TRACKING_CODE) -> dict:
    """A parcel left uncollected at a pickup point and returned to its sender."""
    return {
        "displayId": code,
        "shipmentType": "PARCEL",
        "status": {"main": "RETURN_DELIVERED", "subStatus": []},
        "events": [
            event("2026-06-09T07:18:42.000Z", "The item has been delivered"),
            event("2026-06-09T07:18:42.000Z", "No digital signature was received.", "ESPOO"),
            event("2026-06-09T06:31:53.000Z", "Item is in delivery transportation", "ESPOO"),
            event("2026-06-08T05:15:03.000Z", "The item has been returned to the sender.", "VANTAA"),
            event("2026-06-08T05:13:14.000Z", "Item has not been collected.Returned to sender.", "Parcel locker, Example Store"),
            event("2026-06-04T06:30:22.000Z", "We have tried to reach the recipient again by SMS"),
            event("2026-06-01T06:18:33.000Z", "Item is ready for a pick up ", "Parcel locker, Example Store"),
            event("2026-05-29T12:47:38.000Z", "Item accepted from transport", "VANTAA"),
            event("2026-05-28T17:54:17.000Z", "The item is in sorting", "VANTAA"),
            event("2026-05-28T09:50:00.000Z", "We have received information about an upcoming delivery from the sender"),
        ],
    }
