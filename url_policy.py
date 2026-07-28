"""
Server-URL validation policy for the Squadrons Telemetry plugin.

The configured server URL receives a live bearer token plus the
commander's full journal stream, so it must never silently accept a
destination the user didn't clearly choose:

  * HTTPS is required for every non-loopback host — a plain-HTTP URL
    would leak the token to any on-path observer.
  * Plain HTTP is allowed only for explicit development loopback hosts
    (localhost / 127.0.0.0/8 / [::1]).
  * Embedded credentials (user:pass@host) are rejected outright.
  * Query strings and fragments are rejected — they have no meaning for
    an API base URL and would corrupt the ingest path.
  * Hostnames are IDNA-normalized and lowercased so a visually-confusable
    Unicode hostname can't masquerade as the default server.
  * A bare host ("squadrons.example.org") is upgraded to https:// rather
    than rejected, preserving legitimate self-hosted deployments.

This module is deliberately free of EDMC/tkinter imports so it can be
unit-tested standalone (see test_url_policy.py).
"""

from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

DEFAULT_SERVER_URL = "https://elitesquadrons.com"

_LOOPBACK_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}


def _is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    if host == "::1" or host == "[::1]":
        return True
    # 127.0.0.0/8
    parts = host.split(".")
    if len(parts) == 4 and parts[0] == "127":
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False
    return False


def normalize_server_url(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate and normalize a user-supplied server URL.

    Returns ``(normalized_url, None)`` on success or ``(None, reason)``
    when the value must be rejected. An empty value normalizes to the
    default production URL.
    """
    value = (raw or "").strip()
    if not value:
        return DEFAULT_SERVER_URL, None

    # A bare host without a scheme is a common paste; default it to HTTPS
    # rather than letting urlsplit treat it as a relative path.
    if "://" not in value:
        value = f"https://{value}"

    try:
        parts = urlsplit(value)
    except ValueError:
        return None, "Server URL could not be parsed."

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None, f"Unsupported URL scheme '{scheme}' — use https://."

    if parts.username is not None or parts.password is not None:
        return None, "Server URL must not contain embedded credentials."

    if parts.query:
        return None, "Server URL must not contain a query string."
    if parts.fragment:
        return None, "Server URL must not contain a #fragment."

    host = parts.hostname
    if not host:
        return None, "Server URL is missing a hostname."

    # IDNA-normalize so Unicode lookalike hostnames become their true
    # punycode form (or are rejected as invalid).
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None, "Server URL hostname is not a valid internet hostname."

    if scheme == "http" and not _is_loopback_host(host):
        return None, (
            "Plain http:// is only allowed for localhost development. "
            "Use https:// for every other server."
        )

    try:
        port = parts.port
    except ValueError:
        return None, "Server URL has an invalid port."

    # urlsplit strips the brackets from IPv6 literals — restore them.
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", "")), None


def describe_destination(url: str) -> str:
    """Human-readable destination host for display in the settings UI."""
    try:
        host = urlsplit(url).hostname or url
    except ValueError:
        return url
    return host
