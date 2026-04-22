"""
Squadrons Telemetry — EDMC Plugin

Sends journal events from Elite Dangerous to the Squadrons telemetry API.
Each member generates a personal API token in the Squadrons web app and
enters it in the plugin settings.

Supported events:
- FSDJump, Location, CarrierJump (faction influence / BGS data)
- Docked (station presence for market/outfitting/shipyard context)
- Market, Outfitting, Shipyard (full data from companion API via EDMC)
- Full fleet carrier lifecycle:
    CarrierJumpRequest, CarrierJumpCancelled, CarrierLocation,
    CarrierStats, CarrierDepositFuel, CarrierTradeOrder,
    CarrierNameChanged, CarrierDockingPermission,
    CarrierDecommission, CarrierCancelDecommission,
    CarrierCrewServices

Install:
  Copy this folder into %LOCALAPPDATA%\\EDMarketConnector\\plugins\\
  (or the equivalent on Mac/Linux)

Configure:
  Open EDMC → File → Settings → Squadrons Telemetry
  Paste your API token from the Squadrons web app.
"""

import json
import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk

# EDMC plugin API requires these at module level
this = sys.modules[__name__]
this.plugin_name = "Squadrons Telemetry"
this.version = "1.1.0"
this.server_url = ""
this.api_token = ""
this.cmdr_name = ""
this.event_buffer: List[Dict[str, Any]] = []
this.buffer_lock = threading.Lock()
this.flush_timer: Optional[threading.Timer] = None
this.logger = logging.getLogger(f"{this.plugin_name}")

# Events we want to forward
BGS_EVENTS = {"FSDJump", "Location"}
STATION_EVENTS = {"Docked"}
MARKET_EVENTS = {"Market", "Outfitting", "Shipyard"}
# Full carrier lifecycle.  The backend handles all of these via
# src/lib/carrier-events.ts; CarrierJump is still the only one that also
# carries Factions[] for BGS, so it's listed here alongside the lifecycle
# events rather than under BGS_EVENTS.
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
# immediately.  Jump intent / cancellation / arrival are short-fused; a
# 10-second buffer is enough to leave the backend with stale state when
# the player cancels seconds before the departure window closes.
IMMEDIATE_FLUSH_EVENTS = {
    "CarrierJumpRequest",
    "CarrierJumpCancelled",
    "CarrierJump",
}

# Buffer config
BUFFER_FLUSH_INTERVAL = 10  # seconds
BUFFER_MAX_SIZE = 30


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
    """Called by EDMC to build settings UI."""
    frame = tk.Frame(parent)

    tk.Label(frame, text="Squadrons Telemetry Settings").grid(
        row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10)
    )

    tk.Label(frame, text="Server URL:").grid(row=1, column=0, sticky=tk.W)
    this._url_var = tk.StringVar(value=this.server_url)
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
        text="Generate a token at: <your-squadrons-url>/settings → Telemetry",
        fg="gray",
    ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(5, 0))

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

    try:
        import urllib.request

        url = f"{this.server_url}/api/v1/telemetry/ingest"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {this.api_token}",
                "User-Agent": f"SquadronsTelemetry/{this.version} EDMC-Plugin",
                # Advisory for the backend today; future releases may gate
                # below a minimum version via 426 Upgrade Required.
                "X-Plugin-Version": this.version,
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            this.logger.debug(
                f"Telemetry sent: {result.get('processed', 0)} processed, "
                f"{result.get('skipped', 0)} skipped"
            )
    except Exception as e:
        this.logger.error(f"Telemetry send failed: {e}")
        # Re-buffer events on failure for retry
        with this.buffer_lock:
            this.event_buffer = events + this.event_buffer
            # Cap buffer to prevent unbounded growth
            this.event_buffer = this.event_buffer[:100]


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
