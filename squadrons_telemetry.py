"""
Squadrons Telemetry — EDMC Plugin

Sends Elite Dangerous journal events to a squadron's Elite Squadrons
instance. Each member generates a personal API token in the Squadrons web
app and pastes it into the plugin settings.

Supported events:
    - FSDJump, Location (BGS faction data)
    - Docked (station presence for market/outfitting/shipyard context)
    - Market, Outfitting, Shipyard (full catalog snapshots).  The journal
      line for these events carries only station identity; the inventory
      is read from the ``Market.json`` / ``Outfitting.json`` /
      ``Shipyard.json`` sidecar files in EDMC's configured journal folder
      — the same files EDMC itself reads for EDDN — validated against the
      journal event (MarketID, station, system, timestamp) and reduced to
      the fields the server consumes.  See catalog_snapshots.py.
    - Full fleet-carrier lifecycle:
        CarrierJump, CarrierJumpRequest, CarrierJumpCancelled,
        CarrierLocation, CarrierStats, CarrierDepositFuel,
        CarrierTradeOrder, CarrierNameChanged,
        CarrierDockingPermission, CarrierCrewServices,
        CarrierDecommission, CarrierCancelDecommission
    - Commander stats (balance, loan, current ship, ranks, superpower
      reputation, powerplay, owned ships):
        LoadGame, Rank, Progress, Reputation, Loadout, SuitLoadout,
        Powerplay, StoredShips
    - Community Goals (game-wide initiatives + this commander's
      contribution snapshot):
        CommunityGoal

Install:
    Drop this folder into EDMC's plugins directory, e.g.
    %LOCALAPPDATA%\\EDMarketConnector\\plugins\\SquadronsTelemetry\\
    (equivalents on macOS / Linux are documented in README.md).
    EDMC loads the plugin via the sibling ``load.py`` entrypoint, which
    re-exports the hook functions defined in this module.

Configure:
    EDMC → File → Settings → Squadrons Telemetry
    Paste an API token generated in the Squadrons web app under
    Settings → Telemetry Clients.
"""

import json
import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk
import urllib.error
import urllib.request

# EDMC's theme-aware widget wrappers. Settings tabs must use these so the
# host's light/dark theme engine applies; raw tk widgets parented to
# EDMC's settings Notebook render incorrectly on some platforms.
import myNotebook as nb  # type: ignore[import-not-found]

# Server-URL validation lives in a sibling module with no EDMC imports so
# it can be unit-tested standalone.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from url_policy import (  # noqa: E402
    DEFAULT_SERVER_URL,
    describe_destination,
    normalize_server_url,
)
from telemetry_transport import build_telemetry_opener  # noqa: E402
from catalog_snapshots import (  # noqa: E402
    CatalogChronology,
    load_catalog_snapshot,
)

# Opener used for every telemetry send: refuses all redirects so the bearer
# token can never be forwarded to a redirect target. See telemetry_transport.
_TELEMETRY_OPENER = build_telemetry_opener()

# EDMC plugin API requires these at module level
this = sys.modules[__name__]
this.plugin_name = "Squadrons Telemetry"
this.version = "1.4.0"
this.default_server_url = DEFAULT_SERVER_URL
this.server_url = ""
this.api_token = ""
this.cmdr_name = ""
this.event_buffer: List[Dict[str, Any]] = []
this.buffer_lock = threading.Lock()
# Serialises flushes: the timer thread, an immediate flush and plugin_stop
# may all call _flush_buffer; only one send sequence runs at a time so
# batches reach the server in buffer order and a rebuffered backlog can
# never interleave with a newer flush.
this.flush_lock = threading.Lock()
this.flush_timer: Optional[threading.Timer] = None
this.logger = logging.getLogger(f"{this.plugin_name}")
# Newest catalog snapshot forwarded per (event, MarketID): an older
# observation (replayed journal, clock skew) never follows a newer one.
this.catalog_chronology = CatalogChronology()

