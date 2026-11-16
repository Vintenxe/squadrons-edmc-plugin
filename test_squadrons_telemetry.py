"""End-to-end tests for the plugin module itself (journal_entry → buffer →
flush → HTTP) against a real local HTTP server.

``squadrons_telemetry`` imports EDMC-only modules (``myNotebook``, EDMC's
``config``) and tkinter.  This file installs minimal stand-ins for those
BEFORE importing the plugin, so the real ``journal_entry`` /
``_flush_buffer`` code paths run unmodified:

  * catalog events are forwarded with the sidecar inventory, in the same
    buffer as unrelated carrier telemetry;
  * a refused snapshot forwards nothing and never disturbs other events;
  * flushes are split into server-compliant batches;
  * transient failures rebuffer + back off, permanent rejections drop only
    the offending batch, redirects never carry the token anywhere;
  * config.json credentials survive a plugin upgrade unchanged.

Run with:  python3 -m unittest discover edmc-plugin
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

# ─── EDMC environment stand-ins ─────────────────────────────────────────────

JOURNAL_DIR = tempfile.mkdtemp(prefix="sq-edmc-journal-")


class _StubConfig:
    """Mirrors the two members of EDMC's ``config`` the plugin reads."""

    journaldir = JOURNAL_DIR
    default_journal_dir = JOURNAL_DIR

    def get_str(self, key, default=""):
        if key == "journaldir":
            return self.journaldir or default
        return default


def _install_stubs():
    if "myNotebook" not in sys.modules:
        nb = types.ModuleType("myNotebook")

        class _Widget:
            def __init__(self, *args, **kwargs):
                pass

            def grid(self, *args, **kwargs):
                return self

        nb.Frame = _Widget
        nb.Label = _Widget
        nb.Notebook = _Widget
        sys.modules["myNotebook"] = nb
    try:
        import tkinter  # noqa: F401
        import tkinter.ttk  # noqa: F401
    except ImportError:
        tk = types.ModuleType("tkinter")
        tk.W = "w"
        tk.LEFT = "left"

        class _StringVar:
            def __init__(self, value=""):
                self._v = value

            def get(self):
                return self._v

            def set(self, v):
                self._v = v

        tk.StringVar = _StringVar
        ttk = types.ModuleType("tkinter.ttk")

        class _Entry:
            def __init__(self, *a, **k):
                pass

            def grid(self, *a, **k):
                return self

        ttk.Entry = _Entry
        tk.ttk = ttk
        sys.modules["tkinter"] = tk
        sys.modules["tkinter.ttk"] = ttk
    cfg = types.ModuleType("config")
    cfg.config = _StubConfig()
    sys.modules["config"] = cfg


_install_stubs()
sys.path.insert(0, HERE)
import squadrons_telemetry as plugin  # noqa: E402
import catalog_snapshots  # noqa: E402

# ─── Local servers ──────────────────────────────────────────────────────────

RECEIVED = []  # requests the telemetry server accepted or rejected
TARGET_REQUESTS = []  # anything that reached the redirect target
# Per-request behaviour queue: each entry is {"code": int, "location": str|None}.
# When empty, the server answers 200.
RESPONSES = []


class _TelemetryHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        behaviour = RESPONSES.pop(0) if RESPONSES else {"code": 200, "location": None}
        payload = json.loads(body.decode("utf-8"))
        # An event carrying "poison" fails server-side validation (400 for
        # the whole batch), mirroring zod rejecting one malformed event.
        if any(e.get("poison") for e in payload.get("events", [])):
            behaviour = {"code": 400, "location": None}
        if behaviour.get("delay"):
            import time

            time.sleep(behaviour["delay"])
        RECEIVED.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "plugin_version": self.headers.get("X-Plugin-Version"),
                "body": json.loads(body.decode("utf-8")),
                "bytes": len(body),
                "status": behaviour["code"],
            }
        )
        code = behaviour["code"]
        if 300 <= code < 400:
            self.send_response(code)
            self.send_header("Location", behaviour["location"])
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if code == 200:
            events = RECEIVED[-1]["body"]["events"]
            reply = behaviour.get("reply") or {"processed": len(events), "skipped": 0}
            self.wfile.write(json.dumps(reply).encode())
        else:
            self.wfile.write(json.dumps({"error": "nope"}).encode())

    def log_message(self, *args):
        pass


