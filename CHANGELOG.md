# Changelog

All notable changes to the Squadrons Telemetry EDMC plugin are documented in
this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-11-13

### Fixed
- **Market / Outfitting / Shipyard snapshots were never delivered.** The
  plugin forwarded the bare journal line for these events, which carries
  only station identity (`MarketID`, `StationName`, `StarSystem`) — the
  inventory lives exclusively in the `Market.json` / `Outfitting.json` /
  `Shipyard.json` sidecar files the game writes into the journal folder.
  The server therefore counted every catalog event as `skipped` and the
  Squadron Carrier commerce projection stayed empty. The plugin now reads
  the sidecar file from EDMC's configured journal folder (the same files
  EDMC's own EDDN integration reads) when the matching journal event
  arrives, in a new EDMC-free module `catalog_snapshots.py`.

### Added
- Fail-closed snapshot validation. A file is forwarded only when its
  `event`, `MarketID`, `StationName` and `StarSystem` match the journal
  event and its `timestamp` is within 120 s of it. Missing, empty, partially
  written, malformed, non-UTF-8, over-sized (> 4 MB) or stale files are
  refused with a one-line reason in the EDMC log and nothing is sent — a
  refused snapshot can neither erase nor fabricate inventory, because the
  bare journal line is not sent either. A missing inventory array is never
  treated as an empty inventory.
- Normalisation to the server contract. Market `Items[]` are reduced to
  `Name` (canonical symbol, e.g. `gold` — the language-neutral identity
  EDDN uses; the localised name is NOT sent, so two commanders playing in
  different languages produce one commodity identity), `BuyPrice`,
  `SellPrice`, `Stock`, `Demand`, `MeanPrice`; Outfitting `Items[]` objects
  become the canonical lower-case module list (`Modules: string[]`, free
  `int_planetapproachsuite` excluded); Shipyard `PriceList[]` (also
  `Pricelist` / `ShipyardList`) becomes `PriceList[{ShipType}]`. Nothing
  else from the files (`id`, `Category`, `Rare`, `Consumer`/`Producer`,
  `ShipPrice`, `Horizons`, …) leaves the machine.
- Bounded sizes, never truncation: a snapshot over the server's per-array
  caps (1024 commodities / 1024 modules / 128 ships) or over the 64 KB
  per-event ceiling is refused whole with an explicit log line. Measured
  realistic worst cases (every commodity in the game ≈ 55 KB, a
  Jameson-scale outfitting board ≈ 33 KB) fit the existing contract.
- Per-station chronology guard: an older snapshot for the same
  (event, MarketID) is never forwarded after a newer one.
- Both the journal event and the sidecar file must name the station and the
  system (`StationName`, `StarSystem`) and agree; otherwise the snapshot is
  refused (`missing_station_identity`).
- Outfitting admission mirrors the EDDN outfitting/2 schema exactly
  (`^hpt_` / `^int_` / `_armour_`, any case; `int_planetapproachsuite`
  excluded): paintjobs, decals, bobbleheads, nameplates and ship kits that
  the game lists on the same screen are omitted as EDDN requires, every
  real module is kept, and a module entry without a usable name refuses the
  whole snapshot.
- The server's per-event outcome (`processed` / `skipped` / `errors`) is now
  surfaced in the EDMC log at INFO/WARNING instead of DEBUG, so a snapshot
  that the server declined is visible with its reason.

### Changed
- Flushes are split into server-compliant requests (≤ 50 events and
  < 480 KB each). A backlog rebuffered after an outage is no longer sent
  as one over-long request, and one large catalog event can never drag
  unrelated carrier telemetry into a 413.
- A transient failure (network error, 408, 429, 5xx) rebuffers the failed
  batch *and* every batch not yet attempted, in order, then backs off as
  before. A permanent rejection (3xx redirect, 401, 403, 413, 426 and any
  other 4xx) drops only the offending batch; later batches are still sent.
  Previously a 400 would be retried forever at the front of the capped
  buffer and starve every newer event behind it.
- A 400 for a multi-event batch no longer discards the whole batch: the
  events are re-sent one at a time (bounded, in order) so only the event
  the server cannot validate is dropped.
- Flushes are serialised: the timer-driven flush, the immediate flush for
  short-fused carrier events and the shutdown flush can no longer run
  concurrently and deliver batches out of buffer order.

### Privacy
- The plugin now reads three additional local files: `Market.json`,
  `Outfitting.json` and `Shipyard.json` in EDMC's journal folder, read-only,
  only in response to the matching journal event, and only the fields
  listed above are forwarded. See README → Privacy.

