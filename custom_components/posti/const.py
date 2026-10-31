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

    REGISTERED = "registered"  # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"  # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"  # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"  # Ready to collect at a pickup location
    DELIVERED = "delivered"  # Handed over
    RETURNING = "returning"  # Failed delivery, going back to sender
    PROBLEM = "problem"  # Carrier reports an exception/issue
    UNKNOWN = "unknown"  # Raw status we have not mapped yet


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
    "Tracking": frozenset({"weight", "dimensions", "pickup_point", "url", "history"}),
    "Account": frozenset({"weight", "pickup_point", "url", "history"}),
}

# Fields not confirmed yet — the docs site shows them as "awaiting data".
# Move a field into the declaration above once a real parcel shows it.
PENDING_CAPABILITIES_BY_VARIANT = {
    "Account": frozenset({"delivery_window"}),
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

# --- OmaPosti account source: strong-authentication login + trusted device --
AUTH_SERVICE_URL = "https://auth-service.posti.fi/api/v1"
MFA_URL = "https://mfa.posti.fi/api/v1"
UAS_HOST = "todentaminen.posti.fi"
# AppSync, the same GraphQL the anonymous tracking route uses.
ACCOUNT_GRAPHQL_URL = "https://graphql.posti.fi/graphql"
# One page holds a whole account: pages are not a snapshot, and a shipment that
# drops off one between two requests would move another off the next.
ACCOUNT_PAGE_SIZE = 1000
# AppSync answers a refused id token with status 200, this error type and no
# shipments; a refused role token gets 401.
UNAUTHORIZED_ERROR_TYPE = "Unauthorized"

# Posti's login only works with these client identifiers: one for the API
# calls, one for the login pages.
APP_USER_AGENT = "okhttp/4.12.0"
# suite: allow browser user-agent (the login pages refuse any client identifier but the app's own web view)
WEB_VIEW_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; sdk_gphone64_x86_64 "
    "Build/BE2A.250530.026.F3; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/133.0.6943.137 Mobile Safari/537.36 "
    "OmaPostiAndroid/6.5.1+7713/WebView(133.0.6943.137)"
)
APP_PACKAGE = "fi.itella.posti.android"
LOGIN_REDIRECT_URI = "https://oma.posti.fi/app/login"
# How the integration's trusted device shows in the account's device list.
DEVICE_MODEL = "Home Assistant"
# Order of the authentication methods in the config flow, by provider
# (telia.ftn.oidc.<provider>.1): Mobiilivarmenne, the banks, then the rest in
# the login page's order.
METHOD_ORDER = [
    "mobileid",
    "aktia",
    "danske",
    "nordea",
    "omasp",
    "op",
    "pop",
    "spankki",
    "saastopankki",
    "alandsbanken",
    "hst",
    "htid",
]

CONF_USERNAME = "username"
CONF_ID_TOKEN = "id_token"
CONF_ROLE_TOKENS = "role_tokens"
CONF_DEVICE = "device"
CONF_METHOD = "method"
CONF_RETURN_ADDRESS = "return_address"

# Renew shortly before the unverified JWT's `exp`.
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
