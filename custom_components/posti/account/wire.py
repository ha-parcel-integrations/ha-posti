"""HTTP plumbing shared by the login and the device exchange."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

import aiohttp
from yarl import URL

from ..const import APP_PACKAGE, APP_USER_AGENT, WEB_VIEW_USER_AGENT
from .errors import PostiAccountApiError, PostiAccountNotJson

TIMEOUT = aiohttp.ClientTimeout(total=20)

# What an in-app web view sends. Posti ties the authorisation code to
# the PKCE challenge only for requests that look like they come from it.
BROWSER_HEADERS = {
    "User-Agent": WEB_VIEW_USER_AGENT,
    "X-Requested-With": APP_PACKAGE,
    "x-posti-mobile": "android",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
FORM_HEADERS = {
    "User-Agent": APP_USER_AGENT,
    "Content-Type": "application/x-www-form-urlencoded",
}


class Page(NamedTuple):
    """A fetched page: where it redirected to (if it did) and its text."""

    redirect: str
    text: str


def unreachable(err: BaseException) -> PostiAccountApiError:
    """Describe a transport failure; a timeout carries no message of its own."""
    return PostiAccountApiError(f"not reachable: {str(err) or type(err).__name__}")


async def fetch_page(
    session: aiohttp.ClientSession, address: str, *, follow: bool = True
) -> Page:
    """GET a login page with the web-view headers."""
    try:
        # The addresses carry a signed request, so they must not be re-encoded.
        async with session.get(
            URL(address, encoded=True),
            headers=BROWSER_HEADERS,
            allow_redirects=follow,
            timeout=TIMEOUT,
        ) as reply:
            if reply.status >= 400:
                raise PostiAccountApiError(
                    f"login page answered {reply.status}", status_code=reply.status
                )
            return Page(reply.headers.get("Location", ""), await reply.text())
    except (aiohttp.ClientError, TimeoutError) as err:
        raise unreachable(err) from err


async def call_json(
    session: aiohttp.ClientSession, address: str, **options: Any
) -> tuple[int, dict[str, Any]]:
    """POST to a Posti service and return the status and the JSON object."""
    try:
        async with session.post(address, timeout=TIMEOUT, **options) as reply:
            status = reply.status
            try:
                body = await reply.json(content_type=None)
            except ValueError as err:
                raise PostiAccountNotJson(
                    f"reply was not JSON (status {status})", status_code=status
                ) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise unreachable(err) from err
    if not isinstance(body, dict):
        raise PostiAccountApiError(
            f"reply had an unexpected shape (status {status})", status_code=status
        )
    return status, body


def error_code(body: Mapping[str, Any]) -> Any:
    """Pull the error out of a reply, whichever of Posti's shapes it uses."""
    error = body.get("error")
    if isinstance(error, Mapping):
        # The device service wraps it: {"error": {"code": 403, "message": ...}}.
        return error.get("message") or error.get("code")
    return error or body.get("message") or body.get("code")
