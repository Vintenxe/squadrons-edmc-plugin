# Contributing

Thanks for your interest in improving the Squadrons Telemetry EDMC plugin.

This repository is deliberately small. It contains **one** Python file that
is loaded by [EDMarketConnector](https://github.com/EDCD/EDMarketConnector)
as a plugin, plus its documentation. Contributions that keep it that way
are very welcome; contributions that grow it into a framework are not.

## What fits

- Bug fixes, especially around the event buffer, retry logic, and how the
  plugin handles server errors.
- Clarifying or correcting the documentation.
- Small, targeted improvements to error messages or log output.
- Coverage for journal events that the Squadrons server **already
  accepts** and that the plugin is currently missing.

## What does not fit here

- New server-side features. Those belong in the Squadrons backend, not in
  this plugin.
- Packaging systems, signed installers, auto-updaters, or CI pipelines —
  v1.x is intentionally a loose-Python-folder plugin.
- UI frameworks, telemetry dashboards, or anything that pulls in
  dependencies beyond the Python standard library and EDMC's own APIs.
- Forwarding events that the Squadrons server does not understand.

## Reporting bugs

Open a GitHub issue with:

- your plugin version (from the EDMC log line
  `Squadrons Telemetry v… started`),
- your EDMC version and operating system,
- the relevant lines from the EDMC log (`Help → Open Log Folder`), with
  your token redacted.

Do **not** paste your API token into an issue. If you suspect a token has
leaked, revoke it first from **Settings → Telemetry Clients** in the
Squadrons web app.

For security-sensitive reports, see [SECURITY.md](SECURITY.md) instead of
opening a public issue.

## Pull requests

- Keep changes focused and small.
- Match the existing style of `squadrons_telemetry.py` — standard library
  only, module-level `this` object for EDMC state, type hints on public
  functions.
- Update [CHANGELOG.md](CHANGELOG.md) under an `## [Unreleased]` section
  if you change user-visible behaviour.
- If your change affects what events are sent or how tokens are handled,
  call that out in the PR description so the docs can be updated to match.

By submitting a pull request, you agree that your contribution will be
licensed under the repository's [MIT license](LICENSE).