# Events we forward to the server.
BGS_EVENTS = {"FSDJump", "Location"}
STATION_EVENTS = {"Docked"}
MARKET_EVENTS = {"Market", "Outfitting", "Shipyard"}
# CarrierJump is listed alongside the lifecycle events because it is the
# only carrier event that also carries Factions[] for BGS.
CARRIER_EVENTS = {
    "CarrierJump",
    "CarrierJumpRequest",
    "CarrierJumpCancelled",
    "CarrierLocation",
    "CarrierStats",
    "CarrierDepositFuel",
    "CarrierTradeOrder",
    "CarrierNameChanged",
    "CarrierDockingPermission",
    "CarrierDecommission",
    "CarrierCancelDecommission",
    "CarrierCrewServices",
}
# Commander-stats events feed the per-member commander profile on the
# Squadrons web app (balance, ranks, current ship, owned ships, etc.).
# They are attributed to the authenticated member identity on the server;
# a legacy global-token configuration cannot ingest them because the data
# is intrinsically per-member.
COMMANDER_EVENTS = {
    "LoadGame",
    "Rank",
    "Progress",
    "Reputation",
    "Loadout",
    "SuitLoadout",
    "Powerplay",
    "StoredShips",
}
# CommunityGoal is the journal event that snapshots every CG the commander
# is currently subscribed to (CurrentGoals[]).  It carries both global
# state (CurrentTotal, NumContributors, TopTier, ...) and the commander's
# personal contribution (PlayerContribution, PlayerPercentileBand, ...) —
# the server uses the authenticated identity to attribute the contribution.
COMMUNITY_GOAL_EVENTS = {"CommunityGoal"}
ALL_EVENTS = (
    BGS_EVENTS
    | STATION_EVENTS
    | MARKET_EVENTS
    | CARRIER_EVENTS
    | COMMANDER_EVENTS
    | COMMUNITY_GOAL_EVENTS
)

# Carrier-lifecycle events that should skip the buffer window and flush
# immediately. Jump intent / cancellation / arrival are short-fused; the
# 10-second buffer would leave the server with stale state when the
# player cancels seconds before the departure window closes.
IMMEDIATE_FLUSH_EVENTS = {
    "CarrierJumpRequest",
    "CarrierJumpCancelled",
    "CarrierJump",
}

# Buffer config
BUFFER_FLUSH_INTERVAL = 10  # seconds
BUFFER_MAX_SIZE = 30
# Server contract: at most 50 events per request (telemetryPayloadSchema).
# A flush that holds more (e.g. after a retry rebuffered a backlog) is sent
# as several sequential requests, never as one over-long batch.
MAX_BATCH_EVENTS = 50
# Cap on locally buffered events across retries, to prevent unbounded
# growth if the server is unreachable for a long time.
BUFFER_RETRY_CAP = 100
# Server matches this as its per-batch byte ceiling (512 KB). We flush early
# if the serialised batch approaches this so we never ship a 413.
BUFFER_MAX_BYTES = 480 * 1024
# On transient failures (network / 5xx / 429), wait at least this long before
# the next flush attempt so we don't hammer a recovering server.
RETRY_BACKOFF_SECONDS = 30


def plugin_start3(plugin_dir: str) -> str:
    """Called by EDMC on startup (Python 3 entry point)."""
    _load_config()
    this.logger.info(f"Squadrons Telemetry v{this.version} started")
    return this.plugin_name


def plugin_stop() -> None:
    """Called by EDMC on shutdown."""
    _cancel_flush_timer()
    _flush_buffer()
    this.logger.info("Squadrons Telemetry stopped")


