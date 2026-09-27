"""OmaPosti account GraphQL transport.

Deliberately isolated from the tracking-code source: its own token material,
its own retry/refresh lifecycle, its own minimal field selection. Responses
are returned as raw dictionaries; normalisation belongs in ``parcels.py``.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..const import ACCOUNT_GRAPHQL_URL, ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS
from . import auth
from .auth import PostiAccountApiError, PostiAccountReauthRequired

_TIMEOUT = aiohttp.ClientTimeout(total=20)

TokenCallback = Callable[[dict[str, Any]], Awaitable[None]]

_SHIPMENTS_QUERY = """
query GetShipments {
  shipment {
    shipmentNumber
    parties {
      name
      role
    }
    departure {
      city
    }
    destination {
      city
    }
    trackingNumbers
    events {
      timestamp
      eventDescription {
        lang
        value
      }
      eventLocation {
        city
        country
      }
    }
    shipmentPhase
    savedDateTime
    estimatedDeliveryTime
    grossWeight
    packageQuantity
    pickupPoint {
      type
      lockerAddress
      lockerCode
      pupCode
      availabilityTime
      location {
        street1
        postCode
        city
      }
    }
  }
}
""".strip()


class _Unauthorized(Exception):
    """Internal signal: the current id token/consumer role was rejected."""


def _consumer_role_token(role_tokens: list[dict[str, Any]]) -> str | None:
    """Return the ``type: consumer`` role token's own token value, if any."""
    return next(
        (
            item.get("token")
            for item in role_tokens
            if isinstance(item, dict) and item.get("type") == "consumer" and item.get("token")
        ),
        None,
    )


class PostiAccountClient:
    """Client for the OmaPosti account inbox, with token-refresh-on-demand."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        id_token: str | None,
        refresh_token: str | None,
        role_tokens: list[dict[str, Any]] | None,
        token_callback: TokenCallback | None = None,
    ) -> None:
        """Initialise the client with entry-owned tokens and a persistence hook."""
        self._session = session
        self._id_token = id_token
        self._refresh_token = refresh_token
        self._role_tokens = role_tokens or []
        self._token_callback = token_callback

    async def _async_refresh(self) -> None:
        """Rotate the stored token set and notify the entry owner."""
        if not self._refresh_token:
            raise PostiAccountReauthRequired("no refresh token stored")
        tokens = await auth.async_refresh(
            self._session,
            id_token=self._id_token,
            refresh_token=self._refresh_token,
            previous_role_tokens=self._role_tokens,
        )
        self._id_token = tokens["id_token"]
        self._refresh_token = tokens["refresh_token"]
        self._role_tokens = tokens["role_tokens"]
        if self._token_callback:
            await self._token_callback(tokens)

    async def _async_ensure_fresh_token(self) -> None:
        """Refresh proactively when the id token is close to expiry."""
        if auth.token_expires_soon(
            self._id_token, margin_seconds=ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS
        ):
            await self._async_refresh()

    async def _async_query(self) -> list[dict[str, Any]]:
        """Run ``GetShipments`` once, or raise :class:`_Unauthorized`."""
        role_token = _consumer_role_token(self._role_tokens)
        if not self._id_token or not role_token:
            raise _Unauthorized()

        async with self._session.post(
            ACCOUNT_GRAPHQL_URL,
            json={"operationName": "GetShipments", "variables": {}, "query": _SHIPMENTS_QUERY},
            headers={
                "Authorization": f"Bearer {self._id_token}",
                "X-Omaposti-Roles": role_token,
            },
            timeout=_TIMEOUT,
        ) as response:
            if response.status in (401, 403):
                raise _Unauthorized()
            if response.status != 200:
                raise PostiAccountApiError(
                    f"account inbox request failed (HTTP {response.status})",
                    status_code=response.status,
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise PostiAccountApiError(
                    f"account inbox returned unparseable body ({err})"
                ) from err

        if not isinstance(payload, dict):
            raise PostiAccountApiError("account inbox returned no body")
        data = payload.get("data")
        if not isinstance(data, dict):
            errors = payload.get("errors") or []
            # Only an auth-typed error may trigger refresh/reauth — a schema
            # or resolver error must not push the user into signing in again.
            if any(
                isinstance(error, dict)
                and str(error.get("errorType", "")).startswith("Unauthorized")
                for error in errors
            ):
                raise _Unauthorized()
            raise PostiAccountApiError(
                "account inbox returned errors" if errors else "account inbox returned no data"
            )
        shipments = data.get("shipment")
        if shipments is None:
            return []
        if not isinstance(shipments, list):
            raise PostiAccountApiError("account inbox returned an unexpected shipment list")
        return [item for item in shipments if isinstance(item, dict)]

    async def async_get_shipments(self) -> list[dict[str, Any]]:
        """Return the account's raw shipment list.

        Refreshes proactively when the id token is close to expiry, and once
        reactively on a rejected read. A second rejection — or a refresh that
        itself fails — raises :class:`PostiAccountReauthRequired`, which the
        coordinator turns into HA's reauth flow.
        """
        await self._async_ensure_fresh_token()
        try:
            return await self._async_query()
        except _Unauthorized:
            await self._async_refresh()
            try:
                return await self._async_query()
            except _Unauthorized as err:
                raise PostiAccountReauthRequired(
                    "account inbox rejected the refreshed token"
                ) from err
