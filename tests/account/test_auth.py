"""Tests for the OmaPosti PKCE login, refresh and JWT-expiry helpers."""
from __future__ import annotations

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.posti.account import auth
from custom_components.posti.account.auth import (
    PostiAccountApiError,
    PostiAccountInvalidCredentials,
    PostiAccountReauthRequired,
)

USERNAME = "jane.doe"
PASSWORD = "correct horse"


def _resp(status=200, *, json_body=None, url=None, history=None, text=None):
    response = AsyncMock()
    response.status = status
    response.url = url or ""
    response.history = history or []
    if json_body is not None:
        response.json = AsyncMock(return_value=json_body)
    else:
        response.json = AsyncMock(side_effect=ValueError("no json"))
    response.text = AsyncMock(return_value=text or "")
    return response


def _ctx(response):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(get_responses=(), post_responses=()) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=[_ctx(r) for r in get_responses])
    session.post = MagicMock(side_effect=[_ctx(r) for r in post_responses])
    return session


def _redirect(location: str):
    history_step = MagicMock()
    history_step.headers = {"Location": location}
    return history_step


TOKENS = {
    "id_token": "id-token",
    "refresh_token": "refresh-token",
    "role_tokens": [{"type": "consumer", "token": "consumer-token"}],
}


async def test_async_login_happy_path():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[
            _resp(url="https://auth-service.posti.fi/callback?code=AUTHCODE"),
            _resp(json_body=TOKENS),
        ],
    )

    tokens = await auth.async_login(session, USERNAME, PASSWORD)

    assert tokens == TOKENS
    submit_call = session.post.call_args_list[0]
    assert submit_call.kwargs["params"]["entityID"] == auth.LOGIN_ENTITY_ID


async def test_async_login_finds_code_in_redirect_history():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[
            _resp(
                url="https://final.example/success",
                history=[_redirect("https://cb.example/callback?code=FROMREDIRECT")],
            ),
            _resp(json_body=TOKENS),
        ],
    )

    tokens = await auth.async_login(session, USERNAME, PASSWORD)
    assert tokens == TOKENS


async def test_async_login_visits_callback_for_hidden_form_code():
    page = (
        '<form action="https://auth-service.posti.fi/api/v1/oidc_callback">'
        '<input type="hidden" name="code" value="FROM&amp;FORM">'
        '<input type="hidden" name="state" value="STATE1"></form>'
    )
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
            _resp(url=f"{auth.LOGIN_REDIRECT_URI}?code=APPCODE"),
        ],
        post_responses=[
            _resp(url="https://todentaminen.posti.fi/uas/success.jsp", text=page),
            _resp(json_body=TOKENS),
        ],
    )
    tokens = await auth.async_login(session, USERNAME, PASSWORD)

    assert tokens == TOKENS
    callback_url = session.get.call_args_list[2].args[0]
    assert callback_url.startswith("https://auth-service.posti.fi/api/v1/oidc_callback?")
    assert "code=FROM%26FORM" in callback_url
    assert "state=STATE1" in callback_url
    assert "code=APPCODE" in session.post.call_args_list[1].kwargs["data"]


async def test_async_login_keeps_form_code_when_callback_shows_none():
    page = '<form action="https://cb.example/cb"><input name="code" value="FROMFORM"></form>'
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
            _resp(url="https://cb.example/done"),
        ],
        post_responses=[
            _resp(url="https://final.example/success.jsp", text=page),
            _resp(json_body=TOKENS),
        ],
    )
    await auth.async_login(session, USERNAME, PASSWORD)
    assert "code=FROMFORM" in session.post.call_args_list[1].kwargs["data"]


async def test_async_login_prefers_the_app_redirect_code():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[
            _resp(
                url=f"{auth.LOGIN_REDIRECT_URI}?code=APPCODE",
                history=[_redirect("https://auth-service.posti.fi/api/v1/oidc_callback?code=UASCODE")],
            ),
            _resp(json_body=TOKENS),
        ],
    )
    await auth.async_login(session, USERNAME, PASSWORD)
    assert "code=APPCODE" in session.post.call_args_list[1].kwargs["data"]


async def test_async_login_form_without_code_is_invalid_credentials():
    page = '<form action="x"><input name="username" value=""></form>'
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[_resp(url="https://login.example/x?_id=SESSION123", text=page)],
    )
    with pytest.raises(PostiAccountInvalidCredentials):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_network_error_raises_api_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("down"))
    with pytest.raises(PostiAccountApiError):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_session_id_from_redirect_history():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(
                url="https://login.example/x",
                history=[_redirect("https://login.example/y?_id=FROMHISTORY")],
            ),
        ],
        post_responses=[
            _resp(url="https://auth-service.posti.fi/callback?code=AUTHCODE"),
            _resp(json_body=TOKENS),
        ],
    )
    tokens = await auth.async_login(session, USERNAME, PASSWORD)
    assert tokens == TOKENS


