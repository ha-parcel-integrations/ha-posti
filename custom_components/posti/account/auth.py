"""PKCE login, refresh and token-inspection for the OmaPosti account source.

This emulates the OmaPosti Android app's WebView/SAML login sequence — it is
not a public OAuth redirect flow. Everything here is app-emulation, isolated
from the tracking-code source's anonymous transport: no client, token or
normalizer is shared between the two.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from ..const import (
    LOGIN_ENTITY_ID,
    LOGIN_REDIRECT_URI,
    LOGIN_URL,
    REFRESH_URL,
    TOKEN_URL,
    UAS_BASE_URL,
)

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# The app-WebView headers the login page and the credentials form both
# require — not a general browser fingerprint.
_WEBVIEW_HEADERS = {
    "x-posti-mobile": "android",
    "X-Requested-With": "fi.itella.posti.android",
}



class _FormReader(HTMLParser):
    """Collect the first form's ``action`` and its named input values."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        attributes = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._in_form = True
            self.action = attributes.get("action") or None
        elif tag == "input" and self._in_form and attributes.get("name"):
            self.fields[attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False
            self._done = True


class PostiAccountApiError(Exception):
    """An unexpected account API response."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        """Store safe failure metadata without retaining the response body."""
        super().__init__(detail)
        self.status_code = status_code


class PostiAccountInvalidCredentials(PostiAccountApiError):
    """The supplied username/password was rejected."""


class PostiAccountReauthRequired(PostiAccountApiError):
    """Stored tokens cannot be refreshed; the user must sign in again."""


def _new_pkce() -> tuple[str, str]:
    """Return a fresh ``(verifier, challenge)`` PKCE pair."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _query_param(url: str, name: str) -> str | None:
    """Return the first value of query parameter ``name`` in ``url``, if any."""
    values = parse_qs(urlparse(url).query).get(name)
    return values[0] if values else None


def _code_from_urls(urls: list[str]) -> str | None:
    """Return the code of the app's own redirect first, else any code found."""
    for url in urls:
        if url.startswith(LOGIN_REDIRECT_URI):
            code = _query_param(url, "code")
            if code:
                return code
    for url in urls:
        code = _query_param(url, "code")
        if code:
            return code
    return None


def _extract_authorization(
    redirect_locations: list[str], final_url: str, page_text: str
) -> tuple[str, str | None] | None:
    """Find the authorization code wherever the login sequence left it.

    Returns ``(code, callback_url)``. ``callback_url`` is set only when the
    code came on the success page's auto-submitting form: a WebView posts
    that form to the login service's callback, which must see the code
    before it can be exchanged, so the caller visits it the same way.
    ``None`` means no code anywhere — the credentials form came back.
    """
    code = _code_from_urls(redirect_locations + [final_url])
    if code:
        return code, None
    reader = _FormReader()
    reader.feed(page_text)
    code = reader.fields.get("code")
    if not code or not reader.action:
        return None
    return code, f"{reader.action}?{urlencode(reader.fields)}"


async def _json_body(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        body = await response.json(content_type=None)
    except ValueError as err:
        raise PostiAccountApiError(
            f"non-JSON response (HTTP {response.status})", status_code=response.status
        ) from err
    if isinstance(body, dict):
        return body
    raise PostiAccountApiError("response is not a JSON object", status_code=response.status)


def _validate_tokens(tokens: dict[str, Any]) -> dict[str, Any]:
    """Require an ``id_token`` and a ``consumer`` role token, or raise."""
    if not tokens.get("id_token"):
        raise PostiAccountApiError("Posti did not return an id_token")
    role_tokens = tokens.get("role_tokens") or []
    if not any(
        isinstance(item, dict) and item.get("type") == "consumer" and item.get("token")
        for item in role_tokens
    ):
        raise PostiAccountApiError("Posti did not return a consumer role token")
    return {
        "id_token": tokens["id_token"],
        "refresh_token": tokens.get("refresh_token"),
        "role_tokens": role_tokens,
    }


async def async_login(
    session: aiohttp.ClientSession, username: str, password: str
) -> dict[str, Any]:
    """Log in once with the OmaPosti username/password; return the token set.

    The password is used only for this one exchange and is never returned or
    retained — callers persist the returned token set, never the password.
    ``session`` must have a cookie jar of its own: the SAML hop keeps its
    login session in cookies, and a jar shared with an earlier sign-in could
    carry that sign-in's session into this one.
    """
    try:
        return await _async_login(session, username, password)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise PostiAccountApiError(f"Posti could not be reached ({err})") from err


async def _async_login(
    session: aiohttp.ClientSession, username: str, password: str
) -> dict[str, Any]:
    verifier, challenge = _new_pkce()

    async with session.get(
        LOGIN_URL,
        params={
            "redirect_uri": LOGIN_REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect": "false",
            "locale": "en",
            "mobile": "true",
        },
        timeout=_TIMEOUT,
    ) as response:
        if response.status != 200:
            raise PostiAccountApiError(
                f"login request failed (HTTP {response.status})",
                status_code=response.status,
            )
        login_body = await _json_body(response)
    login_page_url = login_body.get("login_url")
    if not login_page_url:
        raise PostiAccountApiError("Posti did not return a login page")

    async with session.get(
        login_page_url, headers=_WEBVIEW_HEADERS, timeout=_TIMEOUT
    ) as response:
        session_id = _query_param(str(response.url), "_id")
        if not session_id:
            for history_response in response.history:
                session_id = _query_param(
                    str(history_response.headers.get("Location", "")), "_id"
                )
                if session_id:
                    break
    if not session_id:
        raise PostiAccountApiError("Posti did not start a login session")

    async with session.post(
        f"{UAS_BASE_URL}/{session_id}/submit",
        params={"entityID": LOGIN_ENTITY_ID, "locale": "en"},
        headers={
            **_WEBVIEW_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=urlencode(
            {"username": username, "password": password, "method": "passwordsql"}
        ),
        timeout=_TIMEOUT,
    ) as response:
        redirect_locations = [
            str(history_response.headers.get("Location", ""))
            for history_response in response.history
        ]
        final_url = str(response.url)
        page_text = await response.text()

    found = _extract_authorization(redirect_locations, final_url, page_text)
    if found is None:
        raise PostiAccountInvalidCredentials("Posti rejected the username or password")
    code, callback_url = found

    if callback_url:
        async with session.get(
            callback_url, headers=_WEBVIEW_HEADERS, timeout=_TIMEOUT
        ) as response:
            code = (
                _code_from_urls(
                    [
                        str(history_response.headers.get("Location", ""))
                        for history_response in response.history
                    ]
                    + [str(response.url)]
                )
                or code
            )

    async with session.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urlencode(
            {"code": code, "code_verifier": verifier, "grant_type": "authorization_code"}
        ),
        timeout=_TIMEOUT,
    ) as response:
        if response.status != 200:
            raise PostiAccountApiError(
                f"token exchange failed (HTTP {response.status})",
                status_code=response.status,
            )
        tokens = await _json_body(response)

    if "error" in tokens:
        raise PostiAccountApiError(f"Posti refused the exchange: {tokens['error']}")
    return _validate_tokens(tokens)


async def async_refresh(
    session: aiohttp.ClientSession,
    *,
    id_token: str | None,
    refresh_token: str,
    previous_role_tokens: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Rotate the token pair via the app's ``refresh_v2`` route.

    Like the app, the (possibly expired) id token goes in ``Authorization``
    and the refresh token in the form body, unencoded. The app never calls
    ``refresh_v2/ack``, so neither does this. Posti may omit role or refresh
    tokens from the response, so the result is merged with the caller's
    previous token set rather than trusting the response to be complete.
    """
    try:
        async with session.post(
            REFRESH_URL,
            headers={
                "Authorization": f"Bearer {id_token or ''}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=f"refresh_token={refresh_token}",
            timeout=_TIMEOUT,
        ) as response:
            if response.status in (401, 422):
                raise PostiAccountReauthRequired(
                    "refresh token rejected", status_code=response.status
                )
            if response.status != 200:
                raise PostiAccountApiError(
                    f"refresh failed (HTTP {response.status})",
                    status_code=response.status,
                )
            refreshed = await _json_body(response)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise PostiAccountApiError(f"Posti could not be reached ({err})") from err

    merged = {
        "id_token": refreshed.get("id_token"),
        "refresh_token": refreshed.get("refresh_token") or refresh_token,
        "role_tokens": refreshed.get("role_tokens") or previous_role_tokens or [],
    }
    if not merged["id_token"]:
        raise PostiAccountReauthRequired("refresh returned no id_token")
    return _validate_tokens(merged)


def token_expires_soon(id_token: str | None, *, margin_seconds: int) -> bool:
    """Whether ``id_token``'s unverified ``exp`` claim is within ``margin_seconds``.

    Reads the JWT payload only — never verifies the signature, and never
    trusts a claim as identity. A token that cannot be parsed at all is
    treated as already expired, so refresh is attempted rather than silently
    skipped.
    """
    if not id_token:
        return True
    try:
        payload_segment = id_token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
        exp = float(payload["exp"])
    except (IndexError, ValueError, TypeError, KeyError, UnicodeDecodeError):
        return True
    return exp - time.time() <= margin_seconds
