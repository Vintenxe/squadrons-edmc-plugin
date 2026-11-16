"""Unit tests for Market / Outfitting / Shipyard sidecar snapshot loading.

Fixtures under ``fixtures/`` are realistic game-written files for the
Squadron Carrier SFS BLACK SUN (callsign SPFO, MarketID 3716252928) docked
in Leesti, plus the matching journal lines.  The journal lines carry no
inventory at all — the whole point of this module is that the inventory
must be read from the sidecar file and only when it provably belongs to
the same observation.

Run with:  python3 -m unittest discover edmc-plugin
"""

import json
import os
import shutil
import tempfile
import unittest

from catalog_snapshots import (
    CATALOG_EVENT_MAX_BYTES,
    MAX_OUTFITTING_MODULES,
    MAX_MARKET_ITEMS,
    MAX_SHIPYARD_SHIPS,
    MAX_SNAPSHOT_FILE_BYTES,
    CatalogChronology,
    canonical_commodity_symbol,
    canonical_module_name,
    canonical_ship_type,
    load_catalog_snapshot,
    parse_journal_timestamp,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load_journal_events():
    with open(os.path.join(FIXTURES, "journal_catalog_events.json"), "r", encoding="utf-8") as f:
        return json.load(f)


class _JournalDirMixin(unittest.TestCase):
    """Each test gets a private copy of the fixture journal folder."""

    def setUp(self):
        self.journal_dir = tempfile.mkdtemp(prefix="sq-journal-")
        for name in ("Market.json", "Outfitting.json", "Shipyard.json"):
            shutil.copy(os.path.join(FIXTURES, name), os.path.join(self.journal_dir, name))
        self.journal = _load_journal_events()

    def tearDown(self):
        shutil.rmtree(self.journal_dir, ignore_errors=True)

    def write_file(self, name, content):
        path = os.path.join(self.journal_dir, name)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as f:
            f.write(content)

    def read_json(self, name):
        with open(os.path.join(self.journal_dir, name), "r", encoding="utf-8") as f:
            return json.load(f)

    def write_json(self, name, data):
        self.write_file(name, json.dumps(data))


# ─── Valid snapshots ────────────────────────────────────────────────────────


class ValidSnapshotTests(_JournalDirMixin):
    def test_journal_line_alone_carries_no_inventory(self):
        # Root cause of the defect: the journal Market/Outfitting/Shipyard
        # lines have no Items / Modules / PriceList — forwarding them as-is
        # can never populate the server projection.
        for name in ("Market", "Outfitting", "Shipyard"):
            entry = self.journal[name]
            self.assertNotIn("Items", entry)
            self.assertNotIn("Modules", entry)
            self.assertNotIn("PriceList", entry)
            self.assertNotIn("ShipyardList", entry)

    def test_market_snapshot_is_normalised_to_server_schema(self):
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        event = result.event
        self.assertEqual(event["event"], "Market")
        self.assertEqual(event["MarketID"], 3716252928)
        self.assertEqual(event["StationName"], "SPFO")
        self.assertEqual(event["StarSystem"], "Leesti")
        self.assertEqual(event["StationType"], "FleetCarrier")
        # Truthful observation timestamp: the journal event's own.
        self.assertEqual(event["timestamp"], self.journal["Market"]["timestamp"])
        self.assertEqual(result.count, 12)
        self.assertEqual(len(event["Items"]), 12)
        gold = next(i for i in event["Items"] if i["Name"] == "gold")
        self.assertEqual(
            gold,
            {
                "Name": "gold",
                "BuyPrice": 48212,
                "SellPrice": 0,
                "Stock": 312,
                "Demand": 0,
                "MeanPrice": 47609,
            },
        )
        # Only contract fields are forwarded — nothing else from the file.
        # The localised name is NOT one of them: identity is the symbol.
        for item in event["Items"]:
            self.assertEqual(
                set(item) - {"Name", "BuyPrice", "SellPrice", "Stock", "Demand", "MeanPrice"},
                set(),
            )
            self.assertNotIn("Name_Localised", item)
        self.assertNotIn("Horizons", event)
        self.assertNotIn("Modules", event)
        self.assertNotIn("PriceList", event)

    def test_outfitting_object_array_becomes_canonical_module_strings(self):
        result = load_catalog_snapshot(self.journal["Outfitting"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        modules = result.event["Modules"]
        # The server's Outfitting branch consumes Modules: string[] only.
        self.assertTrue(all(isinstance(m, str) for m in modules))
        self.assertEqual(modules, sorted(modules))
        self.assertIn("hpt_pulselaser_fixed_small", modules)
        self.assertIn("int_shieldgenerator_size3_class5", modules)
        self.assertIn("cobramkiii_armour_mirrored", modules)
        self.assertIn("int_guardianfsdbooster_size3", modules)
        # The free planet approach suite is not a purchasable module.
        self.assertNotIn("int_planetapproachsuite", modules)
        # Non-module items the game lists on the same screen (paintjobs,
        # decals, bobbleheads, nameplates, ship kits) are outside the EDDN
        # outfitting/2 pattern and are omitted — the list is the complete
        # set of purchasable modules, not a partial one.
        for excluded in ("paintjob_cobramkiii_default_52", "decal_combat_elite", "bobble_station_coriolis", "nameplate_combat01_white", "cobramkiii_shipkit1_bumper1"):
            self.assertNotIn(excluded, modules)
        self.assertEqual(len(modules), 22)
        self.assertNotIn("Items", result.event)

    def test_every_eddn_pattern_module_is_preserved(self):
        # Every entry matching the EDDN outfitting/2 pattern survives,
        # whatever its casing or family: nothing a station sells is dropped.
        data = self.read_json("Outfitting.json")
        data["Items"] = [
            {"id": 1, "Name": "Hpt_XenoScanner_Basic_Tiny", "BuyPrice": 1},
            {"id": 2, "Name": "int_corrosionproofcargorack_size1_class1", "BuyPrice": 1},
            {"id": 3, "Name": "INT_HYPERDRIVE_OVERCHARGE_SIZE5_CLASS5", "BuyPrice": 1},
            {"id": 4, "Name": "Independant_Trader_Armour_Grade3", "BuyPrice": 1},
            {"id": 5, "Name": "hpt_shieldbooster_size0_class1", "BuyPrice": 1},
            {"id": 6, "Name": "int_fighterbay_size5_class1", "BuyPrice": 1},
        ]
        self.write_json("Outfitting.json", data)
        result = load_catalog_snapshot(self.journal["Outfitting"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(
            result.event["Modules"],
            [
                "hpt_shieldbooster_size0_class1",
                "hpt_xenoscanner_basic_tiny",
                "independant_trader_armour_grade3",
                "int_corrosionproofcargorack_size1_class1",
                "int_fighterbay_size5_class1",
                "int_hyperdrive_overcharge_size5_class5",
            ],
        )

    def test_shipyard_pricelist_preserves_canonical_ship_identifiers(self):
        result = load_catalog_snapshot(self.journal["Shipyard"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        ships = result.event["PriceList"]
        self.assertEqual(
            [s["ShipType"] for s in ships],
            ["asp", "cobramkiii", "eagle", "hauler", "krait_mkii", "sidewinder", "type9", "viper"],
        )
        for ship in ships:
            self.assertEqual(set(ship), {"ShipType"})  # ShipPrice / id dropped
        self.assertNotIn("AllowCobraMkIV", result.event)

    def test_shipyard_accepts_documented_list_spellings(self):
        for key in ("Pricelist", "ShipyardList"):
            with open(os.path.join(FIXTURES, "Shipyard.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            data[key] = data.pop("PriceList")
            self.write_json("Shipyard.json", data)
            result = load_catalog_snapshot(self.journal["Shipyard"], self.journal_dir)
            self.assertTrue(result.ok, f"{key}: {result.detail}")
            self.assertEqual(len(result.event["PriceList"]), 8)

    def test_file_identity_fields_never_replace_journal_identity(self):
        # Matching identity but a different StationType spelling in the
        # file: the forwarded event keeps the journal line's values.
        data = self.read_json("Market.json")
        data["StationType"] = "Something Else"
        self.write_json("Market.json", data)
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.event["StationType"], "FleetCarrier")


# ─── Identity / staleness refusals ──────────────────────────────────────────


class MismatchRefusalTests(_JournalDirMixin):
    def test_marketid_mismatch_is_refused(self):
        entry = dict(self.journal["Market"], MarketID=3716252929)
        result = load_catalog_snapshot(entry, self.journal_dir)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "marketid_mismatch")
        self.assertIsNone(result.event)

    def test_station_name_mismatch_is_refused(self):
        entry = dict(self.journal["Market"], StationName="Q2K-0YZ")
        result = load_catalog_snapshot(entry, self.journal_dir)
        self.assertEqual(result.reason, "station_mismatch")

    def test_star_system_mismatch_is_refused(self):
        entry = dict(self.journal["Shipyard"], StarSystem="Diaguandri")
        result = load_catalog_snapshot(entry, self.journal_dir)
        self.assertEqual(result.reason, "system_mismatch")

    def test_event_mismatch_is_refused(self):
        # Outfitting.json content saved under Market.json name.
        shutil.copy(os.path.join(FIXTURES, "Outfitting.json"), os.path.join(self.journal_dir, "Market.json"))
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertEqual(result.reason, "event_mismatch")

    def test_missing_station_identity_is_refused_on_either_side(self):
        for key in ("StationName", "StarSystem"):
            entry = dict(self.journal["Market"])
            del entry[key]
            result = load_catalog_snapshot(entry, self.journal_dir)
            self.assertEqual(result.reason, "missing_station_identity", key)
            entry[key] = "   "
            self.assertEqual(load_catalog_snapshot(entry, self.journal_dir).reason, "missing_station_identity", key)

            data = self.read_json("Market.json")
            del data[key]
            self.write_json("Market.json", data)
            result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
            self.assertEqual(result.reason, "missing_station_identity", key)
            shutil.copy(os.path.join(FIXTURES, "Market.json"), os.path.join(self.journal_dir, "Market.json"))

    def test_journal_entry_without_marketid_is_refused(self):
        entry = dict(self.journal["Market"])
        del entry["MarketID"]
        self.assertEqual(load_catalog_snapshot(entry, self.journal_dir).reason, "missing_marketid")
        entry["MarketID"] = "3716252928"
        self.assertEqual(load_catalog_snapshot(entry, self.journal_dir).reason, "missing_marketid")

    def test_old_file_from_previous_visit_is_refused(self):
        data = self.read_json("Market.json")
        data["timestamp"] = "2026-09-28T21:05:11Z"  # last night's docking
        self.write_json("Market.json", data)
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertEqual(result.reason, "stale_snapshot")

    def test_small_timestamp_skew_is_tolerated(self):
        data = self.read_json("Market.json")
        data["timestamp"] = "2026-09-29T09:26:43Z"  # 3 s after the journal line
        self.write_json("Market.json", data)
        self.assertTrue(load_catalog_snapshot(self.journal["Market"], self.journal_dir).ok)

    def test_rapid_revisit_window_boundary(self):
        # Rapid consecutive visits to the SAME station (same MarketID): the
        # game rewrites the sidecar every time the screen is opened, so the
        # file on disk is always the most recent write.  The only file that
        # can be older than the journal line is one the game did NOT rewrite
        # for this event, and it is admitted only inside the 120 s window.
        # Just outside the window it is refused; the boundary is exact.
        entry = dict(self.journal["Market"], timestamp="2026-09-29T09:28:41Z")  # 121 s after the file
        self.assertEqual(load_catalog_snapshot(entry, self.journal_dir).reason, "stale_snapshot")
        entry = dict(self.journal["Market"], timestamp="2026-09-29T09:28:40Z")  # exactly 120 s: admitted
        self.assertTrue(load_catalog_snapshot(entry, self.journal_dir).ok)
        # A file NEWER than the journal line (the game already rewrote it for
        # the next visit while this line was still queued) is admitted with the
        # journal line's own timestamp — content can only be newer, never a
        # previous visit's.
        data = self.read_json("Market.json")
        data["timestamp"] = "2026-09-29T09:27:30Z"  # 50 s after the journal line
        self.write_json("Market.json", data)
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.event["timestamp"], self.journal["Market"]["timestamp"])
        # A different station's file written inside the window is still
        # refused on identity, whatever its timestamp.
        data["MarketID"] = 3716252929
        self.write_json("Market.json", data)
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], self.journal_dir).reason, "marketid_mismatch")

    def test_file_without_timestamp_is_refused(self):
        data = self.read_json("Market.json")
        del data["timestamp"]
        self.write_json("Market.json", data)
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], self.journal_dir).reason, "file_malformed")

    def test_unknown_journal_dir_is_refused(self):
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], None).reason, "no_journal_dir")
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], "").reason, "no_journal_dir")

    def test_non_catalog_event_is_refused(self):
        entry = {"event": "Docked", "timestamp": "2026-09-29T09:26:02Z", "MarketID": 3716252928}
        self.assertEqual(load_catalog_snapshot(entry, self.journal_dir).reason, "not_catalog_event")


