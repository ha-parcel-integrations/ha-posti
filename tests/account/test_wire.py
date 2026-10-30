"""Tests for the HTTP plumbing of the OmaPosti login."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.posti.account import wire
from custom_components.posti.account.errors import (
    PostiAccountApiError,
    PostiAccountNotJson,
)
from custom_components.posti.account.wire import Page
from custom_components.posti.const import APP_PACKAGE


def _session_with(response=None, error=None):
    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response, side_effect=error)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=ctx)
    session.post = MagicMock(return_value=ctx)
    return session


def _response(status=200, text="", json_body=None, headers=None, json_error=None):
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.text = AsyncMock(return_value=text)
    response.json = AsyncMock(return_value=json_body, side_effect=json_error)
    return response


async def test_fetch_page_sends_the_web_view_headers_and_keeps_the_url_encoded():
    session = _session_with(_response(text="page", headers={"Location": "/next"}))
    page = await wire.fetch_page(
        session, "https://todentaminen.posti.fi/x?request=a%2Bb", follow=False
    )
    assert page == Page("/next", "page")
    kwargs = session.get.call_args.kwargs
    assert kwargs["headers"]["X-Requested-With"] == APP_PACKAGE
    assert kwargs["allow_redirects"] is False
    assert "%2B" in str(session.get.call_args.args[0])


async def test_fetch_page_error_status_is_an_api_error():
    with pytest.raises(PostiAccountApiError) as err:
        await wire.fetch_page(_session_with(_response(status=503)), "https://x")
    assert err.value.status_code == 503


async def test_fetch_page_timeout_names_its_type():
    with pytest.raises(PostiAccountApiError, match="TimeoutError"):
        await wire.fetch_page(_session_with(error=TimeoutError()), "https://x")


async def test_call_json_returns_status_and_object():
    session = _session_with(_response(status=201, json_body={"a": 1}))
    assert await wire.call_json(session, "https://x") == (201, {"a": 1})


async def test_call_json_not_json_carries_the_status():
    session = _session_with(_response(status=403, json_error=ValueError("html")))
    with pytest.raises(PostiAccountNotJson) as err:
        await wire.call_json(session, "https://x")
    assert err.value.status_code == 403


async def test_call_json_rejects_a_non_object():
    session = _session_with(_response(json_body=[1]))
    with pytest.raises(PostiAccountApiError):
        await wire.call_json(session, "https://x")


async def test_call_json_timeout_names_its_type():
    with pytest.raises(PostiAccountApiError, match="TimeoutError"):
        await wire.call_json(_session_with(error=TimeoutError()), "https://x")


def test_error_code_reads_every_shape():
    nested = {"error": {"code": 403, "message": "INACTIVE_DEVICE"}}
    assert wire.error_code(nested) == "INACTIVE_DEVICE"
    assert wire.error_code({"error": {"code": 404}}) == 404
    assert wire.error_code({"error": "invalid_grant"}) == "invalid_grant"
    assert wire.error_code({"message": "m"}) == "m"
    assert wire.error_code({}) is None
