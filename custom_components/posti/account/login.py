"""Signing in to OmaPosti with strong authentication.

The user is sent to Posti's login page, identifies with a bank or
Mobiilivarmenne, and the code that comes back is traded for tokens. The
identification happens in the user's own browser:
``start_login`` opens the session and lists the methods, ``bank_address`` says
where to send the user, and ``finish_login`` takes the address they ended up on.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import secrets
from typing import Any
import urllib.parse

import aiohttp

from ..const import AUTH_SERVICE_URL, LOGIN_REDIRECT_URI, UAS_HOST
from . import pages
from .errors import (
    PostiAccountApiError,
    PostiAccountInvalidReturnAddress,
    PostiAccountLoginRejected,
)
from .wire import FORM_HEADERS, call_json, fetch_page


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass(frozen=True)
class Pkce:
    """A proof-key pair: the secret verifier and the challenge derived from it."""

    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> Pkce:
        """Create a pair from fresh randomness."""
        verifier = _b64url(secrets.token_bytes(32))
        return cls(verifier, _b64url(hashlib.sha256(verifier.encode()).digest()))


def new_pkce() -> Pkce:
    """Return a fresh PKCE pair."""
    return Pkce.generate()


@dataclass(frozen=True)
class StrongLogin:
    """A login in progress; the session that started it must outlive it."""

    verifier: str
    # Method id -> the link that selects it.
    methods: dict[str, str]


def _form(**fields: str) -> str:
    return urllib.parse.urlencode(fields)


async def start_login(session: aiohttp.ClientSession, locale: str) -> StrongLogin:
    """Open a login and find out which identification methods it offers."""
    pkce = Pkce.generate()
    _, started = await call_json(
        session,
        f"{AUTH_SERVICE_URL}/device_login",
        headers=FORM_HEADERS,
        data={
            "redirect_uri": LOGIN_REDIRECT_URI,
            "locale": locale,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
        },
    )
    login_page = started.get("login_url")
    if not isinstance(login_page, str):
        raise PostiAccountApiError("no login page was given")
    offered = pages.method_links((await fetch_page(session, login_page)).text)
    if not offered:
        raise PostiAccountApiError("the login page lists no identification method")
    return StrongLogin(pkce.verifier, offered)


async def bank_address(
    session: aiohttp.ClientSession, login: StrongLogin, method: str
) -> str:
    """Select a method; return the address where the user identifies."""
    link = login.methods.get(method)
    if link is None:
        raise PostiAccountApiError("that identification method is not offered")
    page = await fetch_page(session, f"https://{UAS_HOST}{link}")
    handover = pages.bank_handover(page.text)
    if handover is None:
        raise PostiAccountApiError("the login page did not lead to the bank")
    return handover


def checked_return_address(address: str) -> str:
    """Accept the pasted address only if it is where Posti's login returns to."""
    address = address.strip()
    parts = urllib.parse.urlsplit(address)
    returns_here = (
        parts.scheme == "https"
        and parts.hostname == UAS_HOST
        and parts.path.startswith("/uas/return/")
        and "code" in urllib.parse.parse_qs(parts.query)
    )
    if not returns_here:
        raise PostiAccountInvalidReturnAddress("not a return address of the login")
    return address


def _authorisation_code(location: str) -> str | None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    return query.get("code", [None])[0]


async def finish_login(
    session: aiohttp.ClientSession, login: StrongLogin, return_address: str
) -> dict[str, Any]:
    """Complete the login from the address the user's browser ended on."""
    landing = await fetch_page(session, checked_return_address(return_address))
    code = pages.hidden_value(landing.text, "code")
    state = pages.hidden_value(landing.text, "state")
    if code is None or state is None:
        # A used or expired address ends on an error page without these.
        raise PostiAccountLoginRejected("the identification was not accepted")

    # The code is in the redirect itself, so the redirect must not be followed.
    callback = (
        f"{AUTH_SERVICE_URL}/device_oidc_callback?"
        f"{urllib.parse.urlencode({'code': code, 'state': state})}"
    )
    authorisation = _authorisation_code(
        (await fetch_page(session, callback, follow=False)).redirect
    )
    if authorisation is None:
        raise PostiAccountLoginRejected("no authorisation code was given")

    _, tokens = await call_json(
        session,
        f"{AUTH_SERVICE_URL}/token_v2",
        headers=FORM_HEADERS,
        data=_form(
            code=authorisation,
            code_verifier=login.verifier,
            grant_type="authorization_code",
        ),
    )
    if "error" in tokens or not tokens.get("id_token"):
        raise PostiAccountLoginRejected("no tokens were given")
    return tokens
