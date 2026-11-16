"""
Market / Outfitting / Shipyard snapshot loading for the Squadrons Telemetry
EDMC plugin.

Elite Dangerous writes three sidecar files into the journal folder each
time the relevant station screen is opened: ``Market.json``,
``Outfitting.json`` and ``Shipyard.json``.  The matching journal line
(``{"event":"Market", ...}``) carries ONLY station identity — the inventory
lives exclusively in the sidecar file.  EDMarketConnector's own EDDN
integration therefore re-reads ``<journaldir>/<Event>.json`` when it sees
one of those journal events; this module does the same for the Squadrons
telemetry stream.

Everything here is fail-closed.  A snapshot is forwarded ONLY when:

  * the sidecar file exists, is non-empty, is below a hard size bound, and
    parses as a JSON object (a partially written file fails JSON parsing
    and is refused — never retried in a loop);
  * the file's ``event`` matches the journal event;
  * the file's ``MarketID`` equals the journal event's ``MarketID`` — one
    station's file is never attached to another station;
  * both the journal event and the file name the station (``StationName``)
    and the system (``StarSystem``) and the two agree — the server keys the
    projection by (system, station), so an observation that cannot name
    them is not forwarded;
  * the file's ``timestamp`` is within a short window of the journal
    event's timestamp (a file left over from an earlier docking at the
    same station is stale and refused);
  * the inventory array is present, is a list, and every entry is well
    formed.  A missing array is NOT an empty inventory — nothing is
    forwarded, nothing is fabricated;
  * the normalised event stays under the server's per-event byte ceiling
    and the server's per-array caps.  Over-cap snapshots are refused
    whole rather than truncated: the server treats each catalog event as a
    full replacement, so a truncated snapshot would delete real rows.

Only the fields the Squadrons ingest contract consumes are forwarded.
Everything else in the sidecar file (``id``, ``Category``, ``Rare``,
``Consumer``/``Producer`` flags, ``Name_Localised``, ``ShipPrice``,
``Horizons``, …) is dropped locally.  Commodity identity is the
language-neutral symbol ("gold"), exactly as EDDN publishes it, so two
commanders running the game in different languages produce one identity.

Outfitting follows the EDDN outfitting/2 schema contract verbatim: the
``modules`` list admits only names matching ``^hpt_`` / ``^int_`` /
``_armour_`` (any case) and excludes ``int_planetapproachsuite``.  Items the
game lists on the same screen that are NOT ship modules — paintjobs,
decals, bobbleheads, nameplates, ship kits, i.e. everything outside that
pattern — are deliberately omitted, as EDDN requires; the resulting list is
the complete set of purchasable modules, not a partial one.  A module entry
that is malformed (no ``Name`` / non-string ``Name``) refuses the whole
snapshot instead.

Kept free of EDMC / tkinter imports so it can be unit-tested standalone
(see test_catalog_snapshots.py).  Standard library only.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Journal events that have a sidecar inventory file.
CATALOG_EVENTS = ("Market", "Outfitting", "Shipyard")

# Journal event → sidecar file name (exact case, as written by the game).
CATALOG_FILE_NAMES: Dict[str, str] = {
    "Market": "Market.json",
    "Outfitting": "Outfitting.json",
    "Shipyard": "Shipyard.json",
}

# Hard bound on the sidecar file we are willing to read.  Real files are
# well under 1 MB (a ~1,300-module Outfitting.json is ~100 KB); anything
# larger is not a game-written snapshot and is refused without reading.
MAX_SNAPSHOT_FILE_BYTES = 4 * 1024 * 1024

# The sidecar file and the journal line are written by the game at the
# same moment; their timestamps normally agree to the second.  A file whose
# timestamp is further than this from the journal event is a leftover from
# a previous visit (or a clock anomaly) and is refused as stale.
SNAPSHOT_TIMESTAMP_TOLERANCE_SECONDS = 120

# Server contract (src/app/api/v1/telemetry/ingest/route.ts):
#   MAX_EVENT_BYTES = 64 KB per serialised event,
#   MAX_ITEMS / MAX_MODULES = 1024, MAX_SHIPS = 128.
# A catalog event that would exceed any of these is refused locally with a
# clear log line instead of being shipped and silently skipped.
CATALOG_EVENT_MAX_BYTES = 64 * 1024
MAX_MARKET_ITEMS = 1024
MAX_OUTFITTING_MODULES = 1024
MAX_SHIPYARD_SHIPS = 128

# Journal `Name` values in Market.json look like "$gold_name;".  The
# canonical commodity symbol is the part between the sigil and the
# "_name;" suffix (same rule EDMC applies for EDDN).
_COMMODITY_SYMBOL_RE = re.compile(r"^\$(.+)_name;$", re.IGNORECASE)

# Canonical module identifiers (lower-case journal internal names such as
# "hpt_pulselaser_fixed_small", "int_shieldgenerator_size3_class5" or
# "sidewinder_armour_grade1").  Admission mirrors the EDDN outfitting/2
# schema pattern `(^Hpt_|^hpt_|^Int_|^int_|_Armour_|_armour_)`: anything
# else on the outfitting screen (paintjobs, decals, bobbleheads, nameplates,
# ship kits) is not a ship module and is omitted, exactly as EDDN requires.
_MODULE_NAME_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_MODULE_NAME_PREFIXES = ("hpt_", "int_")
_MODULE_NAME_ARMOUR_MARKER = "_armour_"
# Free, non-purchasable module that the game lists in every Outfitting.json.
# EDMC's EDDN integration excludes it for the same reason.
_EXCLUDED_MODULES = frozenset({"int_planetapproachsuite"})

# Canonical ship identifiers: the journal's internal `ShipType` token,
# e.g. "sidewinder", "cobramkiii", "type9", "krait_mkii".
_SHIP_TYPE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

# Shipyard.json carries the ship list as `PriceList` (journal manual).  EDMC
# reads the same key as `Pricelist`; accept the documented spellings only.
_SHIPYARD_LIST_KEYS = ("PriceList", "Pricelist", "ShipyardList")


class SnapshotResult:
    """Outcome of one snapshot load.

    ``ok`` is True only when ``event`` holds a fully validated, normalised
    journal event ready to buffer.  Otherwise ``reason`` is a short
    machine code and ``detail`` a human-readable explanation for the log.
    """

    __slots__ = ("ok", "event", "reason", "detail", "count", "bytes")

    def __init__(
        self,
        ok: bool,
        event: Optional[Dict[str, Any]] = None,
        reason: str = "",
        detail: str = "",
        count: int = 0,
        size: int = 0,
    ) -> None:
        self.ok = ok
        self.event = event
        self.reason = reason
        self.detail = detail
        self.count = count
        self.bytes = size


def _refuse(reason: str, detail: str) -> SnapshotResult:
    return SnapshotResult(False, None, reason, detail)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python; the journal never uses booleans
    # for prices or counts, so exclude it explicitly.
    return isinstance(value, int) and not isinstance(value, bool)


def parse_journal_timestamp(value: Any) -> Optional[datetime]:
    """Parse the journal's ``YYYY-MM-DDTHH:MM:SSZ`` timestamp (UTC)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
            return parsed.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_commodity_symbol(name: str) -> str:
    """"$gold_name;" → "gold"; already-canonical values pass through."""
    match = _COMMODITY_SYMBOL_RE.match(name.strip())
    symbol = match.group(1) if match else name.strip()
    return symbol.lower()


