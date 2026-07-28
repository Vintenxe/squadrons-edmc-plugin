"""
HTTP transport policy for the Squadrons Telemetry plugin.

The telemetry request carries a bearer token, so the transport must never
follow an HTTP redirect: urllib's default behaviour would re-issue the
request — Authorization header included — to whatever destination the
(possibly compromised or misconfigured) server points at, including a
plain-HTTP or cross-origin target. The configured server URL is the final,
canonical endpoint; every 3xx (301/302/303/307/308, same-origin included)
is surfaced as an HTTPError and the batch is handled by the caller's
drop-don't-retry policy.

Kept free of EDMC/tkinter imports so it can be unit-tested standalone
(see test_telemetry_transport.py).
"""

import urllib.error
import urllib.request


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Raise instead of following any redirect; the token is never re-sent."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)

    # Python < 3.11 has no http_error_308 on HTTPRedirectHandler; without a
    # handler a 308 already surfaces as a plain HTTPError (not followed),
    # but define it so behaviour is uniform across interpreter versions.
    def http_error_308(self, req, fp, code, msg, headers):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def build_telemetry_opener() -> urllib.request.OpenerDirector:
    """Opener for telemetry sends — standard handlers, redirects refused."""
    return urllib.request.build_opener(RefuseRedirects)
