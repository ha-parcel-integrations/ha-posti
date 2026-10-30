"""Reading the claims of Posti's id tokens.

The claims are only used for timing and for telling accounts apart; the token
is never checked here, because Posti's services do that.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any


def claims(token: Any) -> dict[str, Any] | None:
    """Return the claims of a JSON web token, or None if it is not one."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    segment = token.split(".")[1]
    try:
        decoded = json.loads(
            base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        )
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def expires_within(token: Any, seconds: int) -> bool:
    """Whether the token is expired, or will be within ``seconds``.

    A token without a usable expiry counts as expired.
    """
    expiry = (claims(token) or {}).get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return True
    return expiry - time.time() < seconds
