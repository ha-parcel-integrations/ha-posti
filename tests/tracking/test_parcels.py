"""Tests for the tracking-code source's canonical parcel mapping."""
from __future__ import annotations

from custom_components.posti.const import ParcelStatus
from custom_components.posti.tracking import parcels

from .payloads import TRACKING_CODE, delivered_hit, returned_hit, unknown_status_hit


def test_delivered_hit_maps_to_delivered():
    parcel = parcels.normalize_parcel(delivered_hit(), tracking_code=TRACKING_CODE)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["raw_status"] == "DELIVERED"


def test_delivered_at_is_the_newest_event_timestamp():
    parcel = parcels.normalize_parcel(delivered_hit(), tracking_code=TRACKING_CODE)
    assert parcel["delivered_at"] == "2026-04-29T13:12:42.000Z"


def test_delivered_at_is_none_when_not_delivered():
    parcel = parcels.normalize_parcel(unknown_status_hit(), tracking_code=TRACKING_CODE)
    assert parcel["delivered_at"] is None


def test_unmapped_status_warns_once_and_reports_unknown(caplog):
    parcels._unmapped_statuses_logged.clear()
    parcel = parcels.normalize_parcel(unknown_status_hit(), tracking_code=TRACKING_CODE)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "IN_TRANSPORT"
    assert "IN_TRANSPORT" in caplog.text

    caplog.clear()
    parcels.normalize_parcel(unknown_status_hit(), tracking_code=TRACKING_CODE)
    assert "IN_TRANSPORT" not in caplog.text


def test_missing_status_reports_unknown_silently(caplog):
    parcels._unmapped_statuses_logged.clear()
    parcel = parcels.normalize_parcel({}, tracking_code=TRACKING_CODE)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert caplog.text == ""


def test_barcode_comes_from_the_tracked_code_not_the_payload():
    """A not-yet-found lookup carries no echoed identifier at all."""
    parcel = parcels.normalize_parcel({}, tracking_code=TRACKING_CODE)
    assert parcel["barcode"] == TRACKING_CODE


