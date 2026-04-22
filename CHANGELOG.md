# Changelog

All notable changes to the Squadrons Telemetry EDMC plugin are documented in
this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] - 2026-04-22

### Changed
- Server URL now defaults to `https://elitesquadrons.com` on first run.
- Settings UI now points at the real token-management page instead of a
  placeholder URL.
- Server URL entered in settings is now whitespace-trimmed in addition
  to having a trailing slash stripped, so accidentally pasted leading
  or trailing spaces no longer produce a broken URL.

### Fixed
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
