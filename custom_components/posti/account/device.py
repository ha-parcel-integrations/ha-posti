"""This integration as a trusted device of an OmaPosti account.

Straight after a login we register an RSA key of our own as a trusted device.
Afterwards a request signed with that key
asks Posti for the device's vault, and the key inside the vault renews the
tokens. The device stays trusted until it is removed from the account.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import secrets
from typing import Any
import uuid

import aiohttp

from ..const import APP_USER_AGENT, AUTH_SERVICE_URL, DEVICE_MODEL, MFA_URL
from . import signing, tokens
from .errors import (
    PostiAccountApiError,
    PostiAccountNotJson,
    PostiAccountReauthRequired,
)
from .login import Pkce
from .wire import FORM_HEADERS, call_json, error_code

# Replies that mean Posti has dropped this device.
_DROPPED = frozenset({"INACTIVE_DEVICE", "NO_DEVICE_FOUND"})
_OK = (200, 201)


@dataclass(frozen=True)
class Device:
    """What is kept of a registered device."""

    device_id: str
    # Plain PEM: Home Assistant has nowhere safer to put it.
    private_key: str
    # Base64. With no PIN, the salt alone gives the shared secret.
    salt: str
    # The account the device belongs to (the id token's ``sub``).
    subject: str

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Device:
        """Rebuild a device from the config-entry data it was stored as."""
        return cls(
            data["device_id"], data["private_key"], data["salt"], data["subject"]
        )

    def as_data(self) -> dict[str, str]:
        """Return the form stored in the config entry."""
        return asdict(self)

    def signer(self) -> signing.Signer:
        """Return a signer that acts as this device."""
        return signing.Signer(
            signing.load_key(self.private_key),
            self.device_id,
            self.subject,
            signing.secret_from_salt(base64.b64decode(self.salt)),
        )


def _bearer(id_token: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {id_token}", "User-Agent": APP_USER_AGENT}


def _renewal_form(refresh_key: Any, trust_token: str, id_token: Any = None) -> str:
    # Posti's token endpoint takes these values as they are: the base64 keys
    # contain characters that percent-encoding would change.
    form = f"refresh_token={refresh_key}&device_trust_token={trust_token}"
    return form if id_token is None else f"{form}&id_token={id_token}"


def _renewed(reply: Mapping[str, Any]) -> dict[str, Any]:
    if error_code(reply) == "invalid_grant":
        raise PostiAccountReauthRequired("the renewal is no longer accepted")
    if "error" in reply or not reply.get("id_token"):
        raise PostiAccountApiError(f"tokens were not renewed: {error_code(reply)}")
    return dict(reply)


async def provision(
    session: aiohttp.ClientSession, login_tokens: Mapping[str, Any]
) -> tuple[Device, dict[str, Any]]:
    """Register a device on the account that just logged in.

    Returns the device and the tokens, renewed under it. A login can register
    one device only.
    """
    id_token = login_tokens.get("id_token")
    subject = (tokens.claims(id_token) or {}).get("sub")
    if not isinstance(subject, str) or not subject:
        raise PostiAccountApiError("the id token names no account")

    pkce = Pkce.generate()
    _, begun = await call_json(
        session,
        f"{MFA_URL}/provisionings/initiate",
        params={
            "locale": "fi",
            "state": str(uuid.uuid4()),
            "code_challenge": pkce.challenge,
        },
        headers={**_bearer(id_token), "Origin": "https://mfa.posti.fi/"},
    )
    transaction = begun.get("transaction_id")
    finalization = begun.get("finalization_code")
    if not isinstance(transaction, str) or not isinstance(finalization, str):
        raise PostiAccountApiError(
            f"device registration did not start: {error_code(begun)}"
        )

    key = await asyncio.get_running_loop().run_in_executor(None, signing.generate_key)
    salt = secrets.token_bytes(32)
    secret = signing.secret_from_salt(salt)
    status, registered = await call_json(
        session,
        f"{MFA_URL}/provisionings",
        headers=_bearer(id_token),
        json={
            "transaction_id": transaction,
            "authorization_secret": secret,
            "code_verifier": pkce.verifier,
            "public_key": signing.public_pem(key),
            "device_fingerprint": secrets.token_hex(8),
            "device_model": DEVICE_MODEL,
            "enable_mfa": False,
            "finalization_code": finalization,
            "signature": signing.sign_text(key, secret + transaction),
        },
    )
    device_id = registered.get("device_id")
    if status not in _OK or not isinstance(device_id, str):
        raise PostiAccountApiError(
            f"device was not registered (status {status}): {error_code(registered)}",
            status_code=status,
        )

    device = Device(
        device_id, signing.key_to_pem(key), base64.b64encode(salt).decode(), subject
    )
    status, trusted = await call_json(
        session,
        f"{MFA_URL}/device_trust_token",
        headers=_bearer(id_token),
        json=device.signer().proof(),
    )
    trust_token = trusted.get("token")
    if status not in _OK or not isinstance(trust_token, str):
        raise PostiAccountApiError(
            f"device was not trusted (status {status}): {error_code(trusted)}",
            status_code=status,
        )
    _, renewed = await call_json(
        session,
        f"{AUTH_SERVICE_URL}/device_refresh",
        headers=FORM_HEADERS,
        data=_renewal_form(login_tokens.get("refresh_token"), trust_token, id_token),
    )
    return device, {**login_tokens, **_renewed(renewed)}


async def refresh(
    session: aiohttp.ClientSession, device: Device, id_token: str
) -> dict[str, Any]:
    """Renew the tokens using only the device.

    The reply has new id and role tokens but no refresh token; the vault
    delivers the key to renew with.
    """
    signer = device.signer()
    try:
        status, reply = await call_json(
            session,
            f"{MFA_URL}/device_refresh",
            headers=_bearer(id_token),
            json=signer.proof(),
        )
    except PostiAccountNotJson as err:
        # A refusal needs a new login whatever its body looks like.
        if err.status_code in (401, 403):
            raise PostiAccountReauthRequired(
                f"login refused (status {err.status_code})"
            ) from err
        raise
    problem = error_code(reply)
    if problem in _DROPPED:
        raise PostiAccountReauthRequired(f"the device is no longer trusted ({problem})")
    if status in (401, 403):
        raise PostiAccountReauthRequired(f"login refused (status {status}): {problem}")
    # A 404 without one of the codes above says nothing about the device: a
    # moved endpoint must not send anyone back to their bank.
    trust_token = reply.get("device_trust_token")
    vault = reply.get("vault_data")
    if (
        status not in _OK
        or not isinstance(trust_token, str)
        or not isinstance(vault, str)
    ):
        raise PostiAccountApiError(
            f"vault was not delivered (status {status}): {problem}",
            status_code=status,
        )

    _, renewed = await call_json(
        session,
        f"{AUTH_SERVICE_URL}/device_refresh",
        headers=FORM_HEADERS,
        data=_renewal_form(signer.open_vault(vault), trust_token),
    )
    return _renewed(renewed)


async def deactivate(
    session: aiohttp.ClientSession, device: Device, id_token: str
) -> None:
    """Remove this device, and only this device, from the account.

    The endpoint without a device id turns off every trusted device of the
    account, the phones with the OmaPosti app included, so it is never used.
    """
    status, reply = await call_json(
        session,
        f"{MFA_URL}/deactivations/devices/{device.device_id}",
        headers=_bearer(id_token),
        json=device.signer().removal_proof(),
    )
    if status not in _OK:
        raise PostiAccountApiError(
            f"device was not removed (status {status}): {error_code(reply)}",
            status_code=status,
        )