### Compatibility
- Requires the Elite-Squadrons backend released alongside this version
  (Elite-Squadrons PR #395) to be deployed first. That backend applies a
  forwarded snapshot as one atomic full replacement under the station row
  lock, with per-category source precedence and observation chronology,
  and refuses over-cap or normalised-empty snapshots; an older backend
  accepts the same payload (`Items[]`, `Modules: string[]`,
  `PriceList[]`) but applies it with its previous non-atomic,
  receipt-time writers. Plugin 1.3.2 keeps working unchanged against the
  new backend, so the backend is deployed before this plugin is published.
- `config.json` (`server_url`, `api_token`) is unchanged; existing
  credentials are preserved across the upgrade.
- Requires EDMC 5.x on Python 3.9+ (standard library only, as before).

## [1.3.2] - 2026-07-25

### Security
- Telemetry sends now REFUSE every HTTP redirect (301/302/303/307/308,
  same-origin included) via a dedicated opener (`telemetry_transport.py`).
  Previously urllib's default redirect handling would re-send the request
  — bearer token included — to whatever `Location` the server returned,
  so a compromised or misconfigured endpoint could exfiltrate the token
  to another host. A redirect now drops the batch with a clear
  configuration error naming only the redirect target's hostname; it is
  never retried in a loop.

### Release note
- Published as the standalone `Vintenxe/squadrons-edmc-plugin` v1.3.2
  release on 2026-07-28 (the 1.3.1 URL policy and the 1.3.2 redirect
  refusal together). Earlier notes stating that 1.3.1/1.3.2 remained
  unpublished are superseded.

## [1.3.1] - 2026-07-24

### Security
- The server URL is now strictly validated (new `url_policy.py`, covered
  by unit tests): HTTPS is required for every non-loopback host, plain
  HTTP is allowed only for `localhost`/`127.0.0.0/8`/`[::1]` development
  targets, embedded credentials / query strings / fragments are rejected,
  Unicode hostnames are IDNA-normalized, and bare hosts are upgraded to
  `https://`. Previously any string was accepted after a trim, so a
  mistyped or malicious URL could exfiltrate the bearer token and journal
  stream over plain HTTP to an arbitrary host.
- A stored `config.json` server URL is re-validated on every load;
  invalid values disable telemetry until reconfigured.
- The settings tab now always displays the exact destination host, with
  an explicit warning when it is not the official server.
- `config.json` is written with owner-only (0600) permissions where the
  filesystem supports it.

## [1.3.0] - 2026-04-25

### Added
- Forward `CommunityGoal` journal events. The Squadrons server uses these
  to populate the new Community Goals dashboard with both game-wide
  initiative state (target/tier/expiry/contributors) and per-commander
  contribution snapshots. Member contribution is attributed to the
  authenticated telemetry client; the legacy global key updates global
  state only.

### Compatibility
- Requires server build from 2026-04-25 or later. Earlier server builds
  ignore the unknown event type with no failure mode (counted as
  `skipped`), so it is safe to update the plugin first.

## [1.1.2] - 2026-04-22

### Changed
- Flush now trips on a byte-size threshold (≈480 KB) in addition to the
  count and immediate-flush rules, so the plugin never ships a batch that
  the server will reject with HTTP 413.
- Transient failures (network errors, HTTP 429, 5xx) rebuffer and back off
  for 30 seconds before retrying so a recovering server is not hammered
  by the 10-second flush loop.
- HTTP 413 from the server now drops the offending batch (rather than
  retrying indefinitely) and logs a clear reason.

### Compatibility
- Server version from 2026-04-22 reports per-client health counters
  (accepted / rejected / last error code). No plugin change required to
  benefit — just update to pick up the client-side hardening above.

## [1.1.1] - 2026-04-22

### Changed
- Server URL now defaults to `https://elitesquadrons.com` on first run.
- Settings UI now points at the real token-management page instead of a
  placeholder URL.
- Server URL entered in settings is now whitespace-trimmed in addition
  to having a trailing slash stripped, so accidentally pasted leading
  or trailing spaces no longer produce a broken URL.

### Fixed
- EDMC compatibility: added a `load.py` entrypoint at the plugin root
  that re-exports the EDMC hook functions from `squadrons_telemetry`.
  Without this file, EDMC marks the plugin as broken on startup, and
  even when the module loads it cannot discover `plugin_prefs` /
  `prefs_changed`, so no **Squadrons Telemetry** settings tab appears
  in **File → Settings**. With `load.py` in place, the plugin loads
  cleanly and its settings tab is exposed.
- Settings tab construction: switched the editable fields in
  `plugin_prefs` from `nb.Entry` (which does not exist in EDMC's
  `myNotebook`) to `ttk.Entry`, while keeping `nb.Frame` / `nb.Label`
  for theming. Without this, EDMC raised
  `AttributeError: module 'myNotebook' has no attribute 'Entry'`
  while building the settings tab and the tab did not render.
- A revoked or invalid token (HTTP `401`) no longer causes the event buffer
  to retry the same batch indefinitely. The batch is dropped and a clear
  message is written to the EDMC log.
- HTTP `403 Forbidden` is now handled the same way as `401`: the batch is
  dropped and the log asks the user to review / recreate / re-authorize
  the telemetry client in the Squadrons web app.
- HTTP `426 Upgrade Required` responses are now handled explicitly: the
  batch is dropped and the log explains that a newer plugin version is
  required.

## [1.1.0] - Initial public release baseline

### Added
- Full fleet-carrier lifecycle coverage (11 new event types):
  `CarrierJumpRequest`, `CarrierJumpCancelled`, `CarrierLocation`,
  `CarrierStats`, `CarrierDepositFuel`, `CarrierTradeOrder`,
  `CarrierNameChanged`, `CarrierDockingPermission`, `CarrierCrewServices`,
  `CarrierDecommission`, `CarrierCancelDecommission`.
- `X-Plugin-Version` header on every request so the server can record the
  plugin version per client.
- Immediate-flush path for short-fused carrier events (`CarrierJumpRequest`,
  `CarrierJumpCancelled`, `CarrierJump`) so cancellations and departures
  are not stuck behind the 10-second buffer window.

## [1.0.0] - Initial release

### Added
- Forwarding of `FSDJump`, `Location`, `Docked`, `Market`, `Outfitting`,
  `Shipyard`, and `CarrierJump` events to
  `POST /api/v1/telemetry/ingest`.
- Per-device API token authentication.
- Local event batching (10 seconds or 30 events) with re-buffer on send
  failure.
- Beta-journal guard: events from the Elite Dangerous beta client are
  ignored.