# ─── File-state refusals ────────────────────────────────────────────────────


class FileStateRefusalTests(_JournalDirMixin):
    def test_missing_file_is_refused(self):
        os.remove(os.path.join(self.journal_dir, "Shipyard.json"))
        result = load_catalog_snapshot(self.journal["Shipyard"], self.journal_dir)
        self.assertEqual(result.reason, "file_missing")

    def test_empty_file_is_refused(self):
        self.write_file("Market.json", b"")
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], self.journal_dir).reason, "file_empty")

    def test_partially_written_file_is_refused(self):
        with open(os.path.join(FIXTURES, "Market.json"), "rb") as f:
            full = f.read()
        self.write_file("Market.json", full[: len(full) // 2])
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertEqual(result.reason, "file_malformed")

    def test_non_object_json_is_refused(self):
        self.write_file("Market.json", "[1, 2, 3]")
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], self.journal_dir).reason, "file_malformed")

    def test_non_utf8_file_is_refused(self):
        self.write_file("Market.json", b"\xff\xfe{}")
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], self.journal_dir).reason, "file_malformed")

    def test_oversized_file_is_refused_without_parsing(self):
        with open(os.path.join(self.journal_dir, "Market.json"), "ab") as f:
            f.write(b" " * (MAX_SNAPSHOT_FILE_BYTES + 1))
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], self.journal_dir).reason, "file_too_large")


