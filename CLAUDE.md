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
deliberately **no shared status map and no shared normalizer** — the two
routes report different shapes (`status.main` + `subStatus[]` vs. a flat
`shipmentPhase`) and `status.py` at the domain root holds only the shared
`NEW_ISSUE_URL`, not a map. Do not "simplify" the two into one client just
because `DELIVERED` happens to be spelled the same in both — that is a
coincidence, not a shared vocabulary.

**`CONF_SOURCE` in `entry.data` picks the branch, set once at setup and never
inferred from a token field.** The tracking route is a single hub
(`unique_id = "tracking"`); the account route is `unique_id =
f"account:{normalised username}"`, several of which may coexist. An account
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

**OmaPosti account source (`account/`) — pre-release, provisional.** This
emulates the OmaPosti Android app's WebView/SAML PKCE login
(`account/auth.py`), not a published OAuth flow — isolate it, never launch a
browser or replay a session. `LOGIN_ENTITY_ID` and `LOGIN_REDIRECT_URI` in
`const.py` are fixed protocol parameters the login service requires, not
secrets. The password is used once for the login exchange and is never
stored; only the token set (`id_token`, `refresh_token`, `role_tokens`) is
persisted in `entry.data`, redacted from diagnostics by name. Every sign-in
(setup and reauth) runs on a session with its **own cookie jar**, detached
afterwards — the SAML hop keeps its login session in cookies, and HA's shared
session's jar would carry one account's session into the next sign-in.
When the code only arrives on the success page's auto-submitting form, the
form's callback is visited first (as the WebView would) and the code from the
redirect to `LOGIN_REDIRECT_URI` wins — that redirect is the only place the
app itself reads a code. Refresh uses the app's `refresh_v2` over the legacy
`token_v2` route, sent exactly as the app sends it: **the id token** (not
the refresh token) as `Authorization: Bearer`, the refresh token unencoded
in the form body. The APK declares `refresh_v2/ack` but never calls it, so
neither does this — don't add it back. Only an auth-typed GraphQL error
(`errorType` starting with `Unauthorized`) triggers refresh/reauth; any other
error is an `UpdateFailed`, so a schema change never forces a sign-in loop.
Accounts with two-step verification are unconfirmed: the password step then
yields no code and surfaces as `invalid_auth`, whose text says so. A
rejected refresh raises `ConfigEntryAuthFailed` from
`PostiAccountCoordinator._async_update_data`, which Home Assistant turns into
its normal reauth flow (re-asking only the password, keeping the same
unique ID). The whole seven-literal `shipmentPhase` map in
`account/parcels.py` is a **hypothesis**, not a fixture — none of the seven
literals has been seen on a real account response, every literal is behind
the one-shot warning net, `delivered_at`/`planned_from`/`planned_to`/
`weight`/`dimensions`/`pickup_point` all stay `None` structurally (the
minimal query selects none of the fields that would populate them), and
account polling never suspends (one batched call, fixed
`MID_INTERVAL_MINUTES` cadence, `delivered_codes` always empty).
**This is why the release is 0.x, not 1.0** — promote only once a real
account confirms the envelope, event order and status literals.

**`url` is `https://www.posti.fi/seuranta/{code}` on both sources.**
Verified live in a browser on 2026-09-27: the page runs the lookup for the
code in the path, and the older `/fi/seuranta#/lahetys/{code}` link redirects
to it. An account parcel without a tracking number falls back to its
shipment number, which has not been confirmed to resolve on that page.

**No weight, dimensions or ETA window on either source; only the tracking
route names a pickup point** (`CAPABILITIES_BY_VARIANT`). The tracking route
structurally cannot supply the rest (every plausible field name was checked
and does not exist); the account route's minimal query deliberately does not
select the fields that would. `pickup` is only ever `True` exactly when
`status == AT_PICKUP_POINT`, and no `status.main` literal for a waiting parcel
has been seen yet, so there is no `en_route_to_pickup_point` sensor — it
would be permanently zero by construction.

**Do not build:** the obsolete edge-walled REST tracker as a fallback;
contract OAuth tracking/shipping APIs, general web-portal/browser scraping,
shared credentials or replayed sessions (the narrowly documented account
WebView exchange is the sole app-emulation exception); payment, action or
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
