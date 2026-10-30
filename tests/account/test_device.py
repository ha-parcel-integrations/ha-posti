"""Tests for the OmaPosti trusted-device protocol, against a real RSA key."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest

from custom_components.posti.account import device, signing, tokens
from custom_components.posti.account.device import Device
from custom_components.posti.account.errors import (
    PostiAccountApiError,
    PostiAccountNotJson,
    PostiAccountReauthRequired,
)
from custom_components.posti.account.wire import error_code

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
SALT = base64.b64encode(b"s" * 32).decode()
DEVICE = Device("dev-1", PEM, SALT, "sub-1")
PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
OAEP = padding.OAEP(
    mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
)
VAULT_KEY = "k" * 44


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _verify(data: bytes, signature: str) -> None:
    KEY.public_key().verify(base64.b64decode(signature), data, PSS, hashes.SHA256())


def _vault(key: str = VAULT_KEY) -> str:
    return base64.b64encode(KEY.public_key().encrypt(key.encode(), OAEP)).decode()


@pytest.fixture(autouse=True)
def reset_timestamps():
    signing.clock._last.clear()


def test_device_round_trips_through_entry_data():
    assert Device.from_data(DEVICE.as_data()) == DEVICE


def test_signed_body_carries_two_verifiable_signatures():
    body = DEVICE.signer().proof()
    stamp = body["timestamp"]
    _verify(f"device|dev-1|{stamp}".encode(), body["device_sig"])
    secret = signing.secret_from_salt(b"s" * 32)
    _verify(f"user|sub-1|dev-1|{stamp}|{secret}|".encode(), body["user_sig"])
    with pytest.raises(InvalidSignature):
        _verify(f"device|dev-2|{stamp}".encode(), body["device_sig"])


def test_signing_times_strictly_increase_within_a_second():
    stamps = [signing.clock.tick(DEVICE.device_id) for _ in range(3)]
    assert stamps == sorted(set(stamps))
    assert stamps[0] >= int(time.time())


def test_removal_proof_signs_the_context_too():
    proof = DEVICE.signer().removal_proof()
    stamp = proof["timestamp"]
    context = (
        base64.urlsafe_b64encode(hashlib.sha256(b"deprovision_device|dev-1").digest())
        .decode()
        .rstrip("=")
    )
    secret = signing.secret_from_salt(b"s" * 32)
    _verify(f"user|sub-1|dev-1|{stamp}|{secret}|{context}".encode(), proof["user_sig"])
    assert proof["acting_device_id"] == "dev-1"


def test_other_devices_have_their_own_clock():
    first = signing.clock.tick(DEVICE.device_id)
    other = signing.clock.tick("dev-2")
    assert other <= first + 1


def test_claims_and_expiry():
    soon = _jwt({"exp": time.time() + 10})
    later = _jwt({"exp": time.time() + 3600})
    assert tokens.expires_within(soon, 300)
    assert not tokens.expires_within(later, 300)
    for bad in (None, "", "a.b", "a.!!!.c", _jwt({}), _jwt({"exp": True})):
        assert tokens.expires_within(bad, 300)
    assert tokens.claims(_jwt({"sub": "x"})) == {"sub": "x"}
    assert tokens.claims("a.b") is None


def test_error_of_reads_the_nested_mfa_error():
    assert error_code({"error": {"code": 403, "message": "INACTIVE_DEVICE"}}) == (
        "INACTIVE_DEVICE"
    )
    assert error_code({"error": "invalid_grant"}) == "invalid_grant"
    assert error_code({}) is None


def _requests(*answers):
    mock = AsyncMock(side_effect=list(answers))
    return patch.object(device, "call_json", mock), mock


async def test_refresh_opens_the_vault_and_sends_its_key():
    patcher, mock = _requests(
        (200, {"device_trust_token": "trust", "vault_data": _vault()}),
        (200, {"id_token": "new-id", "role_tokens": []}),
    )
    with patcher:
        fresh = await device.refresh(MagicMock(), DEVICE, "old-id")
    assert fresh["id_token"] == "new-id"
    mfa_call, auth_call = mock.call_args_list
    assert mfa_call.args[1] == f"{device.MFA_URL}/device_refresh"
    assert mfa_call.kwargs["headers"]["Authorization"] == "Bearer old-id"
    assert auth_call.args[1] == f"{device.AUTH_SERVICE_URL}/device_refresh"
    assert (
        auth_call.kwargs["data"]
        == f"refresh_token={VAULT_KEY}&device_trust_token=trust"
    )


async def test_a_removed_device_needs_a_new_login():
    body = {"error": {"code": 403, "message": "INACTIVE_DEVICE"}}
    patcher, _ = _requests((403, body))
    with patcher, pytest.raises(PostiAccountReauthRequired):
        await device.refresh(MagicMock(), DEVICE, "id")


async def test_a_401_without_json_needs_a_new_login():
    patcher = patch.object(
        device,
        "call_json",
        AsyncMock(side_effect=PostiAccountNotJson("x", status_code=401)),
    )
    with patcher, pytest.raises(PostiAccountReauthRequired):
        await device.refresh(MagicMock(), DEVICE, "id")


async def test_a_503_without_json_is_not_a_login_problem():
    patcher = patch.object(
        device,
        "call_json",
        AsyncMock(side_effect=PostiAccountNotJson("x", status_code=503)),
    )
    with patcher, pytest.raises(PostiAccountApiError) as err:
        await device.refresh(MagicMock(), DEVICE, "id")
    assert not isinstance(err.value, PostiAccountReauthRequired)


async def test_a_404_without_a_device_code_is_not_a_login_problem():
    patcher, _ = _requests((404, {"error": {"code": 404, "message": "Not Found"}}))
    with patcher, pytest.raises(PostiAccountApiError) as err:
        await device.refresh(MagicMock(), DEVICE, "id")
    assert not isinstance(err.value, PostiAccountReauthRequired)


async def test_invalid_grant_on_the_final_renewal_needs_a_new_login():
    patcher, _ = _requests(
        (200, {"device_trust_token": "t", "vault_data": _vault()}),
        (400, {"error": "invalid_grant"}),
    )
    with patcher, pytest.raises(PostiAccountReauthRequired):
        await device.refresh(MagicMock(), DEVICE, "id")


async def test_a_vault_key_of_the_wrong_length_is_refused():
    patcher, _ = _requests(
        (200, {"device_trust_token": "t", "vault_data": _vault("short")})
    )
    with patcher, pytest.raises(PostiAccountApiError):
        await device.refresh(MagicMock(), DEVICE, "id")


async def test_a_garbled_vault_is_refused():
    patcher, _ = _requests(
        (200, {"device_trust_token": "t", "vault_data": "!!not-base64!!"})
    )
    with patcher, pytest.raises(PostiAccountApiError):
        await device.refresh(MagicMock(), DEVICE, "id")


async def test_provision_registers_a_signed_device_and_renews_the_login():
    id_token = _jwt({"sub": "sub-9", "exp": time.time() + 3600})
    patcher, mock = _requests(
        (200, {"transaction_id": "tx", "finalization_code": "fin"}),
        (201, {"device_id": "dev-new"}),
        (200, {"token": "trust"}),
        (200, {"id_token": "renewed", "role_tokens": [{"type": "consumer"}]}),
    )
    login_tokens = {"id_token": id_token, "refresh_token": "rt", "role_tokens": []}
    with patcher:
        new_device, renewed = await device.provision(MagicMock(), login_tokens)

    assert new_device.device_id == "dev-new"
    assert new_device.subject == "sub-9"
    assert renewed["id_token"] == "renewed"

    initiate, register, trust, refresh = mock.call_args_list
    assert initiate.args[1] == f"{device.MFA_URL}/provisionings/initiate"
    assert initiate.kwargs["headers"]["Origin"] == "https://mfa.posti.fi/"
    body = register.kwargs["json"]
    assert body["enable_mfa"] is False
    assert body["transaction_id"] == "tx"
    assert body["finalization_code"] == "fin"
    public_key = serialization.load_pem_public_key(body["public_key"].encode())
    public_key.verify(
        base64.b64decode(body["signature"]),
        (body["authorization_secret"] + "tx").encode(),
        PSS,
        hashes.SHA256(),
    )
    assert trust.args[1] == f"{device.MFA_URL}/device_trust_token"
    assert trust.kwargs["json"]["device_id"] == "dev-new"
    assert refresh.kwargs["data"] == (
        f"refresh_token=rt&device_trust_token=trust&id_token={id_token}"
    )


async def test_provision_needs_an_account_in_the_id_token():
    with pytest.raises(PostiAccountApiError):
        await device.provision(MagicMock(), {"id_token": _jwt({})})


async def test_provision_stops_when_the_device_is_not_registered():
    id_token = _jwt({"sub": "s"})
    patcher, _ = _requests(
        (200, {"transaction_id": "tx", "finalization_code": "fin"}),
        (
            403,
            {"error": {"message": "DIRECT_PROVISIONING_AUTHENTICATION_ALREADY_USED"}},
        ),
    )
    with patcher, pytest.raises(PostiAccountApiError, match="ALREADY_USED"):
        await device.provision(MagicMock(), {"id_token": id_token})


async def test_deactivate_targets_only_this_device():
    patcher, mock = _requests((200, {}))
    with patcher:
        await device.deactivate(MagicMock(), DEVICE, "id")
    call = mock.call_args
    assert call.args[1] == f"{device.MFA_URL}/deactivations/devices/dev-1"
    body = call.kwargs["json"]
    assert body["acting_device_id"] == "dev-1"
    _verify(f"device|dev-1|{body['timestamp']}".encode(), body["device_sig"])


async def test_deactivate_failure_is_an_api_error():
    patcher, _ = _requests((500, {"error": "boom"}))
    with patcher, pytest.raises(PostiAccountApiError):
        await device.deactivate(MagicMock(), DEVICE, "id")