class _TargetHandler(http.server.BaseHTTPRequestHandler):
    def _record(self):
        TARGET_REQUESTS.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = _record
    do_GET = _record

    def log_message(self, *args):
        pass


def _start(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _journal_events():
    with open(os.path.join(FIXTURES, "journal_catalog_events.json"), "r", encoding="utf-8") as f:
        return json.load(f)


CARRIER_STATS = {
    "timestamp": "2026-09-29T09:26:50Z",
    "event": "CarrierStats",
    "CarrierID": 3716252928,
    "Callsign": "SPFO",
    "Name": "SFS BLACK SUN",
    "CarrierType": "SquadronCarrier",
    "DockingAccess": "squadron",
    "AllowNotorious": True,
    "FuelLevel": 500,
    "JumpRangeCurr": 500,
    "JumpRangeMax": 500,
    "PendingDecommission": False,
    "SpaceUsage": {"TotalCapacity": 60000, "Crew": 3000, "Cargo": 0, "CargoSpaceReserved": 0, "ShipPacks": 0, "ModulePacks": 0, "FreeSpace": 57000},
    "Finance": {"CarrierBalance": 0, "ReserveBalance": 0, "AvailableBalance": 0, "TaxRate_shipyard": 0},
    "Crew": [],
    "ShipPacks": [],
    "ModulePacks": [],
}

DOCKED = {
    "timestamp": "2026-09-29T09:26:02Z",
    "event": "Docked",
    "StationName": "SPFO",
    "StationType": "FleetCarrier",
    "MarketID": 3716252928,
    "StarSystem": "Leesti",
    "SystemAddress": 3932277478114,
    "StationServices": ["dock", "autodock", "commodities", "shipyard", "squadronBank"],
}


class PluginTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _start(_TelemetryHandler)
        cls.target = _start(_TargetHandler)
        cls.server_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.target_url = f"http://127.0.0.1:{cls.target.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.target):
            server.shutdown()
            server.server_close()

    def setUp(self):
        RECEIVED.clear()
        TARGET_REQUESTS.clear()
        RESPONSES.clear()
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        for name in ("Market.json", "Outfitting.json", "Shipyard.json"):
            shutil.copy(os.path.join(FIXTURES, name), os.path.join(JOURNAL_DIR, name))
        self.journal = _journal_events()
        plugin._cancel_flush_timer()
        with plugin.this.buffer_lock:
            plugin.this.event_buffer.clear()
        plugin.this.server_url = self.server_url
        plugin.this.api_token = "sq_test_token"
        plugin.this.cmdr_name = ""
        plugin.this.catalog_chronology = catalog_snapshots.CatalogChronology()
        sys.modules["config"].config.journaldir = JOURNAL_DIR

    def tearDown(self):
        plugin._cancel_flush_timer()

    # Helpers -------------------------------------------------------------

    def feed(self, *entries, cmdr="Test Cmdr"):
        for entry in entries:
            plugin.journal_entry(cmdr, False, entry.get("StarSystem", ""), entry.get("StationName", ""), dict(entry), {})
        # Tests drive flushes explicitly; never let the timer race them.
        plugin._cancel_flush_timer()

    def buffered(self):
        with plugin.this.buffer_lock:
            return list(plugin.this.event_buffer)

    def flush(self):
        """Run one flush; return True when it scheduled a back-off retry."""
        plugin._flush_buffer()
        scheduled = plugin.this.flush_timer is not None
        plugin._cancel_flush_timer()
        return scheduled

    def all_sent_events(self):
        return [e for r in RECEIVED if r["status"] == 200 for e in r["body"]["events"]]


# ─── Catalog forwarding ─────────────────────────────────────────────────────


class CatalogForwardingTests(PluginTestCase):
    def test_catalog_events_are_forwarded_with_sidecar_inventory(self):
        self.feed(DOCKED, self.journal["Market"], self.journal["Outfitting"], self.journal["Shipyard"], CARRIER_STATS)
        self.flush()

        self.assertEqual(len(RECEIVED), 1, "five events fit one request")
        request = RECEIVED[0]
        self.assertEqual(request["path"], "/api/v1/telemetry/ingest")
        self.assertEqual(request["authorization"], "Bearer sq_test_token")
        self.assertEqual(request["plugin_version"], plugin.this.version)
        self.assertEqual(request["body"]["cmdrName"], "Test Cmdr")
        events = request["body"]["events"]
        self.assertEqual([e["event"] for e in events], ["Docked", "Market", "Outfitting", "Shipyard", "CarrierStats"])

        market, outfitting, shipyard = events[1], events[2], events[3]
        self.assertEqual(len(market["Items"]), 12)
        self.assertEqual(market["MarketID"], 3716252928)
        self.assertEqual(market["timestamp"], self.journal["Market"]["timestamp"])
        self.assertTrue(all(isinstance(m, str) for m in outfitting["Modules"]))
        self.assertEqual(len(outfitting["Modules"]), 22)
        self.assertEqual([s["ShipType"] for s in shipyard["PriceList"]][:2], ["asp", "cobramkiii"])
        # Unrelated telemetry in the same batch is byte-for-byte untouched.
        self.assertEqual(events[0], DOCKED)
        self.assertEqual(events[4], CARRIER_STATS)

    def test_refused_snapshot_forwards_nothing_and_leaves_other_events_alone(self):
        os.remove(os.path.join(JOURNAL_DIR, "Outfitting.json"))
        self.feed(self.journal["Outfitting"], self.journal["Market"], CARRIER_STATS)
        self.assertEqual([e["event"] for e in self.buffered()], ["Market", "CarrierStats"])
        self.flush()
        self.assertEqual([e["event"] for e in self.all_sent_events()], ["Market", "CarrierStats"])
        # No bare Outfitting journal line was sent: the server would treat
        # a Modules-less Outfitting as skipped, but it must not even exist
        # in the stream (nothing to misinterpret, nothing to delete with).
        self.assertTrue(all("Outfitting" != e["event"] for e in self.all_sent_events()))

    def test_marketid_mismatch_forwards_nothing(self):
        other = dict(self.journal["Market"], MarketID=128049152, StationName="George Lucas", StarSystem="Leesti")
        self.feed(other)
        self.assertEqual(self.buffered(), [])

    def test_stale_or_partial_file_forwards_nothing(self):
        data = json.load(open(os.path.join(FIXTURES, "Market.json")))
        data["timestamp"] = "2026-09-28T12:00:00Z"
        with open(os.path.join(JOURNAL_DIR, "Market.json"), "w") as f:
            json.dump(data, f)
        self.feed(self.journal["Market"])
        self.assertEqual(self.buffered(), [])

        with open(os.path.join(FIXTURES, "Shipyard.json"), "rb") as f:
            partial = f.read()[:200]
        with open(os.path.join(JOURNAL_DIR, "Shipyard.json"), "wb") as f:
            f.write(partial)
        self.feed(self.journal["Shipyard"])
        self.assertEqual(self.buffered(), [])

    def test_unknown_journal_dir_disables_catalogs_only(self):
        sys.modules["config"].config.journaldir = ""
        sys.modules["config"].config.default_journal_dir = ""
        try:
            self.feed(self.journal["Market"], DOCKED)
            self.assertEqual([e["event"] for e in self.buffered()], ["Docked"])
        finally:
            sys.modules["config"].config.default_journal_dir = JOURNAL_DIR

    def test_journal_dir_comes_from_edmc_config_not_a_guessed_path(self):
        elsewhere = tempfile.mkdtemp(prefix="sq-edmc-elsewhere-")
        try:
            shutil.copy(os.path.join(FIXTURES, "Market.json"), os.path.join(elsewhere, "Market.json"))
            os.remove(os.path.join(JOURNAL_DIR, "Market.json"))
            sys.modules["config"].config.journaldir = elsewhere
            self.feed(self.journal["Market"])
            self.assertEqual([e["event"] for e in self.buffered()], ["Market"])
        finally:
            shutil.rmtree(elsewhere, ignore_errors=True)

    def test_oversized_snapshot_is_refused_and_unrelated_telemetry_still_flows(self):
        with mock.patch.object(catalog_snapshots, "CATALOG_EVENT_MAX_BYTES", 512):
            self.feed(CARRIER_STATS, self.journal["Market"], DOCKED)
        self.assertEqual([e["event"] for e in self.buffered()], ["CarrierStats", "Docked"])
        self.flush()
        self.assertEqual([e["event"] for e in self.all_sent_events()], ["CarrierStats", "Docked"])

    def test_snapshot_under_limit_is_forwarded_whole(self):
        self.feed(self.journal["Market"])
        event = self.buffered()[0]
        size = len(json.dumps(event).encode("utf-8"))
        self.assertLess(size, catalog_snapshots.CATALOG_EVENT_MAX_BYTES)
        self.assertEqual(len(event["Items"]), 12)

    def test_older_snapshot_never_follows_a_newer_one(self):
        self.feed(self.journal["Market"])
        self.flush()
        # A replayed / older journal line for the same station with a
        # matching older file must not be forwarded after the newer one.
        data = json.load(open(os.path.join(FIXTURES, "Market.json")))
        data["timestamp"] = "2026-09-29T08:00:00Z"
        with open(os.path.join(JOURNAL_DIR, "Market.json"), "w") as f:
            json.dump(data, f)
        self.feed(dict(self.journal["Market"], timestamp="2026-09-29T08:00:00Z"))
        self.assertEqual(self.buffered(), [])
        # The same snapshot again is also not re-sent.
        shutil.copy(os.path.join(FIXTURES, "Market.json"), os.path.join(JOURNAL_DIR, "Market.json"))
        self.feed(self.journal["Market"])
        self.assertEqual(self.buffered(), [])

    def test_beta_and_unconfigured_paths_unchanged(self):
        plugin.journal_entry("Cmdr", True, "Leesti", "SPFO", dict(self.journal["Market"]), {})
        self.assertEqual(self.buffered(), [])
        plugin.this.api_token = ""
        plugin.journal_entry("Cmdr", False, "Leesti", "SPFO", dict(self.journal["Market"]), {})
        self.assertEqual(self.buffered(), [])

    def test_non_catalog_events_are_forwarded_verbatim(self):
        self.feed(DOCKED, CARRIER_STATS)
        self.assertEqual(self.buffered(), [DOCKED, CARRIER_STATS])


# ─── Batching and transmission failures ────────────────────────────────────


class TransmissionTests(PluginTestCase):
    def test_large_flush_is_split_into_compliant_batches(self):
        events = [dict(DOCKED, timestamp=f"2026-09-29T10:{n // 60:02d}:{n % 60:02d}Z") for n in range(120)]
        self.feed(*events)
        self.assertEqual(len(self.buffered()), 120)
        self.assertFalse(self.flush())
        sizes = [len(r["body"]["events"]) for r in RECEIVED]
        self.assertEqual(sizes, [50, 50, 20])
        self.assertTrue(all(s <= plugin.MAX_BATCH_EVENTS for s in sizes))

    def test_byte_ceiling_splits_batches_before_the_server_would_413(self):
        self.feed(self.journal["Market"], self.journal["Outfitting"], self.journal["Shipyard"], CARRIER_STATS)
        with mock.patch.object(plugin, "BUFFER_MAX_BYTES", 2048):
            self.flush()
        self.assertGreater(len(RECEIVED), 1)
        self.assertEqual([e["event"] for e in self.all_sent_events()], ["Market", "Outfitting", "Shipyard", "CarrierStats"])
        for request in RECEIVED:
            # Any batch holding more than one event stayed under the ceiling.
            if len(request["body"]["events"]) > 1:
                self.assertLess(request["bytes"], 2048)

    def test_transient_failure_rebuffers_and_retries_in_order(self):
        self.feed(self.journal["Market"], CARRIER_STATS)
        RESPONSES.append({"code": 503, "location": None})
        self.assertTrue(self.flush(), "a back-off retry is scheduled")
        self.assertEqual(len(RECEIVED), 1)
        self.assertEqual([e["event"] for e in self.buffered()], ["Market", "CarrierStats"])
        # A newer event arriving during the outage queues BEHIND the backlog.
        self.feed(DOCKED)
        self.assertEqual([e["event"] for e in self.buffered()], ["Market", "CarrierStats", "Docked"])
        self.flush()
        self.assertEqual(RECEIVED[-1]["status"], 200)
        self.assertEqual([e["event"] for e in RECEIVED[-1]["body"]["events"]], ["Market", "CarrierStats", "Docked"])
        self.assertEqual(RECEIVED[-1]["body"]["events"][0], RECEIVED[0]["body"]["events"][0], "identical bytes retried")
        self.assertEqual(self.buffered(), [])

    def test_network_error_rebuffers(self):
        self.feed(CARRIER_STATS)
        plugin.this.server_url = "http://127.0.0.1:9"  # nothing listens here
        self.assertTrue(self.flush())
        self.assertEqual([e["event"] for e in self.buffered()], ["CarrierStats"])

    def test_transient_failure_mid_flush_keeps_unsent_batches_in_order(self):
        events = [dict(DOCKED, timestamp=f"2026-09-29T11:{n // 60:02d}:{n % 60:02d}Z") for n in range(80)]
        self.feed(*events)
        RESPONSES.append({"code": 200, "location": None})
        RESPONSES.append({"code": 429, "location": None})
        self.assertTrue(self.flush())
        self.assertEqual([r["status"] for r in RECEIVED], [200, 429])
        rebuffered = self.buffered()
        self.assertEqual(len(rebuffered), 30)
        self.assertEqual(rebuffered[0]["timestamp"], events[50]["timestamp"])
        self.flush()
        self.assertEqual(len(self.all_sent_events()), 80)

    def test_permanent_rejection_drops_only_that_batch(self):
        # 413 / other non-event-specific 4xx: the batch is dropped as a whole
        # (400 is handled separately — see the split tests below).
        events = [dict(DOCKED, timestamp=f"2026-09-29T12:{n // 60:02d}:{n % 60:02d}Z") for n in range(60)]
        self.feed(*events)
        for code in (413, 415):
            RECEIVED.clear()
            with plugin.this.buffer_lock:
                plugin.this.event_buffer[:] = list(events)
            RESPONSES.append({"code": code, "location": None})
            self.assertFalse(self.flush(), "no retry after a permanent rejection")
            self.assertEqual([r["status"] for r in RECEIVED], [code, 200], code)
            self.assertEqual(len(RECEIVED[1]["body"]["events"]), 10)
            self.assertEqual(self.buffered(), [], "nothing is retried after a permanent rejection")

    def test_dead_credential_drops_batch_without_retry(self):
        for code in (401, 403, 426):
            RECEIVED.clear()
            self.feed(CARRIER_STATS)
            RESPONSES.append({"code": code, "location": None})
            self.assertFalse(self.flush(), code)
            self.assertEqual(self.buffered(), [], code)

    def test_redirect_never_carries_the_token_and_is_not_retried(self):
        for code in (301, 302, 303, 307, 308):
            RECEIVED.clear()
            TARGET_REQUESTS.clear()
            self.feed(self.journal["Market"], CARRIER_STATS)
            RESPONSES.append({"code": code, "location": f"{self.target_url}/steal"})
            self.assertFalse(self.flush(), code)
            self.assertEqual(TARGET_REQUESTS, [], code)
            self.assertEqual(self.buffered(), [], "redirected batch is dropped, not retried")
            self.assertEqual(len(RECEIVED), 1)
            self.assertEqual(RECEIVED[0]["authorization"], "Bearer sq_test_token")

    def test_400_for_a_mixed_batch_drops_only_the_malformed_event(self):
        poison = dict(DOCKED, timestamp="2026-09-29T13:00:01Z", poison=True)
        self.feed(self.journal["Market"], poison, CARRIER_STATS, DOCKED)
        self.assertFalse(self.flush())
        statuses = [r["status"] for r in RECEIVED]
        # One rejected batch, then each event alone: Market 200, poison 400,
        # CarrierStats 200, Docked 200.
        self.assertEqual(statuses, [400, 200, 400, 200, 200])
        self.assertEqual([e["event"] for e in self.all_sent_events()], ["Market", "CarrierStats", "Docked"])
        self.assertEqual(self.buffered(), [])

    def test_transient_failure_during_a_split_rebuffers_the_remainder_in_order(self):
        poison = dict(DOCKED, timestamp="2026-09-29T13:10:01Z", poison=True)
        later = dict(DOCKED, timestamp="2026-09-29T13:10:02Z")
        self.feed(poison, CARRIER_STATS, later)
        RESPONSES.append({"code": 400, "location": None})  # the batch (poison forces it anyway)
        RESPONSES.append({"code": 400, "location": None})  # poison alone
        RESPONSES.append({"code": 503, "location": None})  # CarrierStats alone: transient
        self.assertTrue(self.flush())
        self.assertEqual([e["event"] for e in self.buffered()], ["CarrierStats", "Docked"])
        self.flush()
        self.assertEqual([e["event"] for e in self.all_sent_events()], ["CarrierStats", "Docked"])

    def test_concurrent_flushes_are_serialised_in_buffer_order(self):
        first = [dict(DOCKED, timestamp=f"2026-09-29T14:00:{n:02d}Z") for n in range(3)]
        second = [dict(DOCKED, timestamp=f"2026-09-29T14:01:{n:02d}Z") for n in range(3)]
        self.feed(*first)
        RESPONSES.append({"code": 200, "location": None, "delay": 0.4})
        t = threading.Thread(target=plugin._flush_buffer)
        t.start()
        import time

        time.sleep(0.1)  # the first flush is now inside its slow request
        self.feed(*second)
        plugin._flush_buffer()  # a timer-driven flush arriving mid-send
        t.join(timeout=5)
        plugin._cancel_flush_timer()
        self.assertEqual(len(RECEIVED), 2)
        self.assertEqual([e["timestamp"] for e in self.all_sent_events()], [e["timestamp"] for e in first + second])

    def test_server_reported_skips_and_errors_are_logged(self):
        self.feed(self.journal["Market"], CARRIER_STATS)
        RESPONSES.append({"code": 200, "location": None, "reply": {"processed": 1, "skipped": 1, "errors": ["Market at SPFO skipped (unknown_station)"]}})
        with self.assertLogs(plugin.this.logger, level="WARNING") as captured:
            self.flush()
        self.assertTrue(any("1 processed, 1 skipped of 2" in line and "unknown_station" in line for line in captured.output), captured.output)
        for line in captured.output:
            self.assertNotIn("sq_test_token", line)

    def test_missing_station_identity_forwards_nothing(self):
        entry = dict(self.journal["Shipyard"])
        del entry["StationName"]
        self.feed(entry)
        self.assertEqual(self.buffered(), [])

    def test_flush_with_empty_buffer_sends_nothing(self):
        self.flush()
        self.assertEqual(RECEIVED, [])


# ─── Config preservation across upgrades ───────────────────────────────────


class ConfigPreservationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sq-cfg-")
        self.path = os.path.join(self.dir, "config.json")
        self._patch = mock.patch.object(plugin, "_config_path", lambda: self.path)
        self._patch.start()
        self.saved = (plugin.this.server_url, plugin.this.api_token)

    def tearDown(self):
        self._patch.stop()
        plugin.this.server_url, plugin.this.api_token = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_v13_config_file_loads_unchanged(self):
        # Exactly what 1.3.x wrote: two keys, nothing else.
        with open(self.path, "w") as f:
            json.dump({"server_url": "https://elitesquadrons.com", "api_token": "sq_" + "a" * 48}, f)
        plugin._load_config()
        self.assertEqual(plugin.this.server_url, "https://elitesquadrons.com")
        self.assertEqual(plugin.this.api_token, "sq_" + "a" * 48)

    def test_save_round_trip_keeps_the_same_schema(self):
        plugin.this.server_url = "https://squadrons.example.org"
        plugin.this.api_token = "sq_" + "b" * 48
        plugin._save_config()
        with open(self.path) as f:
            on_disk = json.load(f)
        self.assertEqual(set(on_disk), {"server_url", "api_token"})
        plugin._load_config()
        self.assertEqual(plugin.this.server_url, "https://squadrons.example.org")
        self.assertEqual(plugin.this.api_token, "sq_" + "b" * 48)


class VersionTests(unittest.TestCase):
    def test_version_is_semver_and_advertised_in_header(self):
        self.assertRegex(plugin.this.version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(plugin.this.version, "1.4.0")

    def test_changelog_mentions_the_running_version(self):
        with open(os.path.join(HERE, "CHANGELOG.md"), "r", encoding="utf-8") as f:
            changelog = f.read()
        self.assertIn(f"## [{plugin.this.version}]", changelog)


if __name__ == "__main__":
    unittest.main()
