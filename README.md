# Squadrons Telemetry — EDMC Plugin

Optional companion plugin for [EDMarketConnector](https://github.com/EDCD/EDMarketConnector)
that streams Elite Dangerous journal events from your client to your squadron's
Elite Squadrons instance.

The plugin is **squadron-scoped and authenticated**: each member generates a
personal API token in the Squadrons web app, pastes it into the plugin
settings, and from that point on every supported event you generate in-game
flows directly into your squadron's data.

---

## What the plugin actually does

The plugin forwards a fixed set of journal events to
`POST /api/v1/telemetry/ingest`. Nothing else is sent: no chat, no friends
list, no system map data — just the journal events listed below.

### BGS / movement
- `FSDJump`, `Location` — refreshes faction influence in your tracked
  systems and updates your "current system" in the member directory.

### Stations
- `Docked` — keeps the station catalog (type, services, pads, controlling
  faction) up to date for stations you visit, including fleet carriers.

### Markets, outfitting, shipyards
- `Market`, `Outfitting`, `Shipyard` — full live snapshots of commodity
  prices, modules for sale, and ships for sale at the station you're docked
  at. Listings are replaced on each visit, not appended.

### Fleet carriers (full lifecycle)
- `CarrierJump`, `CarrierJumpRequest`, `CarrierJumpCancelled`,
  `CarrierLocation` — schedule, cancel, complete, and reconcile carrier
  jumps. Live telemetry is the strongest source for jump and location data.
- `CarrierStats`, `CarrierDepositFuel` — tritium level and stats
  (services, docking access, name).
- `CarrierTradeOrder` — purchase/sale/cancel orders for commodities on
  your carrier.
- `CarrierNameChanged`, `CarrierDockingPermission`, `CarrierCrewServices` —
  rename, docking access changes, and individual service activations.
- `CarrierDecommission`, `CarrierCancelDecommission` — decommission
  scheduling and cancellation.

### What you get for sending this
- Live BGS influence in your squadron's tracked-system dashboards.
- A live fleet-carrier view with up-to-date location, fuel, services,
  docking access, jump schedule, and trade orders.
- Squadron-scoped market / outfitting / shipyard intelligence for the
  stations your squadron visits.
- A squadron-attributed location for you in the member directory.

The plugin **does not** perform proof-of-participation enrolment, conflict
zone tracking, or operation auto-tagging — those features may layer on the
same telemetry feed in future, but are not part of v1.x.

---

## Where the plugin lives

Source lives in this repository under
[`edmc-plugin/`](https://github.com/Vintenxe/elite-squadrons/tree/main/edmc-plugin).

Right now the supported way to get the plugin is to download it directly
from GitHub:

1. Open the [`edmc-plugin/`](https://github.com/Vintenxe/elite-squadrons/tree/main/edmc-plugin)
   folder on GitHub.
2. Either clone the repository or download the folder as a ZIP via GitHub's
   "Code → Download ZIP" and extract just the `edmc-plugin/` folder.
3. Rename the extracted folder to `SquadronsTelemetry` (any name works —
   the folder name is what EDMC shows).

A signed packaged release is **not** part of v1.x; an in-app downloader is
on the roadmap but is intentionally not in v1.x either.

---

## Installation

EDMC plugins are loose Python folders dropped into EDMC's plugin directory.

1. Find your EDMC plugin directory:
   - **Windows**: `%LOCALAPPDATA%\EDMarketConnector\plugins`
   - **macOS**: `~/Library/Application Support/EDMarketConnector/plugins`
   - **Linux**: `~/.local/share/EDMarketConnector/plugins`
2. Copy the `SquadronsTelemetry` folder you obtained above into that
   directory. The final path should be e.g.
   `…/EDMarketConnector/plugins/SquadronsTelemetry/squadrons_telemetry.py`.
3. Restart EDMC. The plugin appears in
   **File → Settings → Plugins** as **Squadrons Telemetry**.

Requires **EDMC 5.x or later** (Python 3 entry point `plugin_start3`).

---

## Create a telemetry token

Tokens are per-device. If you play on more than one PC, generate one token
per device so you can revoke a single device without breaking the others.

1. Sign in to your squadron on Elite Squadrons.
2. Open **Settings → Telemetry Clients**.
3. Click **New Client**, give it a memorable name (e.g. *Gaming PC*).
4. Copy the token that appears. **It is shown once.** If you lose it,
   revoke that client and create a new one — there is no way to recover
   an existing token.

The token has the format `sq_<48 hex chars>`.

---

## Configure the plugin

1. Open EDMC → **File → Settings → Squadrons Telemetry**.
2. Set **Server URL** to your Squadrons instance, e.g.
   `https://squadrons.example.com` (no trailing slash needed; one is
   stripped automatically).
3. Paste the token into **API Token**.
4. Click **OK** to save.

The plugin batches events for up to 10 seconds (or 30 events) before
sending. Events are buffered locally on send failures and re-attempted on
the next flush.

### Verify it's working

After your next FSDJump or Docked event, return to the Squadrons web app:

- **Settings → Telemetry Clients** — the client row should show a
  `Last used` timestamp and a non-zero event count.
- **Carriers / BGS / Stations** — recently observed data should reflect
  the events you just generated.

If `Last used` never updates, see Troubleshooting below.

---

## Source precedence (where this fits)

Authenticated direct telemetry sits above passive EDDN scrapes for the
domains the plugin affects, because we trust an authenticated squadron
member's first-party events more than anonymous community data:

| Domain          | Highest                          | Notes                                                 |
| --------------- | -------------------------------- | ----------------------------------------------------- |
| BGS faction     | `MANUAL_OVERRIDE` > `MANUAL_ENTRY` > **`SQUAD_TELEMETRY`** > `FRONTIER_CAPI` > `EDDN` > `EDSM` | Telemetry beats EDDN; manual edits beat both.        |
| Galaxy / market | `FRONTIER_CAPI` > `MANUAL` > **`SQUAD_TELEMETRY`** > `EDDN` > `SPANSH` > `EDSM` | cAPI snapshots are still strongest for market data. |
| Carrier live    | **`SQUAD_TELEMETRY`** > `EDDN` > `FRONTIER_CAPI` > `MANUAL` (location, jumps, fuel, decommission) | Live signals belong to telemetry.                    |
| Carrier static  | `MANUAL` / `FRONTIER_CAPI` > `SQUAD_TELEMETRY` (owner, services, displayName) | Curator/cAPI authoritative for slow-changing fields. |

This is not configurable per-client; the precedence tables live in
`src/lib/bgs-ingest.ts`, `src/lib/galaxy-ingest.ts`, and
`src/lib/carrier-ingest.ts`.

---

## Troubleshooting

**`Last used` never updates / no events showing up**
- Confirm Server URL is reachable from your machine
  (`https://<your-instance>/api/health` should return `{ "ok": true }`).
- Confirm the token is the full `sq_…` value with no whitespace.
- Confirm you're not in the Elite Dangerous beta — beta journals are
  intentionally ignored by the plugin (`is_beta` guard).
- Check EDMC's log: `Help → Open Log Folder` → look for lines from
  `Squadrons Telemetry`.

**`401 Unauthorized` in the EDMC log**
- Token was revoked (visible in **Settings → Telemetry Clients**).
- Token was mistyped — re-paste from a freshly created client.

**`429 Rate limit exceeded`**
- Default is 60 requests/minute per client. Each batch is one request, so
  you should never hit this with normal play.
- If you do, leave the plugin alone for a minute and it will resume.

**Worried about a leaked token?**
- Revoke it from **Settings → Telemetry Clients** — the client row is
  rejected immediately on the next request.
- Create a new client with a new token and reconfigure the plugin.

---

## Version & compatibility

The plugin sends `X-Plugin-Version` on every request so the server can
record the running version per client. Today this is advisory only — no
versions are blocked. Future server releases may reject below a minimum
version with `426 Upgrade Required`; the plugin will surface that to you.

| Plugin version | Notes                                                                  |
| -------------- | ---------------------------------------------------------------------- |
| `1.0.0`        | Initial release: BGS, Docked, Market, Outfitting, Shipyard, CarrierJump only. |
| `1.1.0`        | Adds the full carrier lifecycle (11 new event types) and `X-Plugin-Version` header. |

---

## Privacy

- The plugin only sends the journal events listed above. It does not read
  your save game, your Frontier login, your friends list, or anything
  outside the EDMC journal stream.
- Events are scoped to your squadron via your authenticated token. Other
  squadrons cannot see your data.
- Tokens are stored hashed (SHA-256) on the server. Only the prefix is
  visible afterwards.
- You can revoke a token at any time from the Squadrons web app.
