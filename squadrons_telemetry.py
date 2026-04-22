"""
Squadrons Telemetry — EDMC Plugin

Sends Elite Dangerous journal events to a squadron's Elite Squadrons
instance. Each member generates a personal API token in the Squadrons web
app and pastes it into the plugin settings.

Supported events:
    - FSDJump, Location (BGS faction data)
    - Docked (station presence for market/outfitting/shipyard context)
    - Market, Outfitting, Shipyard (full snapshots via EDMC)
    - Full fleet-carrier lifecycle:
        CarrierJump, CarrierJumpRequest, CarrierJumpCancelled,
        CarrierLocation, CarrierStats, CarrierDepositFuel,
        CarrierTradeOrder, CarrierNameChanged,
        CarrierDockingPermission, CarrierCrewServices,
        CarrierDecommission, CarrierCancelDecommission

Install:
    Drop this folder into EDMC's plugins directory, e.g.
    %LOCALAPPDATA%\\EDMarketConnector\\plugins\\SquadronsTelemetry\\
    (equivalents on macOS / Linux are documented in README.md).

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
from tkinter import ttk  # noqa: F401  (kept for plugins that subclass)
import urllib.error
import urllib.request

# EDMC plugin API requires these at module level
this = sys.modules[__name__]
this.plugin_name = "Squadrons Telemetry"
this.version = "1.1.1"
this.default_server_url = "https://elitesquadrons.com"
this.server_url = ""
this.api_token = ""
this.cmdr_name = ""
this.event_buffer: List[Dict[str, Any]] = []
this.buffer_lock = threading.Lock()
this.flush_timer: Optional[threading.Timer] = None
this.logger = logging.getLogger(f"{this.plugin_name}")

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
ALL_EVENTS = BGS_EVENTS | STATION_EVENTS | MARKET_EVENTS | CARRIER_EVENTS

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
# Cap on locally buffered events across retries, to prevent unbounded
# growth if the server is unreachable for a long time.
BUFFER_RETRY_CAP = 100


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


def plugin_prefs(parent: tk.Tk, cmdr: str, is_beta: bool) -> Optional[tk.Frame]:
    """Called by EDMC to build the settings UI."""
    frame = tk.Frame(parent)

    tk.Label(frame, text="Squadrons Telemetry Settings").grid(
        row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10)
    )

    tk.Label(frame, text="Server URL:").grid(row=1, column=0, sticky=tk.W)
    this._url_var = tk.StringVar(
        value=this.server_url or this.default_server_url
    )
    tk.Entry(frame, textvariable=this._url_var, width=50).grid(
        row=1, column=1, sticky=tk.W
    )

    tk.Label(frame, text="API Token:").grid(row=2, column=0, sticky=tk.W)
    this._token_var = tk.StringVar(value=this.api_token)
    tk.Entry(frame, textvariable=this._token_var, width=50, show="*").grid(
        row=2, column=1, sticky=tk.W
    )

    tk.Label(
        frame,
        text=(
            "Generate a token at "
            "https://elitesquadrons.com/settings → Telemetry Clients "
            "(or the equivalent path on your squadron's instance)."
        ),
        fg="gray",
        wraplength=400,
        justify=tk.LEFT,
    ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))

    tk.Label(
        frame,
        text=f"Plugin version: {this.version}",
        fg="gray",
    ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))

    return frame


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    """Called by EDMC when settings are saved."""
    this.server_url = this._url_var.get().rstrip("/")
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

    with this.buffer_lock:
        this.event_buffer.append(entry)

        if (
            event in IMMEDIATE_FLUSH_EVENTS
            or len(this.event_buffer) >= BUFFER_MAX_SIZE
        ):
            _schedule_flush(immediate=True)
        else:
            _schedule_flush()

    return None


def _schedule_flush(immediate: bool = False) -> None:
    """Schedule a buffer flush."""
    _cancel_flush_timer()
    delay = 0.1 if immediate else BUFFER_FLUSH_INTERVAL
    this.flush_timer = threading.Timer(delay, _flush_buffer)
    this.flush_timer.daemon = True
    this.flush_timer.start()


def _cancel_flush_timer() -> None:
    if this.flush_timer:
        this.flush_timer.cancel()
        this.flush_timer = None


def _flush_buffer() -> None:
    """Send buffered events to the Squadrons API."""
    with this.buffer_lock:
        if not this.event_buffer:
            return
        events = list(this.event_buffer)
        this.event_buffer.clear()

    if not this.server_url or not this.api_token or not this.cmdr_name:
        return

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
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            this.logger.debug(
                f"Telemetry sent: {result.get('processed', 0)} processed, "
                f"{result.get('skipped', 0)} skipped"
            )
    except urllib.error.HTTPError as e:
        # 401: token revoked or invalid. Do not keep retrying — drop
        # this batch so we don't pin the buffer forever on a dead token.
        # 426: plugin version is below the server's minimum. Same
        # reasoning: retrying won't help until the user updates.
        if e.code in (401, 426):
            reason = {
                401: (
                    "token rejected (401). Revoke and recreate the client "
                    "in the Squadrons web app, then update the plugin."
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
            return
        this.logger.error(
            f"Telemetry send failed (HTTP {e.code}); will retry on next flush"
        )
        _rebuffer(events)
    except Exception as e:
        this.logger.error(
            f"Telemetry send failed ({e}); will retry on next flush"
        )
        _rebuffer(events)


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
    """Load config from disk."""
    try:
        with open(_config_path(), "r") as f:
            config = json.load(f)
            this.server_url = config.get("server_url", "")
            this.api_token = config.get("api_token", "")
    except (FileNotFoundError, json.JSONDecodeError):
        this.server_url = ""
        this.api_token = ""


def _save_config() -> None:
    """Persist config to disk."""
    try:
        with open(_config_path(), "w") as f:
            json.dump(
                {"server_url": this.server_url, "api_token": this.api_token},
                f,
                indent=2,
            )
    except OSError as e:
        this.logger.error(f"Failed to save config: {e}")