def test_no_confirmed_field_ever_populates_eta_weight_or_dimensions():
    parcel = parcels.normalize_parcel(delivered_hit(), tracking_code=TRACKING_CODE)
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["pickup_point"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_history_is_reversed_to_oldest_first():
    parcel = parcels.normalize_parcel(
        delivered_hit(), tracking_code=TRACKING_CODE, include_history=True
    )
    history = parcel["history"]
    assert [entry["timestamp"] for entry in history] == [
        "2026-04-27T23:03:58.000Z",
        "2026-04-28T15:52:17.000Z",
        "2026-04-29T08:46:00.000Z",
        "2026-04-29T13:12:42.000Z",
    ]
    assert [entry["status"] for entry in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert history[0]["raw_status"] == "Item has been registered"


def test_history_is_none_when_option_is_off():
    parcel = parcels.normalize_parcel(delivered_hit(), tracking_code=TRACKING_CODE)
    assert parcel["history"] is None


def test_history_skips_events_without_a_parseable_timestamp():
    raw = delivered_hit()
    raw["events"].append({"timestamp": None, "eventDescription": "broken"})
    raw["events"].append("not a dict")
    history = parcels.build_history(raw["events"])
    assert len(history) == 4


def test_history_caps_at_max_events():
    events = [
        {"timestamp": f"2026-01-{day:02d}T00:00:00.000Z", "eventDescription": "e"}
        for day in range(1, 26)
    ]
    history = parcels.build_history(events, max_events=5)
    assert len(history) == 5
    assert history[-1]["timestamp"] == "2026-01-25T00:00:00.000Z"


def test_sort_parcels_by_ts_pushes_unparseable_to_the_end():
    parcels_list = [
        {"barcode": "a", "planned_from": None},
        {"barcode": "b", "planned_from": "2026-01-02T00:00:00Z"},
        {"barcode": "c", "planned_from": "2026-01-01T00:00:00Z"},
    ]
    result = parcels.sort_parcels_by_ts(parcels_list, "planned_from")
    assert [p["barcode"] for p in result] == ["c", "b", "a"]


def test_apply_delivered_filter_by_days():
    import datetime as dt

    entry = type(
        "Entry",
        (),
        {"options": {"delivered_filter_type": "days", "delivered_filter_amount": 1}},
    )()
    now = dt.datetime.now(dt.timezone.utc)
    old = (now - dt.timedelta(days=5)).isoformat()
    recent = (now - dt.timedelta(hours=1)).isoformat()
    result = parcels.apply_delivered_filter(
        [{"delivered_at": recent}, {"delivered_at": old}], entry
    )
    assert result == [{"delivered_at": recent}]


def test_apply_delivered_filter_by_count():
    entry = type(
        "Entry",
        (),
        {"options": {"delivered_filter_type": "parcels", "delivered_filter_amount": 1}},
    )()
    result = parcels.apply_delivered_filter(
        [{"delivered_at": "2026-01-02T00:00:00Z"}, {"delivered_at": "2026-01-01T00:00:00Z"}],
        entry,
    )
    assert len(result) == 1


def test_parse_iso_rejects_garbage():
    assert parcels.parse_iso("not-a-date") is None
    assert parcels.parse_iso(None) is None


def test_url_is_the_public_tracking_page():
    parcel = parcels.normalize_parcel(delivered_hit(), tracking_code=TRACKING_CODE)
    assert parcel["url"] == f"https://www.posti.fi/seuranta/{TRACKING_CODE}"


def test_tracking_url_escapes_the_code_and_skips_empty():
    assert parcels.tracking_url("A B/1") == "https://www.posti.fi/seuranta/A%20B%2F1"
    assert parcels.tracking_url(None) is None
    assert parcels.tracking_url("") is None


def test_return_delivered_is_returning_not_delivered():
    parcel = parcels.normalize_parcel(returned_hit(), tracking_code=TRACKING_CODE)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_pickup_point_comes_from_the_ready_for_pickup_event():
    parcel = parcels.normalize_parcel(returned_hit(), tracking_code=TRACKING_CODE)
    assert parcel["pickup_point"] == "Parcel locker, Example Store"
    assert parcel["pickup"] is False


def test_pickup_point_is_none_without_a_pickup_event():
    parcel = parcels.normalize_parcel(delivered_hit(), tracking_code=TRACKING_CODE)
    assert parcel["pickup_point"] is None


def test_history_maps_event_text_to_statuses(caplog):
    parcels._unmapped_events_logged.clear()
    parcel = parcels.normalize_parcel(
        returned_hit(), tracking_code=TRACKING_CODE, include_history=True
    )
    assert [entry["status"] for entry in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.AT_PICKUP_POINT,
        None,
        ParcelStatus.RETURNING,
        ParcelStatus.RETURNING,
        ParcelStatus.OUT_FOR_DELIVERY,
        None,
        ParcelStatus.DELIVERED,
    ]
    assert "Unrecognised Posti tracking event" not in caplog.text


def test_history_keeps_same_timestamp_events_in_carrier_order():
    parcel = parcels.normalize_parcel(
        returned_hit(), tracking_code=TRACKING_CODE, include_history=True
    )
    assert parcel["history"][-1]["raw_status"] == "The item has been delivered"


def test_unmapped_event_text_warns_once_and_leaves_status_empty(caplog):
    parcels._unmapped_events_logged.clear()
    assert parcels.map_event_status("Something new happened") is None
    assert parcels.map_event_status("Something new happened.") is None
    assert caplog.text.count("Unrecognised Posti tracking event") == 1


def test_known_non_status_event_text_does_not_warn(caplog):
    parcels._unmapped_events_logged.clear()
    assert parcels.map_event_status("We sent the recipient an email about the item.") is None
    assert "Unrecognised Posti tracking event" not in caplog.text