async def test_async_login_wrong_credentials_raises_invalid_credentials():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[
            _resp(url="https://login.example/x?_id=SESSION123"),  # no code anywhere
        ],
    )
    with pytest.raises(PostiAccountInvalidCredentials):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_no_login_page_raises():
    session = _session(get_responses=[_resp(json_body={})])
    with pytest.raises(PostiAccountApiError):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_login_request_failure_raises():
    session = _session(get_responses=[_resp(status=500)])
    with pytest.raises(PostiAccountApiError):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_no_session_id_raises():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x"),  # no _id
        ]
    )
    with pytest.raises(PostiAccountApiError):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_token_exchange_failure_raises():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[
            _resp(url="https://auth-service.posti.fi/callback?code=AUTHCODE"),
            _resp(status=500),
        ],
    )
    with pytest.raises(PostiAccountApiError):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_token_error_field_raises():
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[
            _resp(url="https://auth-service.posti.fi/callback?code=AUTHCODE"),
            _resp(json_body={"error": "invalid_grant"}),
        ],
    )
    with pytest.raises(PostiAccountApiError):
        await auth.async_login(session, USERNAME, PASSWORD)


async def test_async_login_missing_consumer_role_token_raises():
    bad_tokens = {"id_token": "id", "refresh_token": "rt", "role_tokens": []}
    session = _session(
        get_responses=[
            _resp(json_body={"login_url": "https://login.example/x"}),
            _resp(url="https://login.example/x?_id=SESSION123"),
        ],
        post_responses=[
            _resp(url="https://auth-service.posti.fi/callback?code=AUTHCODE"),
            _resp(json_body=bad_tokens),
        ],
    )
    with pytest.raises(PostiAccountApiError):
        await auth.async_login(session, USERNAME, PASSWORD)


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_async_refresh_merges_partial_response():
    session = _session(
        post_responses=[_resp(json_body={"id_token": "new-id"})]  # role/refresh omitted
    )
    result = await auth.async_refresh(
        session,
        id_token="old-id",
        refresh_token="old-refresh",
        previous_role_tokens=TOKENS["role_tokens"],
    )
    assert result["id_token"] == "new-id"
    assert result["refresh_token"] == "old-refresh"
    assert result["role_tokens"] == TOKENS["role_tokens"]


async def test_async_refresh_sends_id_token_as_bearer_and_never_acks():
    session = _session(post_responses=[_resp(json_body=TOKENS)])
    await auth.async_refresh(
        session, id_token="old-id", refresh_token="old-refresh", previous_role_tokens=None
    )
    assert session.post.call_count == 1
    call = session.post.call_args_list[0]
    assert call.args[0] == auth.REFRESH_URL
    assert call.kwargs["headers"]["Authorization"] == "Bearer old-id"
    assert call.kwargs["data"] == "refresh_token=old-refresh"


async def test_async_refresh_network_error_raises_api_error():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("down"))
    with pytest.raises(PostiAccountApiError) as exc_info:
        await auth.async_refresh(
            session, id_token="id", refresh_token="rt", previous_role_tokens=None
        )
    assert not isinstance(exc_info.value, PostiAccountReauthRequired)


async def test_async_refresh_rejected_raises_reauth_required():
    session = _session(post_responses=[_resp(status=401)])
    with pytest.raises(PostiAccountReauthRequired):
        await auth.async_refresh(
            session, id_token="id", refresh_token="bad", previous_role_tokens=None
        )


async def test_async_refresh_422_also_raises_reauth_required():
    session = _session(post_responses=[_resp(status=422)])
    with pytest.raises(PostiAccountReauthRequired):
        await auth.async_refresh(
            session, id_token="id", refresh_token="bad", previous_role_tokens=None
        )


async def test_async_refresh_no_id_token_raises_reauth_required():
    session = _session(post_responses=[_resp(json_body={})])
    with pytest.raises(PostiAccountReauthRequired):
        await auth.async_refresh(
            session, id_token="id", refresh_token="old", previous_role_tokens=None
        )


async def test_async_refresh_other_status_raises_api_error():
    session = _session(post_responses=[_resp(status=418)])
    with pytest.raises(PostiAccountApiError):
        await auth.async_refresh(
            session, id_token="id", refresh_token="old", previous_role_tokens=None
        )


# ---------------------------------------------------------------------------
# token_expires_soon
# ---------------------------------------------------------------------------


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_token_expires_soon_true_when_close_to_expiry():
    token = _jwt(time.time() + 60)
    assert auth.token_expires_soon(token, margin_seconds=300) is True


def test_token_expires_soon_false_when_far_from_expiry():
    token = _jwt(time.time() + 3600)
    assert auth.token_expires_soon(token, margin_seconds=300) is False


def test_token_expires_soon_true_for_missing_token():
    assert auth.token_expires_soon(None, margin_seconds=300) is True


def test_token_expires_soon_true_for_unparseable_token():
    assert auth.token_expires_soon("not-a-jwt", margin_seconds=300) is True
