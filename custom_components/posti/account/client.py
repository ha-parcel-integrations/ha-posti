"""OmaPosti account GraphQL transport.

Deliberately isolated from the tracking-code source: its own token material,
its own renewal lifecycle, its own field selection. Responses are returned as
raw dictionaries; normalisation belongs in ``parcels.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..const import (
    ACCOUNT_GRAPHQL_URL,
    ACCOUNT_PAGE_SIZE,
    ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS,
    UNAUTHORIZED_ERROR_TYPE,
)
from . import device as device_mod, tokens as tokens_mod
from .errors import PostiAccountApiError, PostiAccountReauthRequired

_TIMEOUT = aiohttp.ClientTimeout(total=20)

TokenCallback = Callable[[dict[str, Any]], Awaitable[None]]

# Only fields the anonymous route already resolves: an unknown field fails the
# whole query, and the account schema cannot be probed without an account.
_SHIPMENTS_QUERY = """
query GetConsumerShipments($page: Int!, $pageSize: Int!, $locale: String) {
  consumerShipments(page: $page, pageSize: $pageSize, locale: $locale) {
    page
    totalPages
    hits {
      shipmentId
      userRole
      displayId
      displayName
      delivery {
        time {
          timestamp
        }
        destination {
          name
          city
        }
      }
      pickupPoint {
        address {
          publicName
          city
        }
      }
      status {
        main
      }
      measurements {
        weight {
          value
        }
      }
      events {
        city
        eventDescription
        timestamp
      }
    }
  }
}
""".strip()


class _Unauthorized(Exception):
    """Internal signal: the id token or the consumer role was refused."""


def consumer_role_token(role_tokens: list[dict[str, Any]]) -> str | None:
    """Return the ``type: consumer`` role token's own token value, if any."""
    return next(
        (
            item.get("token")
            for item in role_tokens
            if isinstance(item, dict)
            and item.get("type") == "consumer"
            and item.get("token")
        ),
        None,
    )


def _graphql_errors(body: Any) -> list[dict[str, Any]]:
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    return [error for error in errors if isinstance(error, dict)]


class PostiAccountClient:
    """Client for the OmaPosti account, renewing its tokens on demand."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        device: device_mod.Device,
        id_token: str | None,
        role_tokens: list[dict[str, Any]] | None,
        token_callback: TokenCallback | None = None,
    ) -> None:
        """Initialise the client with entry-owned tokens and a persistence hook."""
        self._session = session
        self.device = device
        self._id_token = id_token
        self._role_tokens = role_tokens or []
        self._token_callback = token_callback

    @property
    def id_token(self) -> str | None:
        """Return the current id token."""
        return self._id_token

    async def _async_renew(self) -> None:
        """Renew the tokens with the device key and tell the entry owner."""
        fresh = await device_mod.refresh(
            self._session, self.device, str(self._id_token)
        )
        # The answer may leave out role tokens; those stay as they were.
        self._id_token = fresh["id_token"]
        self._role_tokens = fresh.get("role_tokens") or self._role_tokens
        if self._token_callback:
            await self._token_callback(
                {"id_token": self._id_token, "role_tokens": self._role_tokens}
            )

    async def _async_query_page(
        self, page: int, locale: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch one page of shipments, or raise :class:`_Unauthorized`."""
        role_token = consumer_role_token(self._role_tokens)
        if not self._id_token or not role_token:
            raise _Unauthorized

        try:
            async with self._session.post(
                ACCOUNT_GRAPHQL_URL,
                json={
                    "operationName": "GetConsumerShipments",
                    "variables": {
                        "page": page,
                        "pageSize": ACCOUNT_PAGE_SIZE,
                        "locale": locale,
                    },
                    "query": _SHIPMENTS_QUERY,
                },
                # AppSync takes the role token as it is, the id token in a header of its own.
                headers={
                    "Authorization": role_token,
                    "x-posti-token": f"Bearer {self._id_token}",
                },
                timeout=_TIMEOUT,
            ) as response:
                if response.status == 401:
                    raise _Unauthorized
                if response.status != 200:
                    raise PostiAccountApiError(
                        f"account request failed (HTTP {response.status})",
                        status_code=response.status,
                    )
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    raise PostiAccountApiError("account answer was not JSON") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            # A timeout's message is empty, so its type is the cause.
            raise PostiAccountApiError(
                f"account could not be reached: {str(err) or type(err).__name__}"
            ) from err

        data = payload.get("data") if isinstance(payload, dict) else None
        found = data.get("consumerShipments") if isinstance(data, dict) else None
        hits = found.get("hits") if isinstance(found, dict) else None
        if not isinstance(hits, list):
            # An expired id token answers 200 with an Unauthorized error and no
            # shipments; any other error must not push the user into a login.
            errors = _graphql_errors(payload)
            if found is None and any(
                error.get("errorType") == UNAUTHORIZED_ERROR_TYPE for error in errors
            ):
                raise _Unauthorized
            raise PostiAccountApiError(
                "account refused the query" if errors else "account answer unexpected"
            )
        pages = found.get("totalPages") if isinstance(found, dict) else None
        return (
            [hit for hit in hits if isinstance(hit, dict)],
            pages if isinstance(pages, int) and not isinstance(pages, bool) else 1,
        )

    async def _async_query(self, locale: str) -> list[dict[str, Any]]:
        shipments: list[dict[str, Any]] = []
        page, pages = 1, 1
        while page <= pages:
            hits, pages = await self._async_query_page(page, locale)
            shipments.extend(hits)
            page += 1
        return shipments

    async def async_get_shipments(self, locale: str = "en") -> list[dict[str, Any]]:
        """Return the account's raw shipments, received and sent.

        Renews proactively when the id token is close to expiry, and once
        reactively on a refusal. A second refusal raises
        :class:`PostiAccountReauthRequired`, which the coordinator turns into
        Home Assistant's reauth flow.
        """
        if tokens_mod.expires_within(
            self._id_token, ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS
        ):
            await self._async_renew()
        try:
            return await self._async_query(locale)
        except _Unauthorized:
            await self._async_renew()
            try:
                return await self._async_query(locale)
            except _Unauthorized as err:
                raise PostiAccountReauthRequired(
                    "account refused the renewed tokens"
                ) from err
