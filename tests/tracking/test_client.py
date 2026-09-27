"""Tests for the anonymous-token GraphQL tracking transport."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.posti.tracking.client import (
    PostiTrackingApiError,
    PostiTrackingClient,
)

from .payloads import TRACKING_CODE, delivered_hit

TOKEN_BODY = {
    "id_token": "id-token-1",
    "role_tokens": [{"type": "anonymous", "token": "auth-token-1"}],
}


def _response(status: int, body=None, *, json_error: bool = False, headers=None):
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if json_error:
        response.json = AsyncMock(side_effect=ValueError("bad json"))
    else:
        response.json = AsyncMock(return_value=body)
    return response


def _ctx(response):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(post_responses: list) -> MagicMock:
    """A session whose ``post`` returns each context manager in sequence."""
    session = MagicMock()
    session.post = MagicMock(side_effect=[_ctx(r) for r in post_responses])
    return session


def _search_body(hit=None, *, total_hits=None, errors=None):
    if total_hits is None:
        total_hits = 1 if hit else 0
    return {
        "data": {
            "consumerSearchShipments": {
                "totalHits": total_hits,
                "hits": [hit] if hit else [],
            }
        },
        "errors": errors,
    }


async def test_mints_a_token_then_searches():
    session = _session([_response(200, TOKEN_BODY), _response(200, _search_body(delivered_hit()))])
    client = PostiTrackingClient(session)

    hit = await client.async_get_parcel(TRACKING_CODE)

    assert hit["status"]["main"] == "DELIVERED"
    token_call = session.post.call_args_list[0]
    assert token_call.args[0].endswith("anonymous_token")
    search_call = session.post.call_args_list[1]
    assert search_call.kwargs["headers"]["Authorization"] == "auth-token-1"
    assert search_call.kwargs["headers"]["X-Posti-Token"] == "Bearer id-token-1"


async def test_reuses_the_token_across_calls_within_one_reset_cycle():
    session = _session(
        [
            _response(200, TOKEN_BODY),
            _response(200, _search_body(delivered_hit())),
            _response(200, _search_body(None)),
        ]
    )
    client = PostiTrackingClient(session)
    await client.async_get_parcel(TRACKING_CODE)
    await client.async_get_parcel("OTHERCODE")
    assert session.post.call_count == 3  # one mint, two searches


async def test_reset_token_forces_a_fresh_mint():
    session = _session(
        [
            _response(200, TOKEN_BODY),
            _response(200, _search_body(delivered_hit())),
            _response(200, TOKEN_BODY),
            _response(200, _search_body(delivered_hit())),
        ]
    )
    client = PostiTrackingClient(session)
    await client.async_get_parcel(TRACKING_CODE)
    client.reset_token()
    await client.async_get_parcel(TRACKING_CODE)
    assert session.post.call_count == 4


async def test_zero_hits_is_not_found():
    session = _session([_response(200, TOKEN_BODY), _response(200, _search_body(None))])
    client = PostiTrackingClient(session)
    assert await client.async_get_parcel(TRACKING_CODE) is None


async def test_bare_401_triggers_one_remint_and_retry():
    session = _session(
        [
            _response(200, TOKEN_BODY),
            _response(401, json_error=True),
            _response(200, {**TOKEN_BODY, "id_token": "id-token-2"}),
            _response(200, _search_body(delivered_hit())),
        ]
    )
    client = PostiTrackingClient(session)
    hit = await client.async_get_parcel(TRACKING_CODE)
    assert hit is not None
    assert session.post.call_count == 4


async def test_unauthorized_graphql_error_triggers_one_remint_and_retry():
    session = _session(
        [
            _response(200, TOKEN_BODY),
            _response(
                200,
                {"data": None, "errors": [{"errorType": "Unauthorized"}]},
            ),
            _response(200, TOKEN_BODY),
            _response(200, _search_body(delivered_hit())),
        ]
    )
    client = PostiTrackingClient(session)
    hit = await client.async_get_parcel(TRACKING_CODE)
    assert hit is not None


async def test_second_rejection_raises():
    session = _session(
        [
            _response(200, TOKEN_BODY),
            _response(401, json_error=True),
            _response(200, TOKEN_BODY),
            _response(401, json_error=True),
        ]
    )
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_resolver_failure_raises_without_errors():
    session = _session([_response(200, TOKEN_BODY), _response(200, {"data": None})])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_429_raises_with_retry_after():
    session = _session(
        [
            _response(200, TOKEN_BODY),
            _response(429, headers={"Retry-After": "30"}),
        ]
    )
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError) as err:
        await client.async_get_parcel(TRACKING_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 30


async def test_429_without_parseable_retry_after():
    session = _session(
        [
            _response(200, TOKEN_BODY),
            _response(429, headers={"Retry-After": "not-a-number"}),
        ]
    )
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError) as err:
        await client.async_get_parcel(TRACKING_CODE)
    assert err.value.retry_after is None


async def test_other_non_200_status_raises():
    session = _session([_response(200, TOKEN_BODY), _response(500, {})])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_unparseable_search_body_raises():
    session = _session([_response(200, TOKEN_BODY), _response(200, json_error=True)])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_non_object_search_body_raises():
    session = _session([_response(200, TOKEN_BODY), _response(200, ["not", "a", "dict"])])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_missing_consumer_search_shipments_key_raises():
    session = _session([_response(200, TOKEN_BODY), _response(200, {"data": {}})])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_token_mint_failure_raises():
    session = _session([_response(500, {})])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_token_mint_unparseable_body_raises():
    session = _session([_response(200, json_error=True)])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_token_mint_missing_fields_raises():
    session = _session([_response(200, {"id_token": None, "role_tokens": []})])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_token_mint_non_object_body_raises():
    session = _session([_response(200, ["not", "a", "dict"])])
    client = PostiTrackingClient(session)
    with pytest.raises(PostiTrackingApiError):
        await client.async_get_parcel(TRACKING_CODE)


async def test_network_error_propagates():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = PostiTrackingClient(session)
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel(TRACKING_CODE)
