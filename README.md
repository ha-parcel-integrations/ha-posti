# Posti Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-posti.svg)](https://github.com/ha-parcel-integrations/ha-posti/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-posti/total.svg)](https://github.com/ha-parcel-integrations/ha-posti/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Posti](https://www.posti.fi/) (Finland) parcels and packages, either by tracking code or through your OmaPosti account. Choose one source when you add the integration:

- **Tracking codes** — no account needed, just the tracking code from your shipping confirmation or missed-delivery card.
- **OmaPosti account** — sign in once and every parcel in your account inbox is discovered automatically. **Pre-release (0.x):** the account payload is not yet confirmed against a real inbox — see [Disclaimer](#disclaimer).

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Dynamic polling](#dynamic-polling)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Two independent sources: keyless tracking codes, or an OmaPosti account inbox
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …) and the carrier's own status text
- Summary sensors: incoming parcels, next delivery, awaiting pickup, recently delivered parcels
- Read-only **Deliveries** calendar
- `posti.track_parcel` / `posti.untrack_parcel` services on a tracking-code hub, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- Either a Posti parcel and its tracking code (from the shipping confirmation email or the missed-delivery card), or an OmaPosti account (username and password)

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-posti` as an **Integration**.
3. Install **Posti** and restart Home Assistant.

### Manual

Copy `custom_components/posti` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Posti**, then choose a source:

- **Tracking codes** — the hub is created immediately, nothing to fill in. Add parcels afterwards via the integration's **Configure** dialog, the [`posti.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml).
- **OmaPosti account** — sign in with your OmaPosti username and password. Your password is used only for this one-time sign-in and is never stored; parcels are then discovered automatically. Sign-in may not work for an account with two-step verification turned on.

You can add both a tracking-code hub and one or more OmaPosti accounts side by side — they are entirely independent.

## Options

Open **Configure** on the integration entry. A tracking-code hub gets both sections; an OmaPosti account entry gets **Settings** only (there is no manual parcel list on an account).

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels *(tracking-code hub only)* | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Settings | Delivered parcels: filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Settings | Include status history | off | Adds a `history` attribute per parcel with each status update. |

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence:

- **Tracking-code hub** — the same status-driven schedule as the rest of the suite: quiet hours (00:00–06:00 local, with two catch-up anchors), a hot 15-minute tier while a parcel is out for delivery, a normal 45-minute tier otherwise, and a full stop once every tracked parcel is delivered (or nothing is tracked). A small, fixed per-hub offset avoids every install polling at the same second.
- **OmaPosti account** — one batched inbox read every 45 minutes, at a fixed cadence. There is no delivery-window field on this route to key a "hot" tier off, and the inbox never fully stops polling (a new parcel can appear at any time).

## Removal

Standard HA removal applies: **Settings → Devices & Services → Posti → ⋮ → Delete**. An OmaPosti account's stored tokens are removed with it; nothing else is stored on Posti's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.posti_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.posti_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.posti_next_delivery` | Earliest expected delivery moment across all active parcels *(currently always empty — see [Troubleshooting](#troubleshooting))* |
| `sensor.posti_awaiting_pickup` | Parcels waiting for collection at a pickup point (OmaPosti account only) |
| `sensor.posti_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.posti_last_successful_update` | Diagnostic: when Posti was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. Not every status is reachable from every source — see the notes below.

| Status | Meaning | Tracking codes | OmaPosti account |
|---|---|---|---|
| `registered` | Announced, not yet handed over | — | `WAITING` |
| `in_transit` | In the sorting network | — | `RECEIVED`, `IN_TRANSPORT` |
| `out_for_delivery` | With the courier today | — | `IN_DELIVERY` |
| `at_pickup_point` | Waiting for you at a pickup location | — | `READY_FOR_PICKUP` |
| `delivered` | Delivered | `DELIVERED` | `DELIVERED` |
| `returning` | Going back to the sender | `RETURN_DELIVERED` | `RETURNED_TO_SENDER` |
| `problem` | Posti reports an exception | — | — |
| `unknown` | Not yet scanned, or a status we have not mapped yet | any other `status.main` | any other `shipmentPhase` |

The tracking-code route has two confirmed mappings (`DELIVERED`, and `RETURN_DELIVERED` for a parcel returned to its sender) — every other public status literal reports `unknown` until a real parcel confirms it (see [Disclaimer](#disclaimer)). Its history entries do get a canonical status per event, mapped from Posti's English event text, and a parcel that waited at a pickup point shows that point's name as `pickup_point`. The OmaPosti account's whole status map is provisional for the same reason. The carrier's own text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Posti device):

| Event | When |
|---|---|
| `posti_parcel_registered` | A new parcel appears in the active list |
| `posti_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `posti_parcel_delivered` | A parcel is delivered |
| `posti_parcel_delivery_time_changed` | The expected delivery window changes *(neither source currently populates a delivery window, so this does not fire yet)* |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `posti.track_parcel` | `tracking_code` | Start tracking a parcel (tracking-code hub only) |
| `posti.untrack_parcel` | `tracking_code` | Stop tracking a parcel (tracking-code hub only) |

An OmaPosti account entry does not register these services — its parcels come from the account inbox, not a manual list.

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

## Debugging

```yaml
logger:
  logs:
    custom_components.posti: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — on the tracking-code route this is normal until Posti's public tracker maps that particular status; on an OmaPosti account it means that shipment phase hasn't been confirmed yet. Either way it will resolve itself once the parcel reaches a confirmed status (`delivered`, at least).
- **A status logs "Unrecognised Posti … status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-posti/issues/new) with the logged line so the mapping can be extended.
- **`next_delivery` is always empty, and there is no delivery-window field anywhere** — this is not a bug. Posti's public tracking API has no delivery-time field at all, and the OmaPosti account query deliberately does not select one either (neither route has confirmed what it would mean). `delivered_at` is derived instead, from the newest tracking-code event, only once a parcel is `delivered`.
- **OmaPosti sign-in fails, or stops working after a while** — the account source emulates the OmaPosti Android app's login sequence rather than using a published API; if Posti changes that app's login flow this can break until the integration is updated. A rejected refresh triggers Home Assistant's normal reauthentication flow — just re-enter your password.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Posti, Home Assistant, or any other third party referenced in this project. Please don't contact Posti for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or tracking code where required. These may change or be withdrawn without notice and may be subject to Posti's terms. Data is sent only to Posti's own services; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

The tracking-code source uses the same keyless, anonymous-token public endpoint as the Posti consumer tracking website; its transport, envelope and the `DELIVERED` and `RETURN_DELIVERED` statuses are confirmed against real parcels, but the rest of the public status vocabulary is not yet confirmed. **The OmaPosti account source is pre-release (0.x)**: it emulates the OmaPosti Android app's login sequence rather than a published API, and its shipment payload, event ordering and all seven status literals remain unconfirmed against a real account inbox. Both sources log an `unrecognised status` warning the first time they see something they cannot map — please [open an issue](https://github.com/ha-parcel-integrations/ha-posti/issues/new) if you see one, so the mapping can be confirmed and extended.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
