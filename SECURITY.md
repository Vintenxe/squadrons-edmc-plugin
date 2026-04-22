# Security

## Reporting a vulnerability

If you believe you have found a security issue in this plugin — anything
that could expose a token, let another squadron see data that isn't theirs,
or let someone impersonate a client — please **do not open a public GitHub
issue**.

Instead, email the maintainers at **security@elitesquadrons.com** with:

- a description of the issue,
- steps to reproduce, and
- the plugin version you tested against (shown as `v1.x.y` in the EDMC log
  line `Squadrons Telemetry v… started`).

You should receive an acknowledgement within a few days. We will coordinate
a fix and a disclosure timeline with you.

## Scope

In scope:

- The plugin code in this repository (`squadrons_telemetry.py`).
- Any credential or data-handling defect introduced by this plugin
  specifically.

Out of scope (please report to the relevant project instead):

- Issues in EDMarketConnector itself.
- Issues in the Squadrons server API — report via the Squadrons web app's
  normal support channel.
- Anonymous third-party data pipelines (EDDN, EDSM, Spansh, etc.).

## Handling of secrets

- Your API token is stored in plain text in the plugin folder's
  `config.json`. Treat that file the way you would treat a saved password.
- The token is sent over HTTPS to the Squadrons instance you configured,
  in the `Authorization: Bearer …` header.
- On the server side, tokens are stored hashed (SHA-256); the plaintext
  value is shown to you **once** when you create the client and cannot be
  recovered afterwards.
- If you think your token has leaked, revoke it from
  **Settings → Telemetry Clients** in the Squadrons web app and create a
  new one. Revocation takes effect on the next request.
