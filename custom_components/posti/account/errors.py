"""Exceptions of the OmaPosti account source."""

from __future__ import annotations


class PostiAccountApiError(Exception):
    """Any failure talking to OmaPosti. Carries a value-free detail."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        """Keep the detail and, if any, the HTTP status."""
        super().__init__(detail)
        self.status_code = status_code


class PostiAccountNotJson(PostiAccountApiError):
    """An answer that was not a JSON document."""


class PostiAccountLoginRejected(PostiAccountApiError):
    """Posti did not accept the authentication the user completed."""


class PostiAccountInvalidReturnAddress(PostiAccountApiError):
    """The pasted address is not one Posti's login returns to."""


class PostiAccountReauthRequired(PostiAccountApiError):
    """The login or the trusted device is no longer accepted: log in again."""
