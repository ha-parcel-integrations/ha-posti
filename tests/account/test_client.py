"""Tests for the OmaPosti account GraphQL client."""
from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.posti.account.auth import (
    PostiAccountApiError,
    PostiAccountReauthRequired,
)
from custom_components.posti.account.client import PostiAccountClient

ROLE_TOKENS = [{"type": "consumer", "token": "consumer-token"}]


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _resp(status=200, json_body=None):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=json_body)
    return response


def _ctx(response):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(responses):
    session = MagicMock()
    session.post = MagicMock(side_effect=[_ctx(r) for r in responses])
    return session


def _fresh_id_token() -> str:
    return _jwt(time.time() + 3600)


async def test_returns_shipments_on_success():
    session = _session([_resp(200, {"data": {"shipment": [{"shipmentNumber": "S1"}]}})])
    client = PostiAccountClient(
        session,
        id_token=_fresh_id_token(),
        refresh_token="rt",
        role_tokens=ROLE_TOKENS,
    )

    shipments = await client.async_get_shipments()

    assert shipments == [{"shipmentNumber": "S1"}]
    call = session.post.call_args_list[0]
    assert call.kwargs["headers"]["X-Omaposti-Roles"] == "consumer-token"


async def test_none_shipment_field_returns_empty_list():
    session = _session([_resp(200, {"data": {"shipment": None}})])
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    assert await client.async_get_shipments() == []


async def test_non_list_shipment_field_raises():
    session = _session([_resp(200, {"data": {"shipment": "oops"}})])
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    with pytest.raises(Exception):
        await client.async_get_shipments()


async def test_refreshes_proactively_when_close_to_expiry():
    callback = AsyncMock()
    session = _session([_resp(200, {"data": {"shipment": []}})])
    expiring = _jwt(time.time() + 10)
    client = PostiAccountClient(
        session,
        id_token=expiring,
        refresh_token="rt",
        role_tokens=ROLE_TOKENS,
        token_callback=callback,
    )
    new_tokens = {"id_token": _fresh_id_token(), "refresh_token": "rt2", "role_tokens": ROLE_TOKENS}
    refresh = AsyncMock(return_value=new_tokens)
    with patch("custom_components.posti.account.client.auth.async_refresh", new=refresh):
        await client.async_get_shipments()
    callback.assert_awaited_once_with(new_tokens)
    assert refresh.await_args.kwargs["id_token"] == expiring
    assert refresh.await_args.kwargs["refresh_token"] == "rt"


async def test_401_triggers_one_refresh_and_retry():
    session = _session(
        [_resp(401, {}), _resp(200, {"data": {"shipment": [{"shipmentNumber": "S1"}]}})]
    )
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    new_tokens = {"id_token": _fresh_id_token(), "refresh_token": "rt2", "role_tokens": ROLE_TOKENS}
    with patch(
        "custom_components.posti.account.client.auth.async_refresh",
        new=AsyncMock(return_value=new_tokens),
    ):
        shipments = await client.async_get_shipments()
    assert shipments == [{"shipmentNumber": "S1"}]


async def test_second_rejection_raises_reauth_required():
    session = _session([_resp(401, {}), _resp(403, {})])
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    new_tokens = {"id_token": _fresh_id_token(), "refresh_token": "rt2", "role_tokens": ROLE_TOKENS}
    with patch(
        "custom_components.posti.account.client.auth.async_refresh",
        new=AsyncMock(return_value=new_tokens),
    ):
        with pytest.raises(PostiAccountReauthRequired):
            await client.async_get_shipments()


async def test_no_refresh_token_raises_reauth_required():
    client = PostiAccountClient(
        MagicMock(), id_token=None, refresh_token=None, role_tokens=[]
    )
    with pytest.raises(PostiAccountReauthRequired):
        await client.async_get_shipments()


async def test_non_auth_graphql_error_is_an_api_error_not_reauth():
    session = _session([_resp(200, {"data": None, "errors": [{"message": "Cannot query field"}]})])
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    refresh = AsyncMock()
    with (
        patch("custom_components.posti.account.client.auth.async_refresh", new=refresh),
        pytest.raises(PostiAccountApiError) as exc_info,
    ):
        await client.async_get_shipments()
    assert not isinstance(exc_info.value, PostiAccountReauthRequired)
    refresh.assert_not_awaited()


async def test_unauthorized_graphql_error_triggers_refresh_path():
    session = _session(
        [
            _resp(200, {"data": None, "errors": [{"errorType": "UnauthorizedException"}]}),
            _resp(200, {"data": {"shipment": []}}),
        ]
    )
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    new_tokens = {"id_token": _fresh_id_token(), "refresh_token": "rt2", "role_tokens": ROLE_TOKENS}
    with patch(
        "custom_components.posti.account.client.auth.async_refresh",
        new=AsyncMock(return_value=new_tokens),
    ):
        assert await client.async_get_shipments() == []


async def test_missing_consumer_role_token_triggers_unauthorized_path():
    session = _session(
        [_resp(200, {"data": {"shipment": [{"shipmentNumber": "S1"}]}})]
    )
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=[]
    )
    new_tokens = {"id_token": _fresh_id_token(), "refresh_token": "rt2", "role_tokens": ROLE_TOKENS}
    with patch(
        "custom_components.posti.account.client.auth.async_refresh",
        new=AsyncMock(return_value=new_tokens),
    ):
        shipments = await client.async_get_shipments()
    assert shipments == [{"shipmentNumber": "S1"}]


async def test_unparseable_body_raises_api_error():
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(side_effect=ValueError("bad json"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    from custom_components.posti.account.auth import PostiAccountApiError

    with pytest.raises(PostiAccountApiError):
        await client.async_get_shipments()


async def test_non_object_body_raises_api_error():
    session = _session([_resp(200, ["not", "a", "dict"])])
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    from custom_components.posti.account.auth import PostiAccountApiError

    with pytest.raises(PostiAccountApiError):
        await client.async_get_shipments()


async def test_missing_data_without_errors_raises_api_error():
    session = _session([_resp(200, {"data": None})])
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    from custom_components.posti.account.auth import PostiAccountApiError

    with pytest.raises(PostiAccountApiError):
        await client.async_get_shipments()


async def test_generic_error_status_raises_api_error():
    session = _session([_resp(500, {})])
    client = PostiAccountClient(
        session, id_token=_fresh_id_token(), refresh_token="rt", role_tokens=ROLE_TOKENS
    )
    from custom_components.posti.account.auth import PostiAccountApiError

    with pytest.raises(PostiAccountApiError):
        await client.async_get_shipments()
