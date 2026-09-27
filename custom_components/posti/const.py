"""Constants for the Posti parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "posti"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Only the tracking route reports dimensions. Neither claims delivery_window:
# the tracking route's delivery.time has only been seen null, and the account
# route gives a single estimate. Keep in sync with each normalizer.
CAPABILITIES_BY_VARIANT = {
    "Tracking": frozenset(
        {"weight", "dimensions", "pickup_point", "url", "history"}
    ),
    "Account": frozenset({"weight", "pickup_point", "url", "history"}),
}
CAPABILITIES = CAPABILITIES_BY_VARIANT["Tracking"]

# The legacy ``/fi/seuranta#/lahetys/{code}`` link redirects here.
TRACKING_URL = "https://www.posti.fi/seuranta/{code}"

# Two independent sources dispatched from one config entry, chosen once at
# setup and never inferred from a token field (see __init__.py).
CONF_SOURCE = "source"
SOURCE_TRACKING = "tracking"
SOURCE_ACCOUNT = "account"

# --- Tracking-code source: anonymous consumer transport -------------------
# Two keyless requests: an anonymous token mint, then a GraphQL search by
# tracking code. Neither takes a user credential — the token pair is
# throwaway session material, minted fresh per refresh and never persisted.
TRACKING_ANONYMOUS_TOKEN_URL = "https://auth-service.posti.fi/api/v1/anonymous_token"
TRACKING_GRAPHQL_URL = "https://graphql.posti.fi/graphql"

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — this route has no account or parcel feed, so
# the user enters the codes themselves.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# --- OmaPosti account source: PKCE login + account GraphQL ----------------
LOGIN_URL = "https://auth-service.posti.fi/api/v1/login"
TOKEN_URL = "https://auth-service.posti.fi/api/v1/token_v2"
REFRESH_URL = "https://auth-service.posti.fi/api/v1/refresh_v2"
UAS_BASE_URL = "https://todentaminen.posti.fi/uas/authn"
ACCOUNT_GRAPHQL_URL = "https://oma.posti.fi/graphql/v2"

# Fixed PKCE parameters Posti's own login service requires — protocol
# constants, not application secrets.
LOGIN_REDIRECT_URI = "https://oma.posti.fi/app/login"
# The OmaPosti app's relying-party entityID in Posti's login service — a
# fixed SAML identifier, not a per-user or per-install secret.
LOGIN_ENTITY_ID = "34aaf9ea-e060-4d9d-b9a2-2cc6a0e44a2a"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ID_TOKEN = "id_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ROLE_TOKENS = "role_tokens"

# Refresh shortly before the unverified JWT's `exp`.
ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS = 300

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option. Applies to the tracking-code coordinator only;
# the account inbox polls at a fixed cadence (see account/coordinator.py).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight (registered, in_transit, at_pickup_point, unknown, problem,
# returning).
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
