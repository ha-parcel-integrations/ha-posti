"""Tests for the OmaPosti strong-authentication login."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.posti.account import login
from custom_components.posti.account.errors import (
    PostiAccountApiError,
    PostiAccountInvalidReturnAddress,
    PostiAccountLoginRejected,
)
from custom_components.posti.account.wire import Page

METHOD_ID = "telia.ftn.oidc.op.1"
SELECT = "/uas/authn/abc/select?method=telia.ftn.oidc.op.1"
LOGIN_PAGE = (
    f"<a href='{SELECT}'>OP</a>"
    '<a href="/uas/authn/abc/select?method=telia.ftn.oidc.mobileid.1">Mobile</a>'
    "<a href='/uas/authn/abc/select?method=telia.ftn.oidc.op.1'>dup</a>"
    "<a href='/other'>x</a>"
)
BANK_PAGE = (
    "<form method='post' action='https://tunnistus.telia.fi/idp/auth?a=1&amp;b=2'>"
    "<input type='hidden' name='request' value='sig+nature=='/></form>"
)
RETURN = "https://todentaminen.posti.fi/uas/return/abc?code=one&state=two"
RETURN_PAGE = (
    "<input type='hidden' name='code' value='c&amp;1'/>"
    "<input type='hidden' name='state' value='s1'/>"
)


def test_pkce_challenge_matches_the_verifier():
    pkce = login.new_pkce()
    digest = hashlib.sha256(pkce.verifier.encode()).digest()
    assert pkce.challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert login.new_pkce().verifier != pkce.verifier


@pytest.mark.parametrize(
    "address",
    [
        "http://todentaminen.posti.fi/uas/return/abc?code=x",
        "https://evil.example/uas/return/abc?code=x",
        "https://todentaminen.posti.fi.evil.example/uas/return/abc?code=x",
        "https://todentaminen.posti.fi/uas/authn/abc?code=x",
        "https://todentaminen.posti.fi/uas/return/abc?state=x",
        "not an address",
        "",
    ],
)
def test_a_wrong_return_address_is_refused(address):
    with pytest.raises(PostiAccountInvalidReturnAddress):
        login.checked_return_address(address)


def test_a_return_address_is_trimmed():
    assert login.checked_return_address(f"  {RETURN}\n") == RETURN


async def test_start_login_posts_the_challenge_and_reads_the_methods():
    calls = []

    async def fake_json(session, url, **kwargs):
        calls.append((url, kwargs))
        return 200, {"login_url": "https://todentaminen.posti.fi/uas/authn/abc"}

    with (
        patch.object(login, "call_json", fake_json),
        patch.object(login, "fetch_page", AsyncMock(return_value=Page("", LOGIN_PAGE))),
    ):
        started = await login.start_login(MagicMock(), "fi")

    url, kwargs = calls[0]
    assert url == f"{login.AUTH_SERVICE_URL}/device_login"
    assert kwargs["data"]["code_challenge_method"] == "S256"
    assert kwargs["data"]["locale"] == "fi"
    assert kwargs["data"]["redirect_uri"] == "https://oma.posti.fi/app/login"
    assert "telia.ftn.oidc.op.1" in started.methods
    assert started.verifier


async def test_start_login_without_a_login_page_fails():
    with (
        patch.object(login, "call_json", AsyncMock(return_value=(200, {}))),
        pytest.raises(PostiAccountApiError),
    ):
        await login.start_login(MagicMock(), "fi")


async def test_start_login_without_methods_fails():
    with (
        patch.object(
            login,
            "call_json",
            AsyncMock(return_value=(200, {"login_url": "https://x"})),
        ),
        patch.object(login, "fetch_page", AsyncMock(return_value=Page("", "<html/>"))),
        pytest.raises(PostiAccountApiError),
    ):
        await login.start_login(MagicMock(), "fi")


async def test_a_method_that_was_not_offered_is_refused():
    started = login.StrongLogin("v", {"a": "/x"})
    with pytest.raises(PostiAccountApiError):
        await login.bank_address(MagicMock(), started, "b")


async def test_finish_login_follows_the_callback_without_redirects():
    seen = []

    async def fake_page(session, url, follow=True):
        seen.append((url, follow))
        if len(seen) == 1:
            return Page("", RETURN_PAGE)
        return Page("https://oma.posti.fi/app/login?code=final%2Bcode", "")

    token_call = {}

    async def fake_json(session, url, **kwargs):
        token_call.update(url=url, **kwargs)
        return 200, {"id_token": "id", "refresh_token": "rt", "role_tokens": []}

    with (
        patch.object(login, "fetch_page", fake_page),
        patch.object(login, "call_json", fake_json),
    ):
        tokens = await login.finish_login(
            MagicMock(), login.StrongLogin("verifier", {}), RETURN
        )

    assert tokens["id_token"] == "id"
    assert seen[0] == (RETURN, True)
    callback, follow = seen[1]
    assert follow is False
    assert callback.startswith(
        f"{login.AUTH_SERVICE_URL}/device_oidc_callback?code=c%261&state=s1"
    )
    assert token_call["url"].endswith("/token_v2")
    assert "code=final%2Bcode" in token_call["data"]
    assert "code_verifier=verifier" in token_call["data"]
    assert "grant_type=authorization_code" in token_call["data"]


async def test_an_error_page_is_a_rejected_authentication():
    with (
        patch.object(
            login, "fetch_page", AsyncMock(return_value=Page("", "<html>error</html>"))
        ),
        pytest.raises(PostiAccountLoginRejected),
    ):
        await login.finish_login(MagicMock(), login.StrongLogin("v", {}), RETURN)


async def test_a_callback_without_a_code_is_a_rejected_authentication():
    fetched = AsyncMock(
        side_effect=[Page("", RETURN_PAGE), Page("https://oma.posti.fi/x", "")]
    )
    with (
        patch.object(login, "fetch_page", fetched),
        pytest.raises(PostiAccountLoginRejected),
    ):
        await login.finish_login(MagicMock(), login.StrongLogin("v", {}), RETURN)


async def test_a_token_error_is_a_rejected_authentication():
    fetched = AsyncMock(
        side_effect=[
            Page("", RETURN_PAGE),
            Page("https://oma.posti.fi/app/login?code=z", ""),
        ]
    )
    with (
        patch.object(login, "fetch_page", fetched),
        patch.object(
            login,
            "call_json",
            AsyncMock(return_value=(400, {"error": "invalid_grant"})),
        ),
        pytest.raises(PostiAccountLoginRejected),
    ):
        await login.finish_login(MagicMock(), login.StrongLogin("v", {}), RETURN)


async def test_bank_address_reads_the_page_of_the_chosen_method():
    started = login.StrongLogin("v", {METHOD_ID: "/uas/authn/abc/select?method=m"})
    with patch.object(
        login, "fetch_page", AsyncMock(return_value=Page("", BANK_PAGE))
    ) as page:
        address = await login.bank_address(MagicMock(), started, METHOD_ID)
    assert (
        page.call_args.args[1]
        == "https://todentaminen.posti.fi/uas/authn/abc/select?method=m"
    )
    assert address.startswith("https://tunnistus.telia.fi/")


async def test_bank_address_without_a_hand_over_fails():
    started = login.StrongLogin("v", {METHOD_ID: "/x"})
    with (
        patch.object(login, "fetch_page", AsyncMock(return_value=Page("", "<html/>"))),
        pytest.raises(PostiAccountApiError),
    ):
        await login.bank_address(MagicMock(), started, METHOD_ID)
