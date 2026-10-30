"""Tests for the OmaPosti account GraphQL client."""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.posti.account.client import PostiAccountClient
from custom_components.posti.account.device import Device
from custom_components.posti.account.errors import (
    PostiAccountApiError,
    PostiAccountReauthRequired,
)

ROLE_TOKENS = [{"type": "consumer", "token": "consumer-token"}]
DEVICE = Device("dev-1", "key", "salt", "sub-1")


def _jwt(exp: float) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    )
    return f"header.{payload}.signature"


def _fresh() -> str:
    return _jwt(time.time() + 3600)


def _resp(status=200, body=None):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    return response


def _page(hits, total_pages=1):
    return {"data": {"consumerShipments": {"hits": hits, "totalPages": total_pages}}}


UNAUTHORIZED = {
    "data": {"consumerShipments": None},
    "errors": [{"errorType": "Unauthorized", "message": "nope"}],
}


def _session(responses):
    session = MagicMock()

    def _ctx(response):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session.post = MagicMock(side_effect=[_ctx(r) for r in responses])
    return session


def _client(session, id_token=None, callback=None):
    return PostiAccountClient(
        session,
        device=DEVICE,
        id_token=id_token or _fresh(),
        role_tokens=ROLE_TOKENS,
        token_callback=callback,
    )


def _renewal(new_token=None):
    return patch(
        "custom_components.posti.account.client.device_mod.refresh",
        new=AsyncMock(return_value={"id_token": new_token or _fresh()}),
    )


async def test_returns_shipments_and_sends_both_headers():
    session = _session([_resp(200, _page([{"shipmentId": "S1"}]))])
    client = _client(session, id_token="x.y.z")
    with _renewal():
        shipments = await client.async_get_shipments()
    assert shipments == [{"shipmentId": "S1"}]
    headers = session.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "consumer-token"
    assert headers["x-posti-token"].startswith("Bearer ")
    variables = session.post.call_args.kwargs["json"]["variables"]
    assert variables["pageSize"] == 1000


async def test_pages_are_followed_to_the_last():
    session = _session(
        [
            _resp(200, _page([{"shipmentId": "S1"}], total_pages=2)),
            _resp(200, _page([{"shipmentId": "S2"}], total_pages=2)),
        ]
    )
    shipments = await _client(session).async_get_shipments()
    assert [s["shipmentId"] for s in shipments] == ["S1", "S2"]
    assert session.post.call_args.kwargs["json"]["variables"]["page"] == 2


async def test_expiring_id_token_is_renewed_before_the_query():
    session = _session([_resp(200, _page([]))])
    stored = []

    async def callback(tokens):
        stored.append(tokens)

    client = _client(session, id_token=_jwt(time.time() + 10), callback=callback)
    with _renewal("new-token") as renewal:
        await client.async_get_shipments()
    renewal.assert_awaited_once()
    assert stored == [{"id_token": "new-token", "role_tokens": ROLE_TOKENS}]
    assert (
        session.post.call_args.kwargs["headers"]["x-posti-token"] == "Bearer new-token"
    )


@pytest.mark.parametrize("refusal", [_resp(401), _resp(200, UNAUTHORIZED)])
async def test_a_refusal_renews_once_and_retries(refusal):
    session = _session([refusal, _resp(200, _page([{"shipmentId": "S1"}]))])
    with _renewal() as renewal:
        shipments = await _client(session).async_get_shipments()
    renewal.assert_awaited_once()
    assert shipments == [{"shipmentId": "S1"}]


async def test_a_second_refusal_needs_a_new_login():
    session = _session([_resp(401), _resp(401)])
    with _renewal(), pytest.raises(PostiAccountReauthRequired):
        await _client(session).async_get_shipments()


async def test_an_error_beside_shipments_is_no_refusal():
    body = _page([{"shipmentId": "S1"}]) | {
        "errors": [{"errorType": "Unauthorized", "message": "one field"}]
    }
    session = _session([_resp(200, body)])
    with _renewal() as renewal:
        shipments = await _client(session).async_get_shipments()
    renewal.assert_not_awaited()
    assert shipments == [{"shipmentId": "S1"}]


async def test_other_graphql_errors_are_an_api_error_not_a_login():
    body = {"data": None, "errors": [{"errorType": "FieldUndefined", "message": "x"}]}
    session = _session([_resp(200, body)])
    with _renewal() as renewal, pytest.raises(PostiAccountApiError) as err:
        await _client(session).async_get_shipments()
    renewal.assert_not_awaited()
    assert not isinstance(err.value, PostiAccountReauthRequired)


async def test_server_error_is_an_api_error():
    session = _session([_resp(502)])
    with pytest.raises(PostiAccountApiError) as err:
        await _client(session).async_get_shipments()
    assert err.value.status_code == 502


async def test_a_timeout_names_its_type():
    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=TimeoutError())
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=ctx)
    with pytest.raises(PostiAccountApiError, match="TimeoutError"):
        await _client(session).async_get_shipments()


async def test_missing_consumer_role_needs_a_new_login():
    session = _session([])
    client = PostiAccountClient(
        session, device=DEVICE, id_token=_fresh(), role_tokens=[]
    )
    with _renewal(), pytest.raises(PostiAccountReauthRequired):
        await client.async_get_shipments()
