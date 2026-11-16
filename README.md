# Squadrons Telemetry — EDMC Plugin

An [EDMarketConnector](https://github.com/EDCD/EDMarketConnector) plugin that
streams Elite Dangerous journal events from your client to your squadron's
instance of [Elite Squadrons](https://elitesquadrons.com).

The plugin is **squadron-scoped and authenticated**: each member generates a
personal API token in the Squadrons web app, pastes it into the plugin
settings, and from that point on every supported event you generate in-game
flows directly into your squadron's data.

---

## What the plugin does

The plugin forwards a fixed set of journal events to
`POST /api/v1/telemetry/ingest` on your Squadrons server. Nothing else is
sent: no chat, no friends list, no system map data — just the journal
events listed below.

### BGS / movement
- `FSDJump`, `Location` — refresh faction influence in your squadron's
  tracked systems and update your "current system" in the member
  directory.

### Stations
- `Docked` — keeps the station catalog (type, services, pads, controlling
  faction) up to date for stations you visit, including fleet carriers.

### Markets, outfitting, shipyards
- `Market`, `Outfitting`, `Shipyard` — full live snapshots of commodity
  prices, modules for sale, and ships for sale at the station you're
  docked at. Listings are replaced on each visit, not appended.

The journal line for these events carries only the station's identity.
The inventory itself lives in the `Market.json`, `Outfitting.json` and
`Shipyard.json` files that Elite Dangerous writes into its journal folder
each time you open the corresponding station screen. Since plugin 1.4.0
the plugin reads the matching file from **EDMC's configured journal
folder** (the same files EDMC's own EDDN integration reads) when the
journal event arrives, checks that it belongs to that exact observation
(same `MarketID`, station, system and a timestamp within two minutes),
reduces it to the fields the server uses, and forwards the result in the
same batch as your other events. EDMC must be running at the moment you
open the market / outfitting / shipyard screen. **No separate
Frontier-account link in EDMC is required.**

What is forwarded from those files:

| File | Forwarded | Dropped locally |
| ---- | --------- | --------------- |
| `Market.json` | per commodity: canonical symbol (`gold`), buy/sell/mean price, stock, demand | `id`, localised name (language-dependent; identity is the symbol), category, `Rare`, `Consumer`/`Producer` flags, brackets |
| `Outfitting.json` | canonical lower-case module names (e.g. `int_shieldgenerator_size3_class5`) — every entry matching the EDDN outfitting pattern (`hpt_*`, `int_*`, `*_armour_*`) | `id`, `BuyPrice`, `Horizons`, the free `int_planetapproachsuite`, and non-module items the game lists on the same screen (paintjobs, decals, bobbleheads, nameplates, ship kits) |
| `Shipyard.json` | canonical ship identifiers (e.g. `krait_mkii`) | `id`, `ShipPrice`, `Horizons`, `AllowCobraMkIV` |

A snapshot is **not** forwarded — and the EDMC log says why — when the file
is missing, empty, still being written, malformed, more than two minutes
away from the journal event's timestamp, for a different station or
system, missing the station or system name on either side, larger than
4 MB, over the server's per-list caps (1024 commodities / 1024 modules /
128 ships) or over the 64 KB per-event limit. (The two-minute window exists
because the game writes the file and the journal line in the same moment —
their timestamps normally agree to the second — while a file left from an
earlier visit to the same station is hours apart; two minutes tolerates
any clock or write-ordering jitter without admitting a stale file.) Nothing partial is ever sent: a refused snapshot
cannot erase or invent inventory on the server. A genuinely empty list is
also not forwarded (the server cannot represent "known empty" yet; see
Limitations).

### Fleet carriers (full lifecycle)
- `CarrierJump`, `CarrierJumpRequest`, `CarrierJumpCancelled`,
  `CarrierLocation` — schedule, cancel, complete, and reconcile carrier
  jumps. Live telemetry is the strongest source for jump and location
  data.
- `CarrierStats`, `CarrierDepositFuel` — tritium level and stats
  (services, docking access, name).
- `CarrierTradeOrder` — purchase/sale/cancel orders for commodities on
  your carrier.
- `CarrierNameChanged`, `CarrierDockingPermission`, `CarrierCrewServices`
  — rename, docking-access changes, and individual service activations.
- `CarrierDecommission`, `CarrierCancelDecommission` — decommission
  scheduling and cancellation.

### What you get in return
- Live BGS influence in your squadron's tracked-system dashboards.
- A live fleet-carrier view with up-to-date location, fuel, services,
  docking access, jump schedule, and trade orders.
- Squadron-scoped market / outfitting / shipyard intelligence for the
  stations your squadron visits.
- A squadron-attributed location for you in the member directory.

The plugin **does not** perform proof-of-participation enrolment, conflict
zone tracking, or operation auto-tagging. Those are not part of v1.x.

---

## Requirements

- [EDMarketConnector](https://github.com/EDCD/EDMarketConnector) **5.x or
  later** (Python 3; the plugin uses the `plugin_start3` entry point).
- An account on an Elite Squadrons instance (e.g.
  <https://elitesquadrons.com>) with permission to generate telemetry
  tokens.
- Python standard library only — no extra packages to install.
- EDMC's **E:D journal file location** setting must point at the folder
  the game writes to (it does by default); the plugin reads
  `Market.json` / `Outfitting.json` / `Shipyard.json` from that folder.

---

## Install

EDMC plugins are loose Python folders dropped into EDMC's plugin
directory. There is no installer.

1. Download the plugin. Two paths, pick whichever is available:
   - **Preferred — packaged release.** Open the
     [Releases page](https://github.com/Vintenxe/squadrons-edmc-plugin/releases)
     and download the `SquadronsTelemetry-vX.Y.Z.zip` asset attached to
     the latest release. This archive is packaged to extract cleanly
     into a `SquadronsTelemetry/` folder.
   - **Fallback — repository ZIP.** On the repository page, choose
     **Code → Download ZIP**. GitHub will give you an archive whose
     top-level folder is named after the repo/branch
     (e.g. `squadrons-edmc-plugin-main/`). This works too, but you
     will need to rename that folder to `SquadronsTelemetry` in
     step 3.
2. Extract the archive wherever you like.
3. Make sure the final folder is named exactly `SquadronsTelemetry`
   and contains both `load.py` (EDMC's entrypoint) and
   `squadrons_telemetry.py` (the implementation) at its top level.
   EDMC uses the folder name to identify the plugin; any name would
   technically work, but these docs and log lines assume
   `SquadronsTelemetry`.
4. Move that `SquadronsTelemetry` folder into EDMC's plugin directory:
   - **Windows:** `%LOCALAPPDATA%\EDMarketConnector\plugins`
   - **macOS:** `~/Library/Application Support/EDMarketConnector/plugins`
   - **Linux:** `~/.local/share/EDMarketConnector/plugins`
5. The final paths should look like
   `…/EDMarketConnector/plugins/SquadronsTelemetry/load.py` and
   `…/EDMarketConnector/plugins/SquadronsTelemetry/squadrons_telemetry.py`.
6. Restart EDMC. The plugin appears in
   **File → Settings → Plugins** as **Squadrons Telemetry**, and a
   dedicated **Squadrons Telemetry** settings tab appears alongside
   the other plugin tabs in **File → Settings**.

### Updating

Delete the old `SquadronsTelemetry` folder, drop the new one in its place,
and restart EDMC. Your server URL and API token are stored inside the
plugin folder in `config.json`; if you want to keep them across updates,
copy that file into the new folder before restarting. The `config.json`
format has not changed since 1.1 — a file written by any earlier 1.x
release is read as-is by 1.4.0.

---

## Create a telemetry token

Tokens are per-device. If you play on more than one PC, generate one token
per device so you can revoke a single device without breaking the others.

1. Sign in to your squadron on your Elite Squadrons instance (e.g.
   <https://elitesquadrons.com>).
2. Open **Settings → Telemetry Clients**.
3. Click **New Client** and give it a memorable name (e.g. *Gaming PC*).
4. Copy the token that appears. **It is shown once.** If you lose it,
   revoke that client and create a new one — there is no way to recover
   an existing token.

Tokens have the format `sq_<48 hex chars>`.

---

## Configure the plugin

1. Open EDMC → **File → Settings → Squadrons Telemetry**.
2. **Server URL** defaults to `https://elitesquadrons.com`. If your
   squadron runs its own instance, set this to that URL. A trailing
   slash is fine; it is stripped automatically.
3. Paste your token into **API Token**.
4. Click **OK** to save.

The plugin batches events for up to 10 seconds (or 30 events) before
sending, in requests of at most 50 events / 480 KB each. On a transient
send failure (network error, HTTP 408/429/5xx) unsent events are buffered
locally (capped at 100), in order, and retried after a 30-second back-off.
A permanent rejection (redirect, 401, 403, 413, 426 or any other 4xx)
drops only that batch and logs the reason; later batches are unaffected.
Short-fused carrier events (`CarrierJumpRequest`, `CarrierJumpCancelled`,
`CarrierJump`) skip the buffer window and are flushed immediately.

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
domains the plugin affects, because the server trusts an authenticated
squadron member's first-party events more than anonymous community data:

| Domain          | Highest                          | Notes                                                 |
| --------------- | -------------------------------- | ----------------------------------------------------- |
| BGS faction     | `MANUAL_OVERRIDE` > `MANUAL_ENTRY` > **`SQUAD_TELEMETRY`** > `FRONTIER_CAPI` > `EDDN` > `EDSM` | Telemetry beats EDDN; manual edits beat both.        |
| Galaxy / market | `FRONTIER_CAPI` > `MANUAL` > **`SQUAD_TELEMETRY`** > `EDDN` > `SPANSH` > `EDSM` | cAPI snapshots are still strongest for market data. |
| Carrier live    | **`SQUAD_TELEMETRY`** > `EDDN` > `FRONTIER_CAPI` > `MANUAL` (location, jumps, fuel, decommission) | Live signals belong to telemetry.                    |
| Carrier static  | `MANUAL` / `FRONTIER_CAPI` > `SQUAD_TELEMETRY` (owner, services, displayName) | Curator/cAPI authoritative for slow-changing fields. |

Precedence is set by the server and is not configurable per client.

---

## Troubleshooting

**`Last used` never updates / no events showing up**
- Confirm the Server URL is reachable from your machine
  (`https://<your-instance>/api/health` should return `{ "ok": true }`).
- Confirm the token is the full `sq_…` value with no whitespace.
- Confirm you're not running the Elite Dangerous beta client — beta
  journals are intentionally ignored by the plugin (`is_beta` guard).
- Check EDMC's log via **Help → Open Log Folder** and look for lines
  from `Squadrons Telemetry`.

**`401 Unauthorized` or `403 Forbidden` in the EDMC log**
- `401`: the token was revoked (visible in **Settings → Telemetry
  Clients** in the web app), or was mistyped.
- `403`: the client is no longer authorized — e.g. it was disabled, or
  your squadron membership changed.
- In both cases the plugin drops the current batch and does not keep
  retrying a dead credential. Re-check the client in the Squadrons web
  app, recreate or re-authorize it, paste the new token into the plugin
  settings, and restart EDMC.

**`426 Upgrade Required` in the EDMC log**
- Your plugin version is below the server's minimum. Download the
  latest release from the
  [Releases page](https://github.com/Vintenxe/squadrons-edmc-plugin/releases).

**Market / Outfitting / Shipyard never show up for a station**
- Look for `Market snapshot not forwarded (<reason>)` lines in the EDMC
  log. `file_missing` means EDMC's journal folder setting does not point
  at the folder the game writes to (EDMC → Settings → Configuration →
  E:D journal file location); `stale_snapshot` means the game did not
  rewrite the file for this visit — open the station screen again;
  `marketid_mismatch` / `station_mismatch` mean the file on disk belongs
  to a different station and was correctly refused; `event_too_large` /
  `too_many_items` mean the catalog exceeds the server's bounds and was
  refused whole rather than truncated.
- The snapshot is written when you open the commodities market,
  outfitting or shipyard screen, not merely when you dock.

**`429 Rate limit exceeded`**
- Default is 60 requests/minute per client. Each batch is one request,
  so you should never hit this in normal play.
- If you do, leave the plugin alone for a minute and it will resume.

**Worried about a leaked token?**
- Revoke it from **Settings → Telemetry Clients** — the row is rejected
  immediately on the next request.
- Create a new client with a new token and update the plugin settings.

---

## Versioning & compatibility

The plugin follows [Semantic Versioning](https://semver.org/). Each
release is tagged and published under
[Releases](https://github.com/Vintenxe/squadrons-edmc-plugin/releases);
see [CHANGELOG.md](CHANGELOG.md) for what changed.

Every request includes an `X-Plugin-Version` header so the server can
record the running version per client. Today this is advisory only — no
versions are blocked. Future server releases may reject below a minimum
version with `426 Upgrade Required`; the plugin logs a clear message and
asks you to upgrade when that happens.

---

## Privacy

- The plugin only sends the journal events listed above. It does not
  read your save game, your Frontier login, your friends list, or
  anything outside the EDMC journal stream and the three catalog files
  below.
- Since 1.4.0 the plugin reads three additional local files, read-only
  and only in response to the matching journal event: `Market.json`,
  `Outfitting.json` and `Shipyard.json` in EDMC's configured journal
  folder. Only the inventory fields listed under *Markets, outfitting,
  shipyards* are forwarded; the rest of each file is dropped locally and
  the files are never modified, copied or uploaded as a whole. No other
  file in the journal folder is read.
- Events are scoped to your squadron via your authenticated token.
  Other squadrons cannot see your data.
- Tokens are stored hashed (SHA-256) on the server. Only the prefix is
  visible afterwards.
- Your configured server URL and token are stored locally in
  `<plugin folder>/config.json` in plain text — i.e.
  `…/EDMarketConnector/plugins/SquadronsTelemetry/config.json`.
  Treat that file the way you would treat a saved password: don't
  share the plugin folder or its contents while a token is configured.
  If you need to share logs or hand the folder to someone, revoke the
  token first from the Squadrons web app and create a new one
  afterwards.
- You can revoke a token at any time from the Squadrons web app.

See [SECURITY.md](SECURITY.md) if you need to report a security issue.

---

## Limitations

- A genuinely empty market / outfitting / shipyard (the file lists no
  entries) is not forwarded. The server's full-replacement write ignores
  empty lists rather than clearing previously observed rows, so a station
  that *stopped* selling everything keeps its last observed inventory
  until a newer non-empty snapshot or a higher-precedence source replaces
  it. The freshness note on the Squadrons page shows when the inventory
  was last observed.
- Catalogs above the server's bounds (1024 commodities, 1024 modules, 128
  ships, 64 KB per event) are refused whole. No Squadron Carrier can reach
  these bounds; a few of the largest shipyard stations can exceed the
  module cap, and their outfitting is then simply not forwarded.
- A sidecar file that is still being written when the journal event
  arrives is refused and not re-read; the next time you open that station
  screen a fresh file and a fresh journal event are produced.
- The server applies a snapshot only if nothing fresher or more
  authoritative is already projected for that station and category
  (another member's newer snapshot, or a Frontier cAPI import less than
  24 hours old). Such a snapshot is counted as *skipped* on the server and
  is not an error; the EDMC log shows the server's summary.

---

## License

Released under the [MIT License](LICENSE).
