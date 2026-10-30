"""The device's key: what it signs, and what it opens."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .errors import PostiAccountApiError

_SIGNATURE_PADDING = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
_VAULT_PADDING = padding.OAEP(
    mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
)
# The refresh key the vault holds is always this many characters.
VAULT_KEY_LENGTH = 44


class _Clock:
    """Hands out signing times that never repeat for a device.

    Posti works in whole seconds and rejects a time the device has used
    already, so two requests within one second would otherwise collide.
    """

    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()

    def tick(self, device_id: str) -> int:
        with self._lock:
            now = max(int(time.time()), self._last.get(device_id, 0) + 1)
            self._last[device_id] = now
            return now


clock = _Clock()


def secret_from_salt(salt: bytes, pin: str = "") -> str:
    """Return the hex SHA-256 of the PIN followed by the salt."""
    return hashlib.sha256(pin.encode() + salt).hexdigest()


def generate_key() -> rsa.RSAPrivateKey:
    """Create a 2048-bit RSA key. Slow enough to run off the event loop."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def key_to_pem(key: rsa.RSAPrivateKey) -> str:
    """Serialise the private key without a passphrase."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def public_pem(key: rsa.RSAPrivateKey) -> str:
    """Serialise the public half of the key."""
    return (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def load_key(pem: str) -> rsa.RSAPrivateKey:
    """Load a key written by ``key_to_pem``."""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise PostiAccountApiError("the device key is not an RSA key")
    return key


def sign_text(key: rsa.RSAPrivateKey, message: str) -> str:
    """Return the base64 RSA-PSS signature of a text message."""
    signature = key.sign(message.encode(), _SIGNATURE_PADDING, hashes.SHA256())
    return base64.b64encode(signature).decode()


class Signer:
    """Signs the messages Posti's device service expects for one device."""

    def __init__(
        self, key: rsa.RSAPrivateKey, device_id: str, subject: str, secret: str
    ) -> None:
        """Bind the key to the device, its account and the shared secret."""
        self._key = key
        self._device_id = device_id
        self._subject = subject
        self._secret = secret

    def sign(self, message: str) -> str:
        """Return the base64 signature of a text message."""
        return sign_text(self._key, message)

    def _signatures(self, timestamp: int, context: str = "") -> dict[str, Any]:
        device_part = f"device|{self._device_id}|{timestamp}"
        user_part = (
            f"user|{self._subject}|{self._device_id}|{timestamp}|{self._secret}|"
            f"{context}"
        )
        return {
            "device_sig": self.sign(device_part),
            "user_sig": self.sign(user_part),
        }

    def proof(self) -> dict[str, Any]:
        """Return the fields that prove a request comes from this device."""
        timestamp = clock.tick(self._device_id)
        return {
            "device_id": self._device_id,
            "timestamp": timestamp,
            **self._signatures(timestamp),
        }

    def removal_proof(self) -> dict[str, Any]:
        """Return the fields that authorise removing this device."""
        timestamp = clock.tick(self._device_id)
        digest = hashlib.sha256(
            f"deprovision_device|{self._device_id}".encode()
        ).digest()
        context = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        return {
            "acting_device_id": self._device_id,
            "timestamp": timestamp,
            **self._signatures(timestamp, context),
        }

    def open_vault(self, vault: str) -> str:
        """Decrypt the device's vault into the key that renews its tokens."""
        try:
            opened = self._key.decrypt(base64.b64decode(vault), _VAULT_PADDING)
            text = opened.decode("ascii")
        except ValueError as err:
            raise PostiAccountApiError("the vault could not be opened") from err
        if len(text) != VAULT_KEY_LENGTH:
            raise PostiAccountApiError("the vault held a key of an unexpected length")
        return text
