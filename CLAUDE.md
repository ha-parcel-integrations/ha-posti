# Working in this repository

Home Assistant custom integration for **Posti** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

API mechanics — endpoints, parameters, status vocabularies — live in the
private `carrier-research/posti/api/` and are **never** copied here.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` section |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **The optional pickup-point summaries are a pair.** A carrier that can tell
  a parcel is destined for a pickup point exposes
  `en_route_to_pickup_point` for `pickup is true` before it arrives; one that
  can reach `ParcelStatus.AT_PICKUP_POINT` also exposes `awaiting_pickup`.
  See *Parcel contract* in `CONVENTIONS.md`. Say "pickup point", not
  "ServicePoint"/"parcel shop"/"locker", for the generic concept; the
  example carrier demonstrates both canonical sensors.

## Carrier-specific notes

**Two genuinely separate sources, one domain — `tracking/` and `account/`
subpackages**, mirroring the `ha-bpost` pattern. Each owns its own client,
coordinator and normaliser; nothing is shared beyond the domain-root
presentation layer (`const.py`, `device.py`, `events.py`, `diagnostics.py`,
`sensor.py`/`calendar.py`/`button.py`/`device_trigger.py`). There is
deliberately **no shared status map and no shared normalizer** — the account
route knows a shipment's direction and has its own fields, and `status.py` at
the domain root holds only the shared `NEW_ISSUE_URL`, not a map. Both routes
read `status.main`, so the literals they share must agree (one test guards it),
but do not "simplify" the two into one client.

**`CONF_SOURCE` in `entry.data` picks the branch, set once at setup and never
inferred from a token field.** The tracking route is a single hub
(`unique_id = "tracking"`); the account route is `unique_id =
f"account:{sub claim of the id token}"`, several of which may coexist. An account
entry has no `CONF_PARCELS`, no `parcels` options step, and does not register
`track_parcel`/`untrack_parcel` — those stay scoped to tracking entries only
(`services.py`'s `_resolve_entry` filters on `CONF_SOURCE`), and
`async_unload_entry` only tears services down once the last *tracking* entry
is gone.

**Tracking-code source (`tracking/`) — confirmed and buildable, but the
status vocabulary is narrow.** Two keyless requests per refresh: mint an
anonymous token pair (`TRACKING_ANONYMOUS_TOKEN_URL`), then GraphQL-search by
code (`TRACKING_GRAPHQL_URL`). The token pair is minted fresh once per
coordinator refresh (`PostiTrackingClient.reset_token()`, called at the top
of `_async_update_data`) and never cached across refreshes or persisted
anywhere. The search pins `locale: "en"`: without it event text comes back
in Finnish, and the event map below is keyed on the English wording.
`status.main` has two confirmed literals, `DELIVERED` and `RETURN_DELIVERED`
(the return arriving back at the sender, so `returning`, never `delivered`);
every other literal — and the whole `subStatus[]` dimension — reports
`unknown` behind a one-shot warning. Events carry no code, only text, so
`EVENT_STATUS_MAP` maps the English `eventDescription` of each history entry;
text-only events (notifications, signature notes) map to `None` on purpose,
and unrecognised text warns once. The event map never drives the parcel's
own `status`: on a returned parcel the newest event reads "delivered". The
pickup point's name is the `city` of the newest ready-for-pickup event.
`delivered_at` is derived from the newest event's timestamp **only** when
delivered — this is the *only* delivery-time signal that exists; there is no
ETA field on this route at all, so `planned_from`/`planned_to` are always
`None`.
Two distinct auth-failure shapes are handled explicitly in
`tracking/client.py`: a bare non-JSON `401` (missing `X-Posti-Token`) and an
ordinary `200` with `data: null` and `errors[0].errorType == "Unauthorized"`
(missing/expired `Authorization`) — both trigger exactly one remint-and-retry,
never an infinite loop.

**OmaPosti account source (`account/`) — pre-release, provisional.** Posti
retired the password login in October 2026. The source now uses
strong-authentication login (`account/login.py`) and keeps it alive as a
trusted device (`account/device.py`) — an unpublished flow, not a documented
OAuth one: isolate it, never launch a browser or replay a
session. The bank leg runs in the **user's own browser**; the config flow
shows the bank's address and takes back the address the browser ends on
(`authenticate` step), which `login.checked_return_address` accepts only on
the login host and path with a `code`. Reading the login pages is limited to
the offered method links and the few hidden values the hand-over needs, and
fails closed. The requests carry the web view's `X-Requested-With` header:
Posti binds the code to the PKCE challenge only for those. The callback is
visited **without following redirects** and the code taken from its
`Location`. `LOGIN_REDIRECT_URI` and the package/user-agent strings in
`const.py` are fixed protocol parameters, not secrets. Every sign-in (setup
and reauth) runs on a session with its **own cookie jar**, detached afterwards
or when the flow is abandoned (`async_remove`).

After the first login the integration registers an RSA device
(`device.provision`; one login can register only one) and persists
`id_token`, `role_tokens` and the `device` block (id, private key PEM, salt,
subject) in `entry.data`. **No refresh token is kept** — renewal never returns
one; the device's vault holds the refresh key. The device key is a stronger
credential than the old tokens: it renews the account without the user's
bank. It is stored unencrypted (HA has no secret store), the README says so,
and `diagnostics.py` redacts the whole block. Never log it, and never call
`deactivations` without a device id (it turns off every trusted device of the
account, the phones included). Signing timestamps must strictly increase per
device or Posti answers `TIMESTAMP_REPLAYED`. Removal deactivates this device
(`async_remove_entry`); a device Posti already dropped is silent, any other
failure warns with the entry id, never the email.

Failure mapping: a 401/403 from the renewal, `invalid_grant`, or
`INACTIVE_DEVICE`/`NO_DEVICE_FOUND` raises `PostiAccountReauthRequired` →
`ConfigEntryAuthFailed` → HA's reauth (the same login steps again). A 404
without one of those codes, a timeout or anything else is `UpdateFailed`: an
endpoint that moved must not send anyone to their bank. An entry from the
password era has no `device` and asks for the new login at setup. The entry
identity is `account:{sub claim}`; reauth of an older entry moves its unique id
there and refuses (and deactivates) a device registered on another account.

Shipments come from AppSync `GetConsumerShipments` on the same host as the
tracking route: `Authorization` is the consumer role token **without**
`Bearer`, `x-posti-token` is `Bearer <id token>`. A refusal is a 401 **or** a
200 with `errorType: Unauthorized` **and no shipments**; an error beside
shipments, or any other error, is an `UpdateFailed`, so a schema change never
forces a sign-in loop. Never add a field to the account query that isn't
proven to exist — an unknown field fails the whole query, and the account
schema can't be probed without an account (every selected field is one the
anonymous route already resolves). `userRole` `SENDER` shipments are outgoing,
unless they are on their way back (`RETURN_*`), which makes them incoming; any
other role counts as incoming so a new value can never make parcels disappear.
Outgoing parcels live in `coordinator.outgoing` / `outgoing_delivered` (the
unique-id suffixes `_outgoing_parcels` and `_outgoing_delivered_parcels` are
what the aggregator buckets by), fire only `_outgoing_parcel_status_changed` /
`_outgoing_parcel_delivered` and get no per-parcel sensors, as in `ha-bpost`.
`displayName` is the other party: the sender of an incoming parcel, the
receiver of an outgoing one. `status.main` is the **same field name** as on the tracking route but
the account keeps its own map (`account/parcels.py`) because it knows the
direction; a test asserts both maps agree on the literals they share.
`DELIVERED` and `RETURN_DELIVERED` are the only literals confirmed on a real
parcel, so the rest — `ORDER_RECEIVED`, `RETURN_*` and every account-only
field — stays behind the one-shot warning net. A received parcel in any
`RETURN_*` stage is `returning`; a sent one coming back goes through the
stages again. `eventDescription` is a plain string here. OmaPosti has no
dimensions, and `planned_to` stays `None`; `planned_from` is
`delivery.time.timestamp` and unconfirmed, so `delivery_window` stays pending.
Account polling never suspends (one batched call, fixed `MID_INTERVAL_MINUTES`
cadence, `delivered_codes` always empty). **This is why the release is 0.x, not
1.0** — promote only once a real account confirms the login, the envelope,
event order and status literals.

**`url` is `https://www.posti.fi/seuranta/{code}` on both sources.**
Verified live in a browser on 2026-09-27: the page runs the lookup for the
code in the path, and the older `/fi/seuranta#/lahetys/{code}` link redirects
to it. An account parcel without a tracking number falls back to its
shipment number, which has not been confirmed to resolve on that page.