# ─── Inventory-array refusals ───────────────────────────────────────────────


class InventoryArrayTests(_JournalDirMixin):
    def test_missing_items_array_is_not_an_empty_inventory(self):
        for event, key in (("Market", "Items"), ("Outfitting", "Items"), ("Shipyard", "PriceList")):
            data = self.read_json(f"{event}.json")
            del data[key]
            self.write_json(f"{event}.json", data)
            result = load_catalog_snapshot(self.journal[event], self.journal_dir)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "missing_items", event)
            self.assertIsNone(result.event)

    def test_empty_items_array_is_not_forwarded(self):
        for event, key in (("Market", "Items"), ("Outfitting", "Items"), ("Shipyard", "PriceList")):
            data = self.read_json(f"{event}.json")
            data[key] = []
            self.write_json(f"{event}.json", data)
            result = load_catalog_snapshot(self.journal[event], self.journal_dir)
            self.assertEqual(result.reason, "empty_items", event)

    def test_non_list_items_is_refused(self):
        data = self.read_json("Market.json")
        data["Items"] = {"gold": 1}
        self.write_json("Market.json", data)
        self.assertEqual(load_catalog_snapshot(self.journal["Market"], self.journal_dir).reason, "malformed_items")

    def test_malformed_market_item_refuses_whole_snapshot(self):
        # A partial listing would be applied by the server as a full
        # replacement and delete the rows that were dropped — so one bad
        # entry refuses the snapshot rather than forwarding the rest.
        for bad in (
            {"Name": "$gold_name;", "BuyPrice": "48212", "SellPrice": 0, "Stock": 1, "Demand": 0},
            {"Name": "$gold_name;", "BuyPrice": -1, "SellPrice": 0, "Stock": 1, "Demand": 0},
            {"Name": "$gold_name;", "BuyPrice": 1, "SellPrice": 0, "Stock": True, "Demand": 0},
            {"Name": "", "BuyPrice": 1, "SellPrice": 0, "Stock": 1, "Demand": 0},
            {"BuyPrice": 1, "SellPrice": 0, "Stock": 1, "Demand": 0},
            "gold",
        ):
            data = self.read_json("Market.json")
            data["Items"].append(bad)
            self.write_json("Market.json", data)
            result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
            self.assertEqual(result.reason, "malformed_item", bad)

    def test_market_mean_price_is_optional(self):
        data = self.read_json("Market.json")
        for item in data["Items"]:
            item.pop("MeanPrice", None)
        self.write_json("Market.json", data)
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        self.assertTrue(all("MeanPrice" not in i for i in result.event["Items"]))

    def test_outfitting_arbitrary_object_arrays_are_not_module_lists(self):
        # Loadout-style per-slot objects have no purchasable module Name and
        # must never masquerade as a station's Modules: string[] listing.
        data = self.read_json("Outfitting.json")
        data["Items"] = [
            {"Slot": "MediumHardpoint1", "Item": "hpt_pulselaser_fixed_medium", "On": True, "Priority": 1},
            {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1},
        ]
        self.write_json("Outfitting.json", data)
        result = load_catalog_snapshot(self.journal["Outfitting"], self.journal_dir)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "malformed_item")

    def test_outfitting_skips_unrecognised_names_but_refuses_nameless_entries(self):
        data = self.read_json("Outfitting.json")
        data["Items"].append({"id": 1, "Name": "$int_mystery_name;", "BuyPrice": 1})
        self.write_json("Outfitting.json", data)
        result = load_catalog_snapshot(self.journal["Outfitting"], self.journal_dir)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(len(result.event["Modules"]), 22)

        data["Items"].append({"id": 2, "BuyPrice": 1})
        self.write_json("Outfitting.json", data)
        self.assertEqual(load_catalog_snapshot(self.journal["Outfitting"], self.journal_dir).reason, "malformed_item")

    def test_shipyard_entry_without_shiptype_is_refused(self):
        data = self.read_json("Shipyard.json")
        data["PriceList"].append({"id": 1, "ShipPrice": 100})
        self.write_json("Shipyard.json", data)
        self.assertEqual(load_catalog_snapshot(self.journal["Shipyard"], self.journal_dir).reason, "malformed_item")

    def test_duplicates_collapse_to_one_entry(self):
        data = self.read_json("Shipyard.json")
        data["PriceList"].append({"id": 128049249, "ShipType": "Sidewinder", "ShipPrice": 32000})
        self.write_json("Shipyard.json", data)
        result = load_catalog_snapshot(self.journal["Shipyard"], self.journal_dir)
        self.assertEqual([s["ShipType"] for s in result.event["PriceList"]].count("sidewinder"), 1)


