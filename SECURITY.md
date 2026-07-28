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
  The plugin sets owner-only permissions (0600) on the file where the
  filesystem supports it.
- The token is sent in the `Authorization: Bearer …` header to the server
  you configured. Since v1.3.1 the plugin **enforces** HTTPS for every
  non-loopback server URL (plain HTTP is accepted only for
  `localhost`/`127.0.0.0/8`/`[::1]` development targets), rejects URLs
  with embedded credentials, query strings, or fragments, and normalizes
  Unicode hostnames to their punycode form. The settings tab always shows
  the exact host telemetry will be sent to, with a warning when it is not
  the official server.
- If you change the server URL, you are trusting that server's operator
  with your token and journal stream. Revoke the token before pointing an
  existing configuration at a different server.
- Since v1.3.2 the telemetry request also **refuses every HTTP redirect**
  (301/302/303/307/308, same-origin included): the configured URL is the
  final canonical endpoint, and the token is never re-sent to a
  `Location` target. A redirecting server surfaces as a configuration
  error in the EDMC log.
- On the server side, tokens are stored hashed (SHA-256); the plaintext
  value is shown to you **once** when you create the client and cannot be
  recovered afterwards.
- If you think your token has leaked, revoke it from
  **Settings → Telemetry Clients** in the Squadrons web app and create a
  new one. Revocation takes effect on the next request.
