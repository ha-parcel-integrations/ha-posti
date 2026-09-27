"""Tests for the shared device-info builder."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.posti.const import (
    CONF_SOURCE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.posti.device import build_device_info


def test_tracking_hub_device_name():
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_TRACKING})
    info = build_device_info(entry)
    assert info["name"] == "Posti"


def test_account_device_name_includes_username():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "jane.doe"},
    )
    info = build_device_info(entry)
    assert info["name"] == "Posti (jane.doe)"


def test_account_device_without_username_falls_back():
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE: SOURCE_ACCOUNT})
    info = build_device_info(entry)
    assert info["name"] == "Posti"