def plugin_prefs(parent: nb.Notebook, cmdr: str, is_beta: bool) -> nb.Frame:
    """Called by EDMC to build the settings UI.

    Must use ``myNotebook`` widgets and return an ``nb.Frame`` so EDMC can
    theme the tab and mount it into the settings Notebook.
    """
    frame = nb.Frame(parent)

    nb.Label(frame, text="Squadrons Telemetry Settings").grid(
        row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10)
    )

    nb.Label(frame, text="Server URL:").grid(row=1, column=0, sticky=tk.W)
    this._url_var = tk.StringVar(
        value=this.server_url or this.default_server_url
    )
    ttk.Entry(frame, textvariable=this._url_var, width=50).grid(
        row=1, column=1, sticky=tk.W
    )

    nb.Label(frame, text="API Token:").grid(row=2, column=0, sticky=tk.W)
    this._token_var = tk.StringVar(value=this.api_token)
    ttk.Entry(frame, textvariable=this._token_var, width=50, show="*").grid(
        row=2, column=1, sticky=tk.W
    )

    nb.Label(
        frame,
        text=(
            "Generate a token at "
            "https://elitesquadrons.com/settings → Telemetry Clients "
            "(or the equivalent path on your squadron's instance)."
        ),
        wraplength=400,
        justify=tk.LEFT,
    ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))

    # Live destination read-back: the token and journal stream go to this
    # host, so the resolved destination (or a rejection reason) is always
    # visible right in the settings tab.
    this._url_status_var = tk.StringVar(value=_destination_status(this.server_url))
    nb.Label(
        frame,
        textvariable=this._url_status_var,
        wraplength=400,
        justify=tk.LEFT,
    ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))

    nb.Label(
        frame,
        text=f"Plugin version: {this.version}",
    ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))

    return frame


def _destination_status(url: str) -> str:
    if not url:
        return "No server configured — telemetry is not being sent."
    host = describe_destination(url)
    if url == this.default_server_url:
        return f"Telemetry will be sent to: {host} (official server)"
    return (
        f"⚠ Telemetry (including your API token and journal data) will be "
        f"sent to a NON-DEFAULT server: {host}. Only use a server you "
        f"operate or fully trust."
    )


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    """Called by EDMC when settings are saved.

    The server URL is security-critical (it receives the bearer token and
    the journal stream), so it goes through the strict policy in
    url_policy.py. A rejected value keeps the previous configuration.
    """
    normalized, error = normalize_server_url(this._url_var.get())
    if error is not None:
        this.logger.error(f"Rejected server URL: {error}")
        if hasattr(this, "_url_status_var"):
            this._url_status_var.set(f"✗ {error} Keeping previous server.")
        # Restore the entry to the last-known-good value.
        this._url_var.set(this.server_url or this.default_server_url)
    else:
        if normalized != this.server_url:
            this.logger.info(f"Server URL set to {normalized}")
            if normalized != this.default_server_url:
                this.logger.warning(
                    "Telemetry will be sent to a non-default server: "
                    f"{describe_destination(normalized)}"
                )
        this.server_url = normalized
        this._url_var.set(normalized)
        if hasattr(this, "_url_status_var"):
            this._url_status_var.set(_destination_status(normalized))

    this.api_token = this._token_var.get().strip()
    _save_config()
    this.logger.info(f"Settings updated: server={this.server_url}")