# ─── Size limits ────────────────────────────────────────────────────────────


def _big_outfitting_items(count):
    """A Jameson-Memorial-scale module list: every size/class combination of
    a set of internal module families, i.e. genuinely distinct names."""
    families = [
        "int_shieldgenerator", "int_powerplant", "int_engine", "int_hyperdrive",
        "int_lifesupport", "int_powerdistributor", "int_sensors", "int_fueltank",
        "int_cargorack", "int_fuelscoop", "int_refinery", "int_repairer",
        "int_hullreinforcement", "int_modulereinforcement", "int_shieldcellbank",
        "int_fsdinterdictor", "int_dronecontrol_collection", "int_dronecontrol_prospector",
        "int_dronecontrol_fueltransfer", "int_dronecontrol_repair", "int_passengercabin",
    ]
    items = []
    for family in families:
        for size in range(1, 9):
            for cls in range(1, 6):
                items.append({"id": len(items) + 128000000, "Name": f"{family}_size{size}_class{cls}", "BuyPrice": 1000 * size * cls})
                if len(items) >= count:
                    return items
    hardpoints = ["pulselaser", "beamlaser", "burstlaser", "multicannon", "cannon", "railgun", "plasmaaccelerator", "fragmentcannon", "minelauncher", "basicmissilerack", "dumbfiremissilerack"]
    for hp in hardpoints:
        for mount in ("fixed", "gimbal", "turret"):
            for size in ("small", "medium", "large", "huge"):
                items.append({"id": len(items) + 128000000, "Name": f"hpt_{hp}_{mount}_{size}", "BuyPrice": 10000})
                if len(items) >= count:
                    return items
    return items


