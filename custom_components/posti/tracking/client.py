"""Anonymous-token GraphQL transport for the Posti tracking-code source.

Two keyless requests per lookup batch: an anonymous token mint, then a
GraphQL search by tracking code. The token pair is throwaway session
material — minted fresh at the start of every coordinator refresh, kept in
memory only for that refresh, and never written to entry data, options,
diagnostics, fixtures or logs.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..const import TRACKING_ANONYMOUS_TOKEN_URL, TRACKING_GRAPHQL_URL

_LOGGER = logging.getLogger(__name__)

_SEARCH_QUERY = """
query ConsumerSearchShipments($code: String!) {
  consumerSearchShipments(
    page: 1
    pageSize: 1
    type: PUBLIC_SHIPMENTS
    searchTerms: [$code]
    locale: "en"
  ) {
    totalHits
    hits {
      shipmentId
      shipmentType
      userRole
      displayId
      displayName
      references { type reference }
      delivery {
        method
        signatureRequired
        result
        information { arrivalAnnouncement }
        time { type timestamp timestampLatest }
        destination { name street postcode city }
      }
      pickupPoint {
        type
        lastCollectionDate
        codPayableOnLocation
        status
        pupCode
        pickupDetails {
          identification
          pickupMethod
          pinCode
          lockerId
          shelfId
          itemId
        }
        address {
          streetAddress
          specificLocation
          postcode
          city
          publicName
        }
        openingHours {
          closed
          open24h
          opens
          closes
          dayOfWeek
          validFrom
          validThrough
        }
        exceptions {
          closed
          open24h
          opens
          closes
          validFrom
          validThrough
        }
      }
      dropOffPoint {
        type
        pupCode
        address {
          streetAddress
          specificLocation
          postcode
          city
          publicName
        }
        openingHours {
          closed
          open24h
          opens
          closes
          dayOfWeek
          validFrom
          validThrough
        }
        exceptions {
          closed
          open24h
          opens
          closes
          validFrom
          validThrough
        }
        sendingDetails {
          method
          pinCode
          lockerId
          errandCode
          reservationExpiration { earliest latest }
        }
      }
      payments { type amount currency paid }
      status {
        account
        activeServices { type }
        main
        subStatus
        exception
        redirectionReason
      }
      callToAction { type url actionTarget }
      additionalActions { type url actionTarget }
      measurements {
        size
        weight { unit value }
        height { unit value }
        width { unit value }
        length { unit value }
        volume { unit value }
        packageQuantity { unit value }
        loadingMeters { unit value }
        freightWeight { unit value }
      }
      events {
        city
        eventDescription
        reasonDescription
        timestamp
      }
      packages {
        trackingNumber
        events {
          city
          eventDescription
          reasonDescription
          timestamp
        }
      }
    }
  }
}
""".strip()


class PostiTrackingApiError(Exception):
    """Raised when the tracking transport returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Posti tracking request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class _AuthExpired(Exception):
    """Internal signal: the in-memory token pair was rejected.

    Never raised across the client boundary — ``async_get_parcel`` catches
    this, mints a fresh pair and retries the read exactly once. Posti signals
    a rejected token two different ways: a bare non-JSON 401 (missing
    ``Authorization``) or a ``200`` whose ``data`` is ``null`` with
    ``errors[0].errorType == "Unauthorized"`` (missing/expired
    ``X-Posti-Token``) — both are handled here.
    """


class PostiTrackingClient:
    """Client for the public, keyless Posti consumer tracking route."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session
        self._authorization: str | None = None
        self._id_token: str | None = None

    def reset_token(self) -> None:
        """Discard the in-memory token pair.

        Called once at the start of every coordinator refresh — the anonymous
        token lives an hour, so a per-refresh mint is always well inside its
        lifetime and no cross-refresh caching is needed or wanted.
        """
        self._authorization = None
        self._id_token = None

    async def _async_mint_token(self) -> None:
        """Mint a fresh anonymous token pair."""
        async with self._session.post(
            TRACKING_ANONYMOUS_TOKEN_URL, json={}
        ) as response:
            if response.status != 200:
                raise PostiTrackingApiError(
                    f"anonymous token mint failed (HTTP {response.status})",
                    status_code=response.status,
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise PostiTrackingApiError(
                    f"anonymous token mint returned unparseable body ({err})"
                ) from err

        if not isinstance(payload, dict):
            raise PostiTrackingApiError("anonymous token mint returned no body")
        id_token = payload.get("id_token")
        role_tokens = payload.get("role_tokens") or []
        anonymous = next(
            (
                item
                for item in role_tokens
                if isinstance(item, dict) and item.get("type") == "anonymous"
            ),
            None,
        )
        if not id_token or not anonymous or not anonymous.get("token"):
            raise PostiTrackingApiError("anonymous token mint returned no usable token")
        self._id_token = id_token
        self._authorization = anonymous["token"]

    async def _ensure_token(self) -> None:
        if self._authorization is None or self._id_token is None:
            await self._async_mint_token()

    async def _async_search(self, tracking_code: str) -> dict[str, Any] | None:
        """Run one GraphQL search, or raise :class:`_AuthExpired`."""
        headers = {
            "Authorization": self._authorization or "",
            "X-Posti-Token": f"Bearer {self._id_token}",
        }
        async with self._session.post(
            TRACKING_GRAPHQL_URL,
            json={"query": _SEARCH_QUERY, "variables": {"code": tracking_code}},
            headers=headers,
        ) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None
                raise PostiTrackingApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                # A missing/expired X-Posti-Token, alone, answers a bare
                # non-JSON 401.
                if response.status == 401:
                    raise _AuthExpired()
                raise PostiTrackingApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise PostiTrackingApiError(
                    f"unparseable body ({err})"
                ) from err

        if not isinstance(payload, dict):
            raise PostiTrackingApiError("unexpected body (not a JSON object)")

        errors = payload.get("errors")
        data = payload.get("data")
        if data is None:
            if errors and any(
                isinstance(e, dict) and e.get("errorType") == "Unauthorized"
                for e in errors
            ):
                # A missing/expired Authorization, alone, answers this way —
                # an ordinary 200 with a null result, not an HTTP error.
                raise _AuthExpired()
            raise PostiTrackingApiError(
                f"resolver failure: {errors!r}" if errors else "empty response"
            )

        result = data.get("consumerSearchShipments")
        if result is None:
            raise PostiTrackingApiError("missing consumerSearchShipments in response")

        hits = result.get("hits") or []
        if not result.get("totalHits") or not hits:
            return None
        hit = hits[0]
        return hit if isinstance(hit, dict) else None

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's public tracking details.

        Returns the hit dict, or ``None`` when Posti reports zero hits (an
        error-free ``totalHits: 0`` — not found, never an error). On a single
        auth rejection the in-memory pair is discarded, a fresh pair minted,
        and the same read retried exactly once; a second rejection surfaces
        as :class:`PostiTrackingApiError`. Network errors propagate as
        ``aiohttp.ClientError``.
        """
        await self._ensure_token()
        try:
            return await self._async_search(tracking_code)
        except _AuthExpired:
            self.reset_token()
            await self._ensure_token()
            try:
                return await self._async_search(tracking_code)
            except _AuthExpired as err:
                raise PostiTrackingApiError(
                    "token rejected twice in a row"
                ) from err