**`raw` is the full record on both sources** — the tracking query is the
website's own `ShipmentDetails` fragment, the account query the fields listed above; privacy lives in `diagnostics.py`'s `TO_REDACT` (pickup codes, pin
codes, addresses, names), never in a trimmed query. Weight comes from both
sources, dimensions only from tracking's `measurements` (`{unit, value}`,
`value` a string). Tracking's `pickup_point` prefers the ready-for-pickup
event's `city` over `pickupPoint.address.publicName`: on a returned parcel
the block still named an earlier point than the one it waited at.
`delivery.time` maps to `planned_from`/`planned_to` with a one-shot warning
per `type` — it has only ever been seen `null`, so `delivery_window` is not
claimed in `CAPABILITIES_BY_VARIANT`. `pickup` is only ever `True` exactly when
`status == AT_PICKUP_POINT`, and no `status.main` literal for a waiting parcel
has been seen yet, so there is no `en_route_to_pickup_point` sensor — it
would be permanently zero by construction.

**Do not build:** the obsolete edge-walled REST tracker as a fallback;
contract OAuth tracking/shipping APIs, general web-portal/browser scraping,
shared credentials or replayed sessions (the narrowly documented account
login and trusted-device exchange is the sole exception); payment, action or
mutation GraphQL operations; manual tracking-code management inside an
account entry; cross-source deduplication (a code tracked in both sources is
two separate entries by design — the aggregator owns cross-carrier dedup);
the public mail-delivery-schedule endpoint as a third source (postcode-level
letter-mail scheduling is not parcel tracking).

## Structure, options flow and dynamic polling

These are suite-wide and identical across every carrier — the authoritative
spec is the carrier template's own documentation, and this repo follows it
exactly. The only carrier-specific structural difference is the two-source
split described above (`tracking/` vs. `account/`, and how each dispatches
its options menu and services).

## Running tests

```
python -m pytest tests/ --cov=custom_components.posti
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file in the same commit;
the API reference is never duplicated into this repo (see the top of this
file for where it lives).