def canonical_module_name(name: Any) -> Optional[str]:
    """Return the canonical module identifier, or None when the value is
    not a purchasable module name."""
    if not isinstance(name, str):
        return None
    token = name.strip().lower()
    if not _MODULE_NAME_RE.match(token):
        return None
    if token in _EXCLUDED_MODULES:
        return None
    if token.startswith(_MODULE_NAME_PREFIXES) or _MODULE_NAME_ARMOUR_MARKER in token:
        return token
    return None


def canonical_ship_type(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if not _SHIP_TYPE_RE.match(token):
        return None
    return token


# ─── File access ────────────────────────────────────────────────────────────


def _read_snapshot_file(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[SnapshotResult]]:
    """Read and parse one sidecar file with hard bounds.

    Returns ``(data, None)`` on success or ``(None, refusal)``.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, _refuse("file_missing", f"{os.path.basename(path)} is not present in the journal folder")
    if size <= 0:
        return None, _refuse("file_empty", f"{os.path.basename(path)} is empty")
    if size > MAX_SNAPSHOT_FILE_BYTES:
        return None, _refuse(
            "file_too_large",
            f"{os.path.basename(path)} is {size} bytes (limit {MAX_SNAPSHOT_FILE_BYTES})",
        )
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SNAPSHOT_FILE_BYTES + 1)
    except OSError as exc:
        return None, _refuse("file_unreadable", f"{os.path.basename(path)} could not be read ({exc.__class__.__name__})")
    if len(raw) > MAX_SNAPSHOT_FILE_BYTES:
        return None, _refuse("file_too_large", f"{os.path.basename(path)} grew past the size limit while reading")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # A partially written file fails here.  No retry loop: the next
        # dock produces a fresh file and a fresh journal event.
        return None, _refuse("file_malformed", f"{os.path.basename(path)} is not valid JSON (partial write?)")
    if not isinstance(data, dict):
        return None, _refuse("file_malformed", f"{os.path.basename(path)} is not a JSON object")
    return data, None


# ─── Identity validation ────────────────────────────────────────────────────


def _validate_identity(event: str, entry: Dict[str, Any], data: Dict[str, Any]) -> Optional[SnapshotResult]:
    if data.get("event") != event:
        return _refuse("event_mismatch", f"file event is {data.get('event')!r}, journal event is {event!r}")

    entry_market = entry.get("MarketID")
    file_market = data.get("MarketID")
    if not _is_int(entry_market) or entry_market <= 0:
        return _refuse("missing_marketid", "journal event has no numeric MarketID")
    if not _is_int(file_market) or file_market != entry_market:
        return _refuse(
            "marketid_mismatch",
            f"file MarketID {file_market!r} does not match journal MarketID {entry_market!r}",
        )

    for key, code in (("StationName", "station_mismatch"), ("StarSystem", "system_mismatch")):
        entry_value = entry.get(key)
        file_value = data.get(key)
        if not isinstance(entry_value, str) or not entry_value.strip():
            return _refuse("missing_station_identity", f"journal event has no {key}")
        if not isinstance(file_value, str) or not file_value.strip():
            return _refuse("missing_station_identity", f"snapshot file has no {key}")
        if entry_value.strip() != file_value.strip():
            return _refuse(code, f"file {key} {file_value!r} does not match journal {key} {entry_value!r}")

    entry_ts = parse_journal_timestamp(entry.get("timestamp"))
    file_ts = parse_journal_timestamp(data.get("timestamp"))
    if entry_ts is None:
        return _refuse("entry_missing_timestamp", "journal event has no parseable timestamp")
    if file_ts is None:
        return _refuse("file_malformed", "snapshot file has no parseable timestamp")
    skew = abs((file_ts - entry_ts).total_seconds())
    if skew > SNAPSHOT_TIMESTAMP_TOLERANCE_SECONDS:
        return _refuse(
            "stale_snapshot",
            f"snapshot timestamp {data.get('timestamp')} is {int(skew)}s away from the journal event",
        )
    return None


# ─── Inventory normalisation ────────────────────────────────────────────────


def _normalize_market_items(items: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[SnapshotResult]]:
    if items is None:
        return None, _refuse("missing_items", "Market.json has no Items array")
    if not isinstance(items, list):
        return None, _refuse("malformed_items", "Market.json Items is not a list")
    if len(items) > MAX_MARKET_ITEMS:
        return None, _refuse("too_many_items", f"Market.json lists {len(items)} items (limit {MAX_MARKET_ITEMS})")
    by_symbol: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return None, _refuse("malformed_item", f"Market.json Items[{index}] is not an object")
        name = item.get("Name")
        if not isinstance(name, str) or not name.strip():
            return None, _refuse("malformed_item", f"Market.json Items[{index}] has no Name")
        symbol = canonical_commodity_symbol(name)
        if not symbol:
            return None, _refuse("malformed_item", f"Market.json Items[{index}] has an unusable Name")
        # Identity is the symbol only.  The localised name depends on the
        # commander's game language and would split one commodity into
        # several identities server-side, so it is not forwarded.
        normalised: Dict[str, Any] = {"Name": symbol}
        for key in ("BuyPrice", "SellPrice", "Stock", "Demand"):
            value = item.get(key)
            if not _is_int(value) or value < 0:
                return None, _refuse("malformed_item", f"Market.json Items[{index}].{key} is not a non-negative integer")
            normalised[key] = value
        mean = item.get("MeanPrice")
        if mean is not None:
            if not _is_int(mean) or mean < 0:
                return None, _refuse("malformed_item", f"Market.json Items[{index}].MeanPrice is not a non-negative integer")
            normalised["MeanPrice"] = mean
        # Later duplicates of the same symbol win (the file is a snapshot;
        # duplicates are not expected but must not double-count).
        by_symbol[symbol] = normalised
    return [by_symbol[key] for key in sorted(by_symbol)], None


def _normalize_outfitting_modules(items: Any) -> Tuple[Optional[List[str]], Optional[SnapshotResult]]:
    if items is None:
        return None, _refuse("missing_items", "Outfitting.json has no Items array")
    if not isinstance(items, list):
        return None, _refuse("malformed_items", "Outfitting.json Items is not a list")
    if len(items) > MAX_OUTFITTING_MODULES:
        return None, _refuse(
            "too_many_items",
            f"Outfitting.json lists {len(items)} modules (limit {MAX_OUTFITTING_MODULES})",
        )
    modules = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return None, _refuse("malformed_item", f"Outfitting.json Items[{index}] is not an object")
        name = item.get("Name")
        if not isinstance(name, str) or not name.strip():
            return None, _refuse("malformed_item", f"Outfitting.json Items[{index}] has no Name")
        canonical = canonical_module_name(name)
        if canonical is None:
            # Not a purchasable ship module (planet approach suite, or an
            # unrecognised token).  Skip the entry; the rest of the
            # snapshot is still a faithful list of modules for sale.
            continue
        modules.add(canonical)
    return sorted(modules), None


def _normalize_shipyard_ships(data: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, str]]], Optional[SnapshotResult]]:
    present = [key for key in _SHIPYARD_LIST_KEYS if key in data]
    if not present:
        return None, _refuse("missing_items", "Shipyard.json has no PriceList array")
    ships_raw = data[present[0]]
    if not isinstance(ships_raw, list):
        return None, _refuse("malformed_items", f"Shipyard.json {present[0]} is not a list")
    if len(ships_raw) > MAX_SHIPYARD_SHIPS:
        return None, _refuse("too_many_items", f"Shipyard.json lists {len(ships_raw)} ships (limit {MAX_SHIPYARD_SHIPS})")
    ships = set()
    for index, item in enumerate(ships_raw):
        if not isinstance(item, dict):
            return None, _refuse("malformed_item", f"Shipyard.json {present[0]}[{index}] is not an object")
        ship = canonical_ship_type(item.get("ShipType"))
        if ship is None:
            return None, _refuse("malformed_item", f"Shipyard.json {present[0]}[{index}] has no usable ShipType")
        ships.add(ship)
    return [{"ShipType": ship} for ship in sorted(ships)], None


# ─── Public entry point ─────────────────────────────────────────────────────

# Journal-line fields carried through to the forwarded event.  The journal
# Market/Outfitting/Shipyard lines are tiny station-identity records; the
# sidecar file's own copy of these is validated against them above, never
# used in their place.
_ENTRY_PASSTHROUGH_KEYS = (
    "timestamp",
    "event",
    "MarketID",
    "StationName",
    "StationType",
    "StarSystem",
    "SystemAddress",
    "CarrierDockingAccess",
)


def load_catalog_snapshot(entry: Dict[str, Any], journal_dir: Optional[str]) -> SnapshotResult:
    """Build the forwardable catalog event for a Market / Outfitting /
    Shipyard journal entry, or explain why it must not be forwarded.

    Never raises for bad data; never blocks beyond one bounded file read.
    """
    event = entry.get("event") if isinstance(entry, dict) else None
    if event not in CATALOG_FILE_NAMES:
        return _refuse("not_catalog_event", f"{event!r} has no sidecar snapshot")
    if not journal_dir:
        return _refuse("no_journal_dir", "EDMC journal folder is unknown")

    path = os.path.join(journal_dir, CATALOG_FILE_NAMES[event])
    data, refusal = _read_snapshot_file(path)
    if refusal is not None or data is None:
        return refusal or _refuse("file_malformed", "snapshot file unreadable")

    refusal = _validate_identity(event, entry, data)
    if refusal is not None:
        return refusal

    forwarded: Dict[str, Any] = {}
    for key in _ENTRY_PASSTHROUGH_KEYS:
        if key in entry:
            forwarded[key] = entry[key]

    count = 0
    if event == "Market":
        listings, refusal = _normalize_market_items(data.get("Items"))
        if refusal is not None or listings is None:
            return refusal or _refuse("malformed_items", "Market.json Items unusable")
        if not listings:
            # A genuinely empty market cannot be represented by the
            # server's full-replacement write (it ignores empty arrays
            # rather than deleting rows), so forwarding it would only be
            # counted as skipped.  Refuse locally and say why.
            return _refuse("empty_items", "Market.json lists no commodities")
        forwarded["Items"] = listings
        count = len(listings)
    elif event == "Outfitting":
        modules, refusal = _normalize_outfitting_modules(data.get("Items"))
        if refusal is not None or modules is None:
            return refusal or _refuse("malformed_items", "Outfitting.json Items unusable")
        if not modules:
            return _refuse("empty_items", "Outfitting.json lists no purchasable modules")
        forwarded["Modules"] = modules
        count = len(modules)
    else:
        ships, refusal = _normalize_shipyard_ships(data)
        if refusal is not None or ships is None:
            return refusal or _refuse("malformed_items", "Shipyard.json ship list unusable")
        if not ships:
            return _refuse("empty_items", "Shipyard.json lists no ships")
        forwarded["PriceList"] = ships
        count = len(ships)

    try:
        size = len(json.dumps(forwarded, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return _refuse("event_unserialisable", "normalised event could not be serialised")
    if size > CATALOG_EVENT_MAX_BYTES:
        return _refuse(
            "event_too_large",
            f"normalised {event} event is {size} bytes (server limit {CATALOG_EVENT_MAX_BYTES}); "
            "snapshot not forwarded rather than truncated",
        )
    return SnapshotResult(True, forwarded, "ok", f"{count} entries, {size} bytes", count, size)


# ─── Chronology guard ───────────────────────────────────────────────────────


class CatalogChronology:
    """Remembers the newest snapshot timestamp forwarded per
    (event, MarketID) so an older observation can never follow a newer one
    into the buffer (e.g. a replayed journal).  Bounded in size."""

    def __init__(self, max_entries: int = 64) -> None:
        self._latest: Dict[Tuple[str, int], datetime] = {}
        self._max_entries = max_entries

    def is_newer(self, event: Dict[str, Any]) -> bool:
        key = self._key(event)
        observed = parse_journal_timestamp(event.get("timestamp"))
        if key is None or observed is None:
            return False
        previous = self._latest.get(key)
        return previous is None or observed > previous

    def record(self, event: Dict[str, Any]) -> None:
        key = self._key(event)
        observed = parse_journal_timestamp(event.get("timestamp"))
        if key is None or observed is None:
            return
        previous = self._latest.get(key)
        if previous is not None and observed <= previous:
            return
        self._latest[key] = observed
        while len(self._latest) > self._max_entries:
            oldest = min(self._latest, key=lambda k: self._latest[k])
            del self._latest[oldest]

    @staticmethod
    def _key(event: Dict[str, Any]) -> Optional[Tuple[str, int]]:
        name = event.get("event")
        market = event.get("MarketID")
        if name not in CATALOG_FILE_NAMES or not _is_int(market):
            return None
        return (name, market)