_REAL_COMMODITY_SYMBOLS = (
    "hydrogenfuel", "tritium", "gold", "silver", "palladium", "platinum", "painite",
    "bauxite", "bertrandite", "gallite", "indite", "lepidolite", "rutile", "uraninite",
    "clothing", "consumertechnology", "domesticappliances", "water", "fish", "grain",
    "tea", "coffee", "algae", "animalmeat", "fruitandvegetables", "syntheticmeat",
    "beer", "wine", "liquor", "tobacco", "explosives", "pesticides", "mineraloil",
    "hydrogenperoxide", "liquidoxygen", "agriculturalmedicines", "basicmedicines",
    "performanceenhancers", "progenitorcells", "combatstabilisers", "advancedcatalysers",
    "autofabricators", "bioreducinglichen", "computercomponents", "hazardousenvironmentsuits",
    "medicaldiagnosticequipment", "microcontrollers", "powergenerators", "resonatingseparators",
    "robotics", "superconductors", "polymers", "semiconductors", "ceramiccomposites",
    "metaalloys", "insulatingmembrane", "cmmcomposite", "aluminium", "beryllium", "copper",
    "lithium", "titanium", "tantalum", "thorium", "uranium", "cobalt", "osmium", "praseodymium",
    "samarium", "thallium", "bismuth", "gallium", "indium", "lanthanum", "boron",
)