def journal_entry(
    cmdr: str,
    is_beta: bool,
    system: str,
    station: str,
    entry: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """Called by EDMC for each journal event."""
    if is_beta:
        return None

    event = entry.get("event", "")
    if event not in ALL_EVENTS:
        return None

    if not this.server_url or not this.api_token:
        return None

    this.cmdr_name = cmdr or this.cmdr_name

    if event in MARKET_EVENTS:
        # The journal line has no inventory.  Read the sidecar snapshot EDMC
        # itself watches, prove it belongs to this exact observation, and
        # forward the normalised result — or forward nothing at all.  A
        # refused snapshot never erases or fabricates inventory server-side
        # because the bare journal line is not sent either.
        snapshot = load_catalog_snapshot(entry, _journal_dir())
        if not snapshot.ok:
            this.logger.info(
                f"{event} snapshot not forwarded ({snapshot.reason}): {snapshot.detail}"
            )
            return None
        if not this.catalog_chronology.is_newer(snapshot.event):
            this.logger.info(
                f"{event} snapshot not forwarded (older_than_forwarded): a newer "
                f"snapshot for MarketID {entry.get('MarketID')} was already sent"
            )
            return None
        this.catalog_chronology.record(snapshot.event)
        this.logger.debug(f"{event} snapshot loaded: {snapshot.detail}")
        entry = snapshot.event

    with this.buffer_lock:
        this.event_buffer.append(entry)

        # Byte-size tripwire: if the serialised buffer is close to the
        # server's per-batch ceiling, flush now instead of waiting for the
        # timer or the count-based cap.  Cheap to compute — one JSON dump
        # on the slow path.
        oversized = False
        try:
            oversized = len(
                json.dumps({"cmdrName": this.cmdr_name, "events": this.event_buffer})
            ) >= BUFFER_MAX_BYTES
        except (TypeError, ValueError):
            oversized = False

        if (
            event in IMMEDIATE_FLUSH_EVENTS
            or len(this.event_buffer) >= BUFFER_MAX_SIZE
            or oversized
        ):
            _schedule_flush(immediate=True)
        else:
            _schedule_flush()

    return None


def _schedule_flush(
    immediate: bool = False,
    delay_override: Optional[float] = None,
) -> None:
    """Schedule a buffer flush.

    ``delay_override`` lets the retry path back off on transient server
    failures so the next attempt doesn't run in the 10-second window.
    """
    _cancel_flush_timer()
    if delay_override is not None:
        delay = delay_override
    elif immediate:
        delay = 0.1
    else:
        delay = BUFFER_FLUSH_INTERVAL
    this.flush_timer = threading.Timer(delay, _flush_buffer)
    this.flush_timer.daemon = True
    this.flush_timer.start()


def _cancel_flush_timer() -> None:
    if this.flush_timer:
        this.flush_timer.cancel()
        this.flush_timer = None


def _journal_dir() -> Optional[str]:
    """EDMC's configured journal folder — the directory EDMC itself watches
    and the one its EDDN integration reads Market.json & co. from.

    Resolved the same way EDMC does (``journaldir`` setting, falling back to
    the platform default) rather than guessing a Windows path.  Returns None
    outside EDMC or when the setting is unavailable; catalog snapshots are
    then refused with ``no_journal_dir`` and everything else keeps working.
    """
    try:
        from config import config  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        configured = config.get_str("journaldir")
        if not configured:
            configured = config.default_journal_dir
    except Exception:
        return None
    if not configured:
        return None
    return os.path.expanduser(str(configured))


def _event_bytes(event: Dict[str, Any]) -> int:
    try:
        return len(json.dumps(event).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _split_batches(events: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split a flush into server-compliant requests.

    Each batch holds at most MAX_BATCH_EVENTS events and stays under
    BUFFER_MAX_BYTES once serialised, so one large (but valid) catalog
    snapshot can never drag unrelated carrier telemetry into a 413 — and a
    backlog rebuffered after an outage is never sent as a single over-long
    request the server would reject outright.
    """
    # Fixed envelope: {"cmdrName": "...", "events": [...]} plus separators.
    overhead = len(json.dumps({"cmdrName": this.cmdr_name, "events": []}).encode("utf-8"))
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_bytes = overhead
    for event in events:
        size = _event_bytes(event) + 2  # ", " separator
        if current and (
            len(current) >= MAX_BATCH_EVENTS
            or current_bytes + size >= BUFFER_MAX_BYTES
        ):
            batches.append(current)
            current = []
            current_bytes = overhead
        current.append(event)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def _flush_buffer() -> None:
    """Send buffered events to the Squadrons API, one compliant batch at a
    time.  A transient failure rebuffers the failed batch AND every batch
    not yet attempted (order preserved) and backs off; a permanent
    rejection drops only the offending batch and the remaining batches are
    still sent.  Flushes are serialised (see this.flush_lock)."""
    with this.flush_lock:
        with this.buffer_lock:
            if not this.event_buffer:
                return
            events = list(this.event_buffer)
            this.event_buffer.clear()

        if not this.server_url or not this.api_token or not this.cmdr_name:
            return

        batches = _split_batches(events)
        for index, batch in enumerate(batches):
            outcome = _send_batch(batch)
            if outcome == "retry":
                pending = [event for later in batches[index:] for event in later]
                _rebuffer(pending)
                _schedule_flush(delay_override=RETRY_BACKOFF_SECONDS)
                return
            if outcome == "rejected" and len(batch) > 1:
                # The server refused the batch as a whole (400 validation
                # failure).  One malformed event must not take the other
                # events down with it: re-send them one at a time so only
                # the offending event(s) are dropped.  Bounded: at most
                # len(batch) extra requests, and a transient failure during
                # the split still rebuffers the remainder in order.
                for single_index, event in enumerate(batch):
                    single = _send_batch([event])
                    if single == "retry":
                        pending = batch[single_index:] + [
                            later_event for later in batches[index + 1:] for later_event in later
                        ]
                        _rebuffer(pending)
                        _schedule_flush(delay_override=RETRY_BACKOFF_SECONDS)
                        return


def _send_batch(events: List[Dict[str, Any]]) -> str:
    """POST one batch.

    Returns "sent", "retry" (transient: rebuffer + back off), "rejected"
    (the server refused this exact payload with 400 — the caller may split
    it and re-send the events individually) or "dropped" (permanent for
    this batch; nothing to retry)."""
    payload = {
        "cmdrName": this.cmdr_name,
        "events": events,
    }

    url = f"{this.server_url}/api/v1/telemetry/ingest"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {this.api_token}",
            "User-Agent": f"SquadronsTelemetry/{this.version} EDMC-Plugin",
            # Advisory today; the server may one day gate below a minimum
            # version via 426 Upgrade Required (handled below).
            "X-Plugin-Version": this.version,
        },
        method="POST",
    )

    try:
        # _TELEMETRY_OPENER refuses every 3xx: the bearer token must never
        # be forwarded to a redirect target (see _RefuseRedirects).
        with _TELEMETRY_OPENER.open(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            _log_server_outcome(result, len(events))
            return "sent"
    except urllib.error.HTTPError as e:
        # 3xx: the configured server redirected the telemetry endpoint.
        # This is a configuration/compromise signal, not a transient fault —
        # retrying would just hit the redirect again, so the batch is
        # dropped. The token was NOT sent to the redirect target. Log only
        # the target hostname (never the full URL, token, or payload).
        if 300 <= e.code < 400:
            target_host = "unknown"
            try:
                from urllib.parse import urlsplit

                location = e.headers.get("Location", "") if e.headers else ""
                target_host = urlsplit(location).hostname or "unknown"
            except Exception:
                pass
            this.logger.error(
                f"Telemetry send failed and batch dropped: the configured "
                f"server responded with a redirect (HTTP {e.code}) toward "
                f"host '{target_host}'. Redirects are refused because the "
                f"request carries your API token. Fix the Server URL in "
                f"settings to the final canonical address."
            )
            return "dropped"
        # 401 / 403: token is rejected or the client is no longer
        # authorized (revoked, disabled, or stripped of its squadron
        # membership). Do not keep retrying — drop this batch so we
        # don't pin the buffer forever on a dead credential.
        # 413: payload too large. Batches are split below the server's
        # ceiling, so this should not happen; if it does, drop only this
        # batch — the remaining batches are still sent.
        # 426: plugin version is below the server's minimum. Same
        # reasoning: retrying won't help until the user updates.
        if e.code in (401, 403, 413, 426):
            reason = {
                401: (
                    "token rejected (401). Revoke and recreate the client "
                    "in the Squadrons web app, then update the plugin."
                ),
                403: (
                    "client no longer authorized (403). Re-check the "
                    "telemetry client in the Squadrons web app "
                    "(Settings → Telemetry Clients) and recreate or "
                    "re-authorize it, then update the plugin."
                ),
                413: (
                    "payload rejected (413 Payload Too Large). The batch "
                    "was dropped; subsequent flushes will be smaller."
                ),
                426: (
                    "plugin version rejected (426 Upgrade Required). "
                    "Download the latest release from "
                    "https://github.com/Vintenxe/squadrons-edmc-plugin/releases."
                ),
            }[e.code]
            this.logger.error(
                f"Telemetry send failed and batch dropped: {reason}"
            )
            return "dropped"
        # 400: the server could not validate this exact payload.  Re-sending
        # identical bytes cannot succeed, and a rebuffered batch at the FRONT
        # of a capped buffer would starve every newer event behind it — but
        # usually only ONE event in the batch is malformed, so the caller
        # splits a multi-event batch and re-sends the events individually.
        if e.code == 400:
            if len(events) > 1:
                this.logger.warning(
                    f"Telemetry batch of {len(events)} events rejected by the "
                    f"server (HTTP 400); re-sending the events individually so "
                    f"only the malformed one is dropped."
                )
                return "rejected"
            this.logger.error(
                f"Telemetry send failed and event dropped: the server rejected "
                f"a {events[0].get('event', '?')} event as invalid (HTTP 400)."
            )
            return "dropped"
        # Other 4xx (404, 405, 415, …) are permanent for this batch and not
        # event-specific.  408 and 429 are the transient exceptions and fall
        # through to the retry path below.
        if 400 <= e.code < 500 and e.code not in (408, 429):
            this.logger.error(
                f"Telemetry send failed and batch dropped: the server "
                f"rejected the batch (HTTP {e.code}). Later batches are "
                f"unaffected."
            )
            return "dropped"
        # 408 / 429 / 5xx: rebuffer and back off so a recovering server
        # isn't hammered by an immediate re-flush timer.
        this.logger.error(
            f"Telemetry send failed (HTTP {e.code}); will retry in "
            f"{RETRY_BACKOFF_SECONDS}s"
        )
        return "retry"
    except Exception as e:
        this.logger.error(
            f"Telemetry send failed ({e}); will retry in "
            f"{RETRY_BACKOFF_SECONDS}s"
        )
        return "retry"


def _log_server_outcome(result: Any, sent: int) -> None:
    """A 2xx is not the same as "everything landed": the server reports
    per-event `processed` / `skipped` and a bounded `errors` list.  Surface
    anything that was not processed so a user reading the EDMC log can see
    why a snapshot did not appear, instead of a silent success."""
    if not isinstance(result, dict):
        this.logger.warning(f"Telemetry sent ({sent} events) but the server reply was not understood.")
        return
    processed = result.get("processed", 0)
    skipped = result.get("skipped", 0)
    errors = result.get("errors") or []
    summary = f"Telemetry sent: {processed} processed, {skipped} skipped of {sent}"
    if errors:
        # Server-side messages are short diagnostics (event type, station,
        # reason); they never contain the token or the payload.
        shown = "; ".join(str(e) for e in errors[:5])
        this.logger.warning(f"{summary}; server reported errors: {shown}")
    elif skipped:
        this.logger.info(summary)
    else:
        this.logger.debug(summary)


def _rebuffer(events: List[Dict[str, Any]]) -> None:
    """Put events back on the front of the buffer for the next flush,
    capping the total to prevent unbounded growth if the server stays
    unreachable."""
    with this.buffer_lock:
        this.event_buffer = (events + this.event_buffer)[:BUFFER_RETRY_CAP]


def _config_path() -> str:
    """Path to the plugin config file."""
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(plugin_dir, "config.json")


def _load_config() -> None:
    """Load config from disk.

    The stored URL is re-validated on every load: a config file edited by
    hand (or written by an older plugin version) must not smuggle in a
    destination the current policy would reject.
    """
    try:
        with open(_config_path(), "r") as f:
            config = json.load(f)
            stored_url = config.get("server_url", "")
            this.api_token = config.get("api_token", "")
    except (FileNotFoundError, json.JSONDecodeError):
        this.server_url = ""
        this.api_token = ""
        return

    if not stored_url:
        this.server_url = ""
        return
    normalized, error = normalize_server_url(stored_url)
    if error is not None:
        this.logger.error(
            f"Stored server URL rejected ({error}) — telemetry disabled "
            "until a valid server is configured in settings."
        )
        this.server_url = ""
    else:
        this.server_url = normalized
        if normalized != this.default_server_url:
            this.logger.warning(
                "Telemetry configured for a non-default server: "
                f"{describe_destination(normalized)}"
            )


def _save_config() -> None:
    """Persist config to disk (owner-only permissions where supported —
    the file contains the API token in plain text)."""
    try:
        path = _config_path()
        with open(path, "w") as f:
            json.dump(
                {"server_url": this.server_url, "api_token": this.api_token},
                f,
                indent=2,
            )
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Best-effort — not meaningful on some Windows filesystems.
            pass
    except OSError as e:
        this.logger.error(f"Failed to save config: {e}")
