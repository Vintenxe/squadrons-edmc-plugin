# Changelog

All notable changes to the Squadrons Telemetry EDMC plugin are documented in
this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