def _big_market_items(count):
    """A market listing with realistic symbol / localised-name lengths.

    Only ~400 distinct commodities exist in the game; this cycles through
    real symbols (with a short suffix for uniqueness beyond the real list)
    so the measured size reflects genuine field lengths."""
    items = []
    for n in range(count):
        symbol = _REAL_COMMODITY_SYMBOLS[n % len(_REAL_COMMODITY_SYMBOLS)]
        suffix = "" if n < len(_REAL_COMMODITY_SYMBOLS) else str(n // len(_REAL_COMMODITY_SYMBOLS))
        items.append(
            {
                "id": 128049000 + n,
                "Name": f"${symbol}{suffix}_name;",
                "Name_Localised": f"{symbol.capitalize()} {suffix}".strip(),
                "Category": "$MARKET_category_metals;",
                "Category_Localised": "Metals",
                "BuyPrice": 100000 + n,
                "SellPrice": 99000 + n,
                "MeanPrice": 100500 + n,
                "StockBracket": 2,
                "DemandBracket": 0,
                "Stock": 250000 + n,
                "Demand": 0,
                "Consumer": False,
                "Producer": True,
                "Rare": False,
            }
        )
    return items


class SizeLimitTests(_JournalDirMixin):
    def test_realistic_full_catalogs_fit_the_per_event_limit(self):
        # Largest realistic catalogs in the live game: ~450 distinct
        # commodities exist in total, a Jameson-scale outfitting board lists
        # ~1,000 modules, no shipyard sells more than ~45 ship types.  All of
        # them must transmit under the server's 64 KB per-event ceiling once
        # normalised (the raw files are several times larger).
        data = self.read_json("Market.json")
        data["Items"] = _big_market_items(400)
        self.write_json("Market.json", data)
        market = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertTrue(market.ok, market.detail)
        self.assertEqual(market.count, 400)
        self.assertLess(market.bytes, CATALOG_EVENT_MAX_BYTES)

        data = self.read_json("Outfitting.json")
        data["Items"] = _big_outfitting_items(1000)
        distinct_modules = len({item["Name"] for item in data["Items"]})
        self.assertGreaterEqual(distinct_modules, 950)
        self.write_json("Outfitting.json", data)
        raw_size = os.path.getsize(os.path.join(self.journal_dir, "Outfitting.json"))
        outfitting = load_catalog_snapshot(self.journal["Outfitting"], self.journal_dir)
        self.assertTrue(outfitting.ok, outfitting.detail)
        self.assertEqual(outfitting.count, distinct_modules)
        self.assertLess(outfitting.bytes, CATALOG_EVENT_MAX_BYTES)
        self.assertLess(outfitting.bytes, raw_size)

        data = self.read_json("Shipyard.json")
        data["PriceList"] = [{"id": n, "ShipType": f"shiptype{n:02d}", "ShipPrice": 10 ** 7} for n in range(45)]
        self.write_json("Shipyard.json", data)
        shipyard = load_catalog_snapshot(self.journal["Shipyard"], self.journal_dir)
        self.assertTrue(shipyard.ok, shipyard.detail)
        self.assertLess(shipyard.bytes, CATALOG_EVENT_MAX_BYTES)

    def test_over_cap_arrays_are_refused_not_truncated(self):
        data = self.read_json("Market.json")
        data["Items"] = _big_market_items(MAX_MARKET_ITEMS + 1)
        self.write_json("Market.json", data)
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertEqual(result.reason, "too_many_items")
        self.assertIsNone(result.event)

        data = self.read_json("Outfitting.json")
        data["Items"] = [{"id": n, "Name": f"int_x_size{n}", "BuyPrice": 1} for n in range(MAX_OUTFITTING_MODULES + 1)]
        self.write_json("Outfitting.json", data)
        self.assertEqual(load_catalog_snapshot(self.journal["Outfitting"], self.journal_dir).reason, "too_many_items")

        data = self.read_json("Shipyard.json")
        data["PriceList"] = [{"ShipType": f"ship{n}"} for n in range(MAX_SHIPYARD_SHIPS + 1)]
        self.write_json("Shipyard.json", data)
        self.assertEqual(load_catalog_snapshot(self.journal["Shipyard"], self.journal_dir).reason, "too_many_items")

    def test_event_over_byte_limit_is_refused_whole(self):
        # Within the array cap but with pathological (still well-formed)
        # localised names the serialised event can exceed 64 KB.  It must
        # be refused whole — never trimmed to fit.
        data = self.read_json("Market.json")
        items = _big_market_items(600)
        for index, item in enumerate(items):
            item["Name"] = f"${'averyverylongcommoditysymbol' * 4}{index:04d}_name;"
        data["Items"] = items
        self.write_json("Market.json", data)
        result = load_catalog_snapshot(self.journal["Market"], self.journal_dir)
        self.assertEqual(result.reason, "event_too_large")
        self.assertIsNone(result.event)


# ─── Chronology guard ───────────────────────────────────────────────────────


class ChronologyTests(unittest.TestCase):
    def test_older_snapshot_for_same_market_is_rejected(self):
        guard = CatalogChronology()
        newer = {"event": "Market", "MarketID": 3716252928, "timestamp": "2026-09-29T09:26:40Z"}
        older = {"event": "Market", "MarketID": 3716252928, "timestamp": "2026-09-29T08:00:00Z"}
        self.assertTrue(guard.is_newer(newer))
        guard.record(newer)
        self.assertFalse(guard.is_newer(older))
        self.assertFalse(guard.is_newer(dict(newer)))  # exact repeat
        # Other events / other stations are tracked independently.
        self.assertTrue(guard.is_newer({"event": "Outfitting", "MarketID": 3716252928, "timestamp": "2026-09-29T08:00:00Z"}))
        self.assertTrue(guard.is_newer({"event": "Market", "MarketID": 1, "timestamp": "2026-09-29T08:00:00Z"}))
        # Recording an older observation never regresses the watermark.
        guard.record(older)
        self.assertFalse(guard.is_newer(older))

    def test_guard_is_bounded(self):
        guard = CatalogChronology(max_entries=3)
        for n in range(10):
            guard.record({"event": "Market", "MarketID": n, "timestamp": f"2026-09-29T09:00:{n:02d}Z"})
        self.assertEqual(len(guard._latest), 3)

    def test_unparseable_events_are_never_newer(self):
        guard = CatalogChronology()
        self.assertFalse(guard.is_newer({"event": "Market", "MarketID": 1}))
        self.assertFalse(guard.is_newer({"event": "Docked", "MarketID": 1, "timestamp": "2026-09-29T09:00:00Z"}))


# ─── Helpers ────────────────────────────────────────────────────────────────


class HelperTests(unittest.TestCase):
    def test_commodity_symbol(self):
        self.assertEqual(canonical_commodity_symbol("$gold_name;"), "gold")
        self.assertEqual(canonical_commodity_symbol("$Meta_Alloys_name;"), "meta_alloys")
        self.assertEqual(canonical_commodity_symbol("gold"), "gold")

    def test_module_names(self):
        self.assertEqual(canonical_module_name("Hpt_PulseLaser_Fixed_Small"), "hpt_pulselaser_fixed_small")
        self.assertEqual(canonical_module_name("int_shieldgenerator_size3_class5"), "int_shieldgenerator_size3_class5")
        self.assertEqual(canonical_module_name("CobraMkIII_Armour_Grade1"), "cobramkiii_armour_grade1")
        self.assertIsNone(canonical_module_name("int_planetapproachsuite"))
        self.assertIsNone(canonical_module_name("$int_mystery_name;"))
        self.assertIsNone(canonical_module_name("hpt_ pulse"))
        self.assertIsNone(canonical_module_name(42))
        self.assertIsNone(canonical_module_name("suit_something"))

    def test_ship_types(self):
        self.assertEqual(canonical_ship_type("Krait_MkII"), "krait_mkii")
        self.assertEqual(canonical_ship_type(" type9 "), "type9")
        self.assertIsNone(canonical_ship_type("Cobra Mk III"))
        self.assertIsNone(canonical_ship_type(""))
        self.assertIsNone(canonical_ship_type(None))

    def test_timestamp_parsing(self):
        self.assertIsNotNone(parse_journal_timestamp("2026-09-29T09:26:40Z"))
        self.assertIsNotNone(parse_journal_timestamp("2026-09-29T09:26:40+00:00"))
        self.assertIsNone(parse_journal_timestamp("yesterday"))
        self.assertIsNone(parse_journal_timestamp(1700000000))
        self.assertIsNone(parse_journal_timestamp(""))


if __name__ == "__main__":
    unittest.main()
