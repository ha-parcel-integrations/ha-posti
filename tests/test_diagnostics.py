"""Tests for Posti diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.posti.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.data = {}
    entry.options = {"parcels": [{"tracking_code": "JJFI00000000000001"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "JJFI00000000000001",
            "sender": None,
            "receiver": None,
            "status": "delivered",
            "raw": {
                "displayId": "JJFI00000000000001",
                "status": {"main": "DELIVERED", "subStatus": []},
                "events": [{"timestamp": "2026-01-01T00:00:00Z", "eventDescription": "x"}],
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["displayId"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["events"][0]["eventDescription"] == "**REDACTED**"
    assert result["incoming"][0]["status"] == "delivered"


async def test_diagnostics_redacts_account_entry_data():
    entry = MagicMock()
    entry.data = {
        "source": "account",
        "username": "jane.doe",
        "id_token": "secret-id-token",
        "refresh_token": "secret-refresh-token",
        "role_tokens": [{"type": "consumer", "token": "x", "email": "jane@example.com"}],
    }
    entry.options = {}
    entry.runtime_data.coordinator.current_tier_minutes = 45
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=45)
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert result["entry_data"]["username"] == "**REDACTED**"
    assert result["entry_data"]["id_token"] == "**REDACTED**"
    assert result["entry_data"]["refresh_token"] == "**REDACTED**"
    assert result["entry_data"]["role_tokens"] == "**REDACTED**"
    assert result["entry_data"]["source"] == "account"


async def test_diagnostics_reports_suspended_polling():
    entry = MagicMock()
    entry.data = {}
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
