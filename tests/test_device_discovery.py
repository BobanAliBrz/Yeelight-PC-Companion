"""Tests for the Yeelight device model, configuration projection and discovery.

Everything here is deterministic: no real LAN discovery is performed in the unit
suite. ``yeelight.discover_bulbs`` is replaced by a fake that returns canned SSDP
responses, so the normalization, the duplicate handling, the matching against the
configured list and the failure paths are all exercised for real.

Run with:  python -m unittest discover -s tests -t .
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_manager as cm  # noqa: E402
import yeelight_devices as yd  # noqa: E402

try:
    import yeelight_device_ui as ui  # noqa: E402
except Exception as exc:  # pragma: no cover - depends on the test machine
    ui = None
    UI_IMPORT_ERROR = exc
else:
    UI_IMPORT_ERROR = None

try:
    import first_run_wizard as wizard  # noqa: E402
except Exception as exc:  # pragma: no cover - depends on the test machine
    wizard = None
    WIZARD_IMPORT_ERROR = exc
else:
    WIZARD_IMPORT_ERROR = None

SKIP_REASON = f"yeelight_device_ui is not importable here: {UI_IMPORT_ERROR}"
WIZARD_SKIP_REASON = f"first_run_wizard is not importable here: {WIZARD_IMPORT_ERROR}"

YEE_ID_A = "yeelight:0x00000000037073d2"
YEE_ID_B = "yeelight:0x0000000003b0cb6d"


def device(name, ip, enabled=True, device_id=None, **extra):
    device_id = device_id or yd.new_local_device_id()
    entry = {"id": device_id, "name": name, "ip": ip, "enabled": enabled}
    entry.update(extra)
    return entry


def bulb(ip, device_id="0x00000000037073d2", model="color", **capabilities):
    """One raw `discover_bulbs()` style response."""
    caps = {
        "id": device_id,
        "model": model,
        "fw_ver": "76",
        "power": "on",
        "name": "",
    }
    caps.update(capabilities)
    return {
        "ip": ip,
        "port": 55443,
        "capabilities": {key: value for key, value in caps.items() if value is not None},
    }


def report_with(*bulbs):
    return yd.normalize_discovery_results(list(bulbs))


class TestDeviceIdentity(unittest.TestCase):
    """A device id is stable, unique and independent of the display name."""

    def test_a_discovered_id_is_namespaced(self):
        self.assertEqual(YEE_ID_A, yd.discovered_device_id("0x00000000037073d2"))
        # An already-namespaced id is not prefixed twice.
        self.assertEqual(YEE_ID_A, yd.discovered_device_id(YEE_ID_A))

    def test_a_locally_assigned_id_is_durable_and_unique(self):
        first = yd.new_local_device_id()
        second = yd.new_local_device_id()
        self.assertTrue(first.startswith(yd.DEVICE_ID_LOCAL_PREFIX))
        self.assertNotEqual(first, second)
        self.assertTrue(yd.is_local_device_id(first))
        self.assertFalse(yd.is_local_device_id(YEE_ID_A))

    def test_a_manual_device_keeps_its_id_when_it_is_renamed(self):
        devices = yd.add_device([], "Desk Lamp", "192.168.1.50")
        device_id = devices[0]["id"]
        renamed = yd.update_device(devices, device_id, name="Office Lamp", ip="192.168.1.60")
        self.assertEqual(device_id, renamed[0]["id"])
        self.assertEqual("Office Lamp", renamed[0]["name"])
        self.assertEqual("192.168.1.60", renamed[0]["ip"])

    def test_the_display_name_is_never_the_identity(self):
        devices = yd.add_device([], "Desk Lamp", "192.168.1.50")
        self.assertNotIn("Desk Lamp", devices[0]["id"])


class TestDeviceListOperations(unittest.TestCase):
    """Add / edit / remove on the configured device list."""

    def base(self):
        return [device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A)]

    def test_adding_a_device_generates_a_stable_id(self):
        devices = yd.add_device(self.base(), "Lightstrip", "192.168.1.51")
        self.assertEqual(2, len(devices))
        self.assertTrue(yd.is_local_device_id(devices[1]["id"]))
        self.assertEqual("192.168.1.51", devices[1]["ip"])
        self.assertTrue(devices[1]["enabled"])

    def test_adding_a_duplicate_address_is_refused(self):
        with self.assertRaises(yd.DeviceError) as caught:
            yd.add_device(self.base(), "Second", "192.168.1.50")
        self.assertIn("already used by 'Desk Lamp'", str(caught.exception))

    def test_an_invalid_address_is_refused(self):
        for value in ("", "192.168.1", "not-an-ip"):
            with self.subTest(address=value):
                with self.assertRaises(yd.DeviceError):
                    yd.add_device([], "Desk Lamp", value)

    def test_a_blank_name_is_refused(self):
        with self.assertRaises(yd.DeviceError):
            yd.add_device([], "   ", "192.168.1.50")

    def test_editing_into_a_duplicate_address_is_refused(self):
        devices = [
            device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A),
            device("Lightstrip", "192.168.1.51"),
        ]
        with self.assertRaises(yd.DeviceError):
            yd.update_device(devices, devices[1]["id"], ip="192.168.1.50")
        # The rejected edit changed nothing.
        self.assertEqual("192.168.1.51", devices[1]["ip"])

    def test_removing_one_device_leaves_the_others(self):
        devices = [
            device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A),
            device("Lightstrip", "192.168.1.51"),
            device("Living Room", "192.168.1.52", enabled=False),
        ]
        remaining = yd.remove_device(devices, YEE_ID_A)
        self.assertEqual(["Lightstrip", "Living Room"], [item["name"] for item in remaining])
        self.assertEqual(3, len(devices), "the original list must not be mutated")

    def test_removing_everything_is_allowed(self):
        devices = [device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A)]
        self.assertEqual([], yd.remove_device(devices, YEE_ID_A))

    def test_duplicate_detection_normalizes_addresses(self):
        # IPv6 spellings that describe the same address, and surrounding
        # whitespace, must not hide a duplicate.
        devices = [
            device("Desk Lamp", "fe80::1"),
            device("Lightstrip", "fe80:0:0:0:0:0:0:1"),
        ]
        self.assertEqual({"fe80::1": [0, 1]}, yd.find_duplicate_ips(devices))
        self.assertEqual(
            {"192.168.1.50": [0, 1]},
            yd.find_duplicate_ips([device("A", "192.168.1.50"), device("B", " 192.168.1.50 ")]),
        )
        ids = yd.find_duplicate_ids([device("A", "10.0.0.1", device_id="same"),
                                     device("B", "10.0.0.2", device_id="same")])
        self.assertEqual({"same": [0, 1]}, ids)

    def test_the_summary_counts_enabled_and_configured(self):
        devices = [
            device("Desk Lamp", "192.168.1.50"),
            device("Lightstrip", "192.168.1.51"),
            device("Living Room", "192.168.1.52", enabled=False),
            {"id": "broken", "name": "Broken", "ip": "", "enabled": True},
        ]
        self.assertEqual("2 enabled / 4 configured", yd.device_summary(devices))
        self.assertEqual("none configured", yd.device_summary([]))


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-device-test-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def write_config(self, config):
        path = self.path(cm.CONFIG_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
        return path


class TestDeviceSchema(TempDirTestCase):
    """The v2 device list is validated strictly and never repaired silently."""

    def valid_config(self):
        config = cm.default_config()
        config["location"].update({"latitude": "11.1111", "longitude": "-22.2222"})
        config["lights"]["devices"] = [
            device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A),
            device("Lightstrip", "192.168.1.51"),
        ]
        return config

    def test_any_number_of_devices_is_valid(self):
        for count in (0, 1, 2, 7, 25):
            with self.subTest(count=count):
                config = self.valid_config()
                config["lights"]["devices"] = [
                    device(f"Device {index}", f"192.168.1.{index + 10}")
                    for index in range(count)
                ]
                self.assertEqual([], cm.validate_config(config).errors)

    def test_duplicate_device_ids_are_rejected(self):
        config = self.valid_config()
        config["lights"]["devices"][1]["id"] = YEE_ID_A
        errors = cm.validate_config(config).errors
        self.assertTrue(any("used by more than one entry" in error for error in errors), errors)

    def test_duplicate_addresses_are_rejected(self):
        config = self.valid_config()
        config["lights"]["devices"][1]["ip"] = "192.168.1.50"
        errors = cm.validate_config(config).errors
        self.assertTrue(any("same address" in error or "own address" in error for error in errors), errors)

    def test_the_same_address_in_a_different_spelling_is_still_a_duplicate(self):
        config = self.valid_config()
        config["lights"]["devices"][1]["ip"] = "fe80:0:0:0:0:0:0:1"
        config["lights"]["devices"][0]["ip"] = "fe80::1"
        errors = cm.validate_config(config).errors
        self.assertTrue(any("fe80::1" in error for error in errors), errors)

    def test_malformed_device_entries_are_rejected(self):
        broken_entries = {
            "not-an-object": "garbage",
            "missing id": {"name": "Desk Lamp", "ip": "192.168.1.50", "enabled": True},
            "blank id": {"id": "", "name": "Desk Lamp", "ip": "192.168.1.50", "enabled": True},
            "blank name": {"id": "manual:1", "name": "  ", "ip": "192.168.1.50", "enabled": True},
            "non-text name": {"id": "manual:1", "name": 5, "ip": "192.168.1.50", "enabled": True},
            "invalid address": {"id": "manual:1", "name": "Desk", "ip": "10.0.0.999", "enabled": True},
            "missing address": {"id": "manual:1", "name": "Desk", "enabled": True},
            "non-boolean enabled": {
                "id": "manual:1", "name": "Desk", "ip": "192.168.1.50", "enabled": "yes",
            },
            "non-text model": {
                "id": "manual:1", "name": "Desk", "ip": "192.168.1.50", "enabled": True, "model": 3,
            },
        }
        for label, entry in broken_entries.items():
            with self.subTest(case=label):
                config = self.valid_config()
                config["lights"]["devices"] = [entry]
                self.assertTrue(cm.validate_config(config).errors, label)

    def test_a_malformed_device_list_is_rejected_not_normalized(self):
        config = self.valid_config()
        config["lights"]["devices"] = "garbage"
        self.assertTrue(cm.validate_config(config).errors)

        config["lights"] = ["not", "an", "object"]
        self.assertTrue(cm.validate_config(config).errors)

    def test_a_missing_devices_key_is_not_a_valid_v2_configuration(self):
        # A v2 configuration always carries the device list; a missing one is
        # defaulted by normalization, a malformed one is rejected.
        config = self.valid_config()
        del config["lights"]["devices"]
        normalized = cm.normalize_config(config)
        self.assertEqual([], normalized["lights"]["devices"])

    def test_optional_discovery_metadata_is_accepted(self):
        config = self.valid_config()
        config["lights"]["devices"][0]["model"] = "color"
        config["lights"]["devices"][0]["firmware"] = "76"
        self.assertEqual([], cm.validate_config(config).errors)

    def test_v2_save_load_round_trip(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        config = self.valid_config()
        manager.save(config)
        loaded = manager.load()
        self.assertEqual(cm.CONFIG_VERSION, loaded["config_version"])
        self.assertEqual(config["lights"], loaded["lights"])
        self.assertEqual(2, len(loaded["lights"]["devices"]))

    def test_export_and_import_preserve_the_device_list(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        manager.save(self.valid_config())
        exported = manager.export_config(self.path("export.json"))

        other = cm.ConfigManager(self.path("other", cm.CONFIG_FILENAME))
        imported = other.import_config(exported)
        self.assertEqual(
            cm.configured_devices(manager.load()), cm.configured_devices(imported)
        )
        self.assertEqual(2, len(imported["lights"]["devices"]))

    def test_importing_a_malformed_device_list_changes_nothing(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        manager.save(self.valid_config())
        before = manager.load()

        broken = self.valid_config()
        broken["lights"]["devices"] = [{"id": "manual:1", "name": "", "ip": "nope", "enabled": True}]
        bad_path = self.write_config(broken)

        with self.assertRaises(cm.ConfigValidationError):
            manager.import_config(bad_path)
        self.assertEqual(before, manager.load())


class TestEnabledDeviceProjection(TempDirTestCase):
    """The runtime view: only enabled devices, only addresses, never raising."""

    def test_disabled_devices_are_excluded(self):
        config = cm.default_config()
        config["lights"]["devices"] = [
            device("Desk Lamp", "192.168.1.50"),
            device("Living Room", "192.168.1.52", enabled=False),
            device("Lightstrip", "192.168.1.51"),
        ]
        self.assertEqual(["192.168.1.50", "192.168.1.51"], cm.enabled_device_ips(config))
        self.assertEqual(
            ["Desk Lamp", "Lightstrip"],
            [entry["name"] for entry in cm.enabled_configured_devices(config)],
        )

    def test_an_empty_device_list_projects_to_nothing(self):
        self.assertEqual([], cm.enabled_device_ips(cm.default_config()))

    def test_malformed_entries_are_skipped_without_raising(self):
        config = cm.default_config()
        config["lights"]["devices"] = [
            "garbage",
            {"id": "manual:1", "name": "No address", "enabled": True},
            {"id": "manual:2", "name": "Not enabled", "ip": "192.168.1.60"},
            device("Desk Lamp", "192.168.1.50"),
            device("Duplicate", "192.168.1.50"),
        ]
        self.assertEqual(["192.168.1.50"], cm.enabled_device_ips(config))

    def test_a_malformed_lights_section_never_raises(self):
        config = cm.default_config()
        config["lights"] = "garbage"
        self.assertEqual([], cm.enabled_device_ips(config))
        self.assertEqual([], cm.configured_devices(config))
        self.assertEqual("none configured", cm.device_count_summary(config))

    def test_the_summary_string_is_factual(self):
        config = cm.default_config()
        config["lights"]["devices"] = [
            device("Desk Lamp", "192.168.1.50"),
            device("Lightstrip", "192.168.1.51"),
            device("Living Room", "192.168.1.52", enabled=False),
            device("Study", "192.168.1.53", enabled=False),
        ]
        self.assertEqual("2 enabled / 4 configured", cm.device_count_summary(config))


class TestV1ToV2Migration(TempDirTestCase):
    """Every legacy address-slot combination migrates without losing a device."""

    def v1(self, bulb_ip="", lightstrip_ip=""):
        config = cm.default_config()
        config["config_version"] = 1
        config["lights"] = {"bulb_ip": bulb_ip, "lightstrip_ip": lightstrip_ip}
        return config

    def test_no_addresses_migrate_to_an_empty_list(self):
        migrated, from_version = cm.migrate_config(self.v1())
        self.assertEqual(1, from_version)
        self.assertEqual([], migrated["lights"]["devices"])
        self.assertEqual([], cm.enabled_device_ips(migrated))
        self.assertEqual([], cm.validate_config(migrated).errors)

    def test_bulb_only(self):
        migrated, _ = cm.migrate_config(self.v1(bulb_ip="192.168.1.50"))
        devices = migrated["lights"]["devices"]
        self.assertEqual(1, len(devices))
        self.assertEqual("192.168.1.50", devices[0]["ip"])
        self.assertEqual("Yeelight Bulb", devices[0]["name"])
        self.assertTrue(devices[0]["enabled"])
        self.assertNotIn("bulb_ip", migrated["lights"])

    def test_lightstrip_only(self):
        migrated, _ = cm.migrate_config(self.v1(lightstrip_ip="192.168.1.51"))
        devices = migrated["lights"]["devices"]
        self.assertEqual(1, len(devices))
        self.assertEqual("192.168.1.51", devices[0]["ip"])
        self.assertEqual("Yeelight Lightstrip", devices[0]["name"])

    def test_both_devices(self):
        migrated, _ = cm.migrate_config(
            self.v1(bulb_ip="192.168.1.50", lightstrip_ip="192.168.1.51")
        )
        devices = migrated["lights"]["devices"]
        self.assertEqual(["192.168.1.50", "192.168.1.51"], [d["ip"] for d in devices])
        self.assertEqual(2, len({d["id"] for d in devices}), "ids must be unique")
        self.assertEqual([], cm.validate_config(migrated).errors)

    def test_a_repeated_address_becomes_one_device(self):
        migrated, _ = cm.migrate_config(
            self.v1(bulb_ip="192.168.1.50", lightstrip_ip="192.168.1.50")
        )
        self.assertEqual(1, len(migrated["lights"]["devices"]))
        self.assertEqual(["192.168.1.50"], cm.enabled_device_ips(migrated))
        # The same address in a different spelling is still the same device.
        migrated, _ = cm.migrate_config(
            self.v1(bulb_ip="fe80::1", lightstrip_ip="fe80:0:0:0:0:0:0:1")
        )
        self.assertEqual(1, len(migrated["lights"]["devices"]))

    def test_migrated_devices_are_enabled_and_named(self):
        # A user who only ever configured one slot keeps exactly that one device.
        migrated, _ = cm.migrate_config(self.v1(bulb_ip="10.0.0.5"))
        self.assertEqual(
            [{"ip": "10.0.0.5", "name": "Yeelight Bulb", "enabled": True}],
            [
                {"ip": d["ip"], "name": d["name"], "enabled": d["enabled"]}
                for d in migrated["lights"]["devices"]
            ],
        )

    def test_a_v0_config_migrates_all_the_way(self):
        raw = {
            "location": {"latitude": "11.1111", "longitude": "-22.2222"},
            "lights": {"bulb_ip": "192.168.1.50", "lightstrip_ip": ""},
            "paths": {"openrgb": "C:\\Program Files\\OpenRGB\\OpenRGB.exe"},
        }
        migrated, from_version = cm.migrate_config(raw)
        self.assertEqual(0, from_version)
        self.assertEqual(cm.CONFIG_VERSION, migrated["config_version"])
        self.assertEqual(["192.168.1.50"], cm.enabled_device_ips(migrated))
        self.assertTrue(migrated["integrations"]["openrgb"]["enabled"])
        self.assertEqual([], cm.validate_config(migrated).errors)

    def test_a_hybrid_file_keeps_both_the_device_list_and_the_legacy_addresses(self):
        raw = self.v1(bulb_ip="10.0.0.1", lightstrip_ip="10.0.0.2")
        raw["lights"]["devices"] = [
            {"id": "manual:existing", "name": "Existing", "ip": "10.0.0.1", "enabled": True}
        ]
        migrated, _ = cm.migrate_config(raw)
        devices = migrated["lights"]["devices"]
        # The existing entry wins for its address; the other legacy address is
        # appended instead of being dropped.
        self.assertEqual(["Existing", "Yeelight Lightstrip"], [d["name"] for d in devices])
        self.assertEqual(["10.0.0.1", "10.0.0.2"], cm.enabled_device_ips(migrated))
        self.assertEqual([], cm.validate_config(migrated).errors)

    def test_migration_is_persisted_with_a_backup(self):
        path = self.write_config(self.v1(bulb_ip="192.168.1.50", lightstrip_ip="192.168.1.51"))
        manager = cm.ConfigManager(path)

        migrated = manager.load()
        self.assertEqual(1, manager.last_load_migrated_from)
        manager.save(migrated, create_backup=True)

        on_disk = self.read_json(path)
        self.assertEqual(cm.CONFIG_VERSION, on_disk["config_version"])
        self.assertEqual(2, len(on_disk["lights"]["devices"]))
        backup = self.read_json(path + cm.BACKUP_SUFFIX)
        self.assertEqual(1, backup["config_version"])
        self.assertEqual("192.168.1.50", backup["lights"]["bulb_ip"])

    def test_the_runtime_loader_migrates_the_same_way(self):
        path = self.write_config(self.v1(bulb_ip="192.168.1.50", lightstrip_ip="192.168.1.51"))
        runtime = cm.ConfigManager(path).load_runtime()
        self.assertEqual(["192.168.1.50", "192.168.1.51"], cm.enabled_device_ips(runtime))
        # The runtime read never rewrites the file.
        self.assertEqual(1, self.read_json(path)["config_version"])

    def test_importing_a_v1_configuration_migrates_it(self):
        legacy_path = self.write_config(self.v1(bulb_ip="192.168.1.50", lightstrip_ip=""))
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        imported = manager.import_config(legacy_path)
        self.assertEqual(cm.CONFIG_VERSION, imported["config_version"])
        self.assertEqual(["192.168.1.50"], cm.enabled_device_ips(imported))
        self.assertEqual(imported, manager.load())


class TestDiscoveryNormalization(unittest.TestCase):
    """Discovery results are normalized, de-duplicated and never faked."""

    def test_multiple_devices_are_normalized(self):
        devices = report_with(
            bulb("192.168.1.50", device_id="0x00000000037073d2", model="color"),
            bulb("192.168.1.51", device_id="0x0000000003b0cb6d", model="stripe", power="off"),
        )
        self.assertEqual(2, len(devices))
        self.assertEqual(["192.168.1.50", "192.168.1.51"], [d["ip"] for d in devices])
        self.assertEqual(YEE_ID_A, devices[0]["id"])
        self.assertEqual("color", devices[0]["model"])
        self.assertEqual("76", devices[0]["firmware"])
        self.assertEqual("on", devices[0]["power"])
        self.assertEqual("off", devices[1]["power"])
        self.assertEqual(55443, devices[0]["port"])

    def test_duplicate_responses_collapse_into_one_device(self):
        # The same device answering twice, and a second response that only
        # differs by the address spelling.
        devices = report_with(
            bulb("192.168.1.50"),
            bulb("192.168.1.50"),
            bulb("192.168.001.050"),
        )
        self.assertEqual(1, len(devices))

    def test_a_device_without_an_id_is_still_reported(self):
        devices = report_with(bulb("192.168.1.50", device_id=None))
        self.assertEqual(1, len(devices))
        self.assertIsNone(devices[0]["id"])
        # The selection key falls back to the address so it stays stable.
        self.assertEqual("ip:192.168.1.50", devices[0]["key"])

    def test_useless_responses_are_dropped(self):
        raw = [
            "garbage",
            {"ip": "not-an-ip", "capabilities": {"id": "0x1"}},
            {"ip": "192.168.1.50"},
            {"ip": "192.168.1.60", "capabilities": {"id": "0x2", "name": "  "}},
        ]
        devices = yd.normalize_discovery_results(raw)
        # Unusable responses are dropped; an address with no capabilities at all
        # is still a reachable device and is kept without metadata.
        self.assertEqual(["192.168.1.50", "192.168.1.60"], [d["ip"] for d in devices])
        # An empty reported name is not presented as a name.
        self.assertIsNone(devices[1]["name"])

    def test_the_address_falls_back_to_the_location_header(self):
        raw = [
            {
                "capabilities": {
                    "id": "0x3",
                    "Location": "yeelight://192.168.1.70:55443",
                }
            }
        ]
        devices = yd.normalize_discovery_results(raw)
        self.assertEqual(["192.168.1.70"], [d["ip"] for d in devices])

    def test_a_friendly_default_name_is_derived(self):
        self.assertEqual("Kitchen", yd.default_device_name("Kitchen", "color"))
        self.assertEqual("Yeelight color", yd.default_device_name("", "color"))
        self.assertEqual(yd.DEFAULT_DEVICE_NAME, yd.default_device_name(None, None))


class TestDiscoveryMatching(unittest.TestCase):
    """Matching a discovery result against the configured device list."""

    def test_a_new_device_is_new(self):
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50")), [])
        self.assertEqual(1, len(plan))
        self.assertEqual(yd.MATCH_NEW, plan[0]["state"])
        self.assertTrue(yd.selectable_match(plan[0]))

    def test_an_already_configured_device_is_recognized(self):
        configured = [device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A)]
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50")), configured)
        self.assertEqual(yd.MATCH_ALREADY_ADDED, plan[0]["state"])
        self.assertFalse(yd.selectable_match(plan[0]))

    def test_the_same_device_id_at_a_new_address_is_an_update(self):
        configured = [device("Desk Lamp", "192.168.1.99", device_id=YEE_ID_A)]
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50")), configured)
        self.assertEqual(yd.MATCH_IP_CHANGED, plan[0]["state"])
        self.assertEqual("192.168.1.99", plan[0]["previous_ip"])

        updated = yd.apply_discovery_plan(configured, plan, [YEE_ID_A])
        self.assertEqual(1, len(updated), "an address change must not add a device")
        self.assertEqual("192.168.1.50", updated[0]["ip"])
        self.assertEqual(YEE_ID_A, updated[0]["id"])
        self.assertEqual("Desk Lamp", updated[0]["name"])

    def test_a_manually_added_device_can_adopt_its_yeelight_id(self):
        manual = device("Desk Lamp", "192.168.1.50")
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50")), [manual])
        self.assertEqual(yd.MATCH_ID_AVAILABLE, plan[0]["state"])

        updated = yd.apply_discovery_plan([manual], plan, [YEE_ID_A])
        self.assertEqual(1, len(updated))
        self.assertEqual(YEE_ID_A, updated[0]["id"])
        self.assertEqual("Desk Lamp", updated[0]["name"], "the user's name is kept")

    def test_the_id_wins_over_the_address(self):
        # The device moved to an address that another entry never had, while a
        # *different* device sits at the old address: the id decides.
        configured = [
            device("Desk Lamp", "192.168.1.99", device_id=YEE_ID_A),
            device("Lightstrip", "192.168.1.50", device_id=YEE_ID_B),
        ]
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50")), configured)
        self.assertEqual(yd.MATCH_IP_CHANGED, plan[0]["state"])
        self.assertEqual(0, plan[0]["configured_index"])

    def test_applying_a_selection_adds_only_what_was_selected(self):
        discovered = report_with(
            bulb("192.168.1.50", device_id="0x00000000037073d2"),
            bulb("192.168.1.51", device_id="0x0000000003b0cb6d"),
            bulb("192.168.1.52", device_id="0x0000000003c1a1a1"),
        )
        plan = yd.plan_discovery(discovered, [])
        updated = yd.apply_discovery_plan([], plan, [YEE_ID_A, "yeelight:0x0000000003c1a1a1"])
        self.assertEqual(["192.168.1.50", "192.168.1.52"], [d["ip"] for d in updated])
        self.assertEqual(YEE_ID_A, updated[0]["id"])

    def test_selected_names_are_used(self):
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50")), [])
        updated = yd.apply_discovery_plan(
            [], plan, [YEE_ID_A], {YEE_ID_A: "Kitchen Ceiling"}
        )
        self.assertEqual("Kitchen Ceiling", updated[0]["name"])

    def test_a_discovered_name_is_used_when_the_device_reports_one(self):
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50", name="Hallway")), [])
        updated = yd.apply_discovery_plan([], plan)
        self.assertEqual("Hallway", updated[0]["name"])

    def test_discovery_metadata_is_stored_but_nothing_else_is(self):
        plan = yd.plan_discovery(report_with(bulb("192.168.1.50")), [])
        updated = yd.apply_discovery_plan([], plan)
        entry = updated[0]
        self.assertEqual("color", entry["model"])
        self.assertEqual("76", entry["firmware"])
        # The raw discovery response is not dumped into the configuration.
        self.assertNotIn("power", entry)
        self.assertNotIn("port", entry)
        self.assertNotIn("bright", entry)
        self.assertNotIn("support", entry)

    def test_applying_cannot_produce_duplicate_addresses(self):
        # Device A moved onto the address of device C: applying that update is
        # refused instead of silently producing two devices at one address.
        configured = [
            device("A", "192.168.1.50", device_id=YEE_ID_A),
            device("C", "192.168.1.51", device_id="manual:cccc"),
        ]
        plan = yd.plan_discovery(report_with(bulb("192.168.1.51")), configured)
        self.assertEqual(yd.MATCH_IP_CHANGED, plan[0]["state"])
        with self.assertRaises(yd.DeviceError):
            yd.apply_discovery_plan(configured, plan)


class TestDiscoveryFailurePaths(unittest.TestCase):
    """Zero results and network failures are normal, not exceptions."""

    def test_zero_results_is_not_an_error(self):
        with mock.patch("yeelight.discover_bulbs", return_value=[]):
            report = yd.discover_devices(timeout=0.5)
        self.assertEqual([], report.devices)
        self.assertEqual("", report.error)
        self.assertFalse(report)

    def test_a_network_error_is_reported_gracefully(self):
        with mock.patch("yeelight.discover_bulbs", side_effect=OSError("network unreachable")):
            report = yd.discover_devices(timeout=0.5)
        self.assertEqual([], report.devices)
        self.assertIn("network unreachable", report.error)
        self.assertIn("Firewall", report.error)

    def test_a_missing_library_is_reported(self):
        with mock.patch.dict(sys.modules, {"yeelight": None}):
            report = yd.discover_devices(timeout=0.5)
        self.assertEqual([], report.devices)
        self.assertTrue(report.error)

    def test_the_timeout_is_bounded(self):
        recorded = {}

        def fake_discover(timeout=None, interface=False):
            recorded["timeout"] = timeout
            return []

        with mock.patch("yeelight.discover_bulbs", side_effect=fake_discover):
            yd.discover_devices(timeout=999)
            self.assertEqual(yd.DISCOVERY_MAX_TIMEOUT_SECONDS, recorded["timeout"])
            yd.discover_devices(timeout=0.0001)
            self.assertEqual(yd.DISCOVERY_MIN_TIMEOUT_SECONDS, recorded["timeout"])
            yd.discover_devices(timeout="not-a-number")
            self.assertEqual(yd.DISCOVERY_TIMEOUT_SECONDS, recorded["timeout"])


@unittest.skipIf(ui is None, SKIP_REASON)
class TestDiscoveryWorker(unittest.TestCase):
    """The worker thread offloads discovery and always reports back."""

    def test_the_worker_returns_every_device(self):
        raw = [bulb("192.168.1.50"), bulb("192.168.1.51", device_id="0x0000000003b0cb6d")]
        worker = ui.DeviceDiscoveryThread(timeout=0.5)
        collected = []
        worker.result_ready.connect(collected.append)
        with mock.patch("yeelight.discover_bulbs", return_value=raw):
            worker.run()
        self.assertEqual(1, len(collected))
        self.assertEqual(2, len(collected[0].devices))
        self.assertEqual("", collected[0].error)

    def test_the_worker_reports_zero_devices(self):
        worker = ui.DeviceDiscoveryThread(timeout=0.5)
        collected = []
        worker.result_ready.connect(collected.append)
        with mock.patch("yeelight.discover_bulbs", return_value=[]):
            worker.run()
        self.assertEqual(1, len(collected))
        self.assertEqual([], collected[0].devices)

    def test_the_worker_reports_a_failure_instead_of_raising(self):
        worker = ui.DeviceDiscoveryThread(timeout=0.5)
        collected = []
        worker.result_ready.connect(collected.append)
        with mock.patch("yeelight.discover_bulbs", side_effect=OSError("blocked")):
            worker.run()
        self.assertEqual(1, len(collected))
        self.assertEqual([], collected[0].devices)
        self.assertIn("blocked", collected[0].error)

    def test_an_unexpected_worker_error_is_still_reported(self):
        worker = ui.DeviceDiscoveryThread(timeout=0.5)
        collected = []
        worker.result_ready.connect(collected.append)
        # The deliberate failure is logged as well as reported.
        with self.assertLogs(level="ERROR"):
            with mock.patch.object(ui, "discover_devices", side_effect=RuntimeError("boom")):
                worker.run()
        self.assertEqual(1, len(collected))
        self.assertIn("boom", collected[0].error)


# ---------------------------------------------------------
# The discovery guard: a timed-out search must never freeze the GUI
# ---------------------------------------------------------
# `run_discovery()` used to wait for the worker a second time after its guard
# had already fired (`worker.wait(timeout + grace)` on the GUI thread), which
# could block the GUI for another full timeout — and could leave a still-running
# QThread to be destroyed with its Python reference. The machinery below drives
# the real guard with a fake progress dialog and a search the test controls.

_QT_APPLICATION = None


def report_of(*bulbs):
    """A successful `DiscoveryReport` carrying the given raw SSDP responses."""
    return yd.DiscoveryReport(report_with(*bulbs), "")


def qt_application():
    """A QApplication for the guard tests (offscreen, created once)."""
    global _QT_APPLICATION
    if _QT_APPLICATION is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        _QT_APPLICATION = QApplication.instance() or QApplication([])
    return _QT_APPLICATION


def pump_events(predicate, timeout_ms=5000):
    """Process events until `predicate()` holds (or the bound expires)."""
    from PyQt6.QtCore import QCoreApplication, QThread

    waited = 0
    while waited < timeout_ms:
        QCoreApplication.processEvents()
        if predicate():
            return True
        QThread.msleep(10)
        waited += 10
    return bool(predicate())


class RecordingProgressDialog:
    """A stand-in for the modal progress dialog (no widget, records the calls)."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.shown = False
        self.closed = False
        RecordingProgressDialog.instances.append(self)

    def palette(self):
        from PyQt6.QtGui import QPalette

        return QPalette()

    def setWindowTitle(self, *args):
        pass

    def setWindowModality(self, *args):
        pass

    def setCancelButton(self, *args):
        pass

    def setMinimumDuration(self, *args):
        pass

    def setAutoClose(self, *args):
        pass

    def setAutoReset(self, *args):
        pass

    def setPalette(self, *args):
        pass

    def setStyleSheet(self, *args):
        pass

    def show(self):
        self.shown = True

    def close(self):
        self.closed = True


def recording_worker_class(created):
    """The real discovery worker, recorded, so a test can inspect its thread."""

    class RecordedDiscoveryWorker(ui.DeviceDiscoveryThread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.waits = []
            self.terminated = 0
            created.append(self)

        def wait(self, *args, **kwargs):
            self.waits.append(args[0] if args else kwargs.get("timeout"))
            return super().wait(*args, **kwargs)

        def terminate(self):
            self.terminated += 1
            return super().terminate()

    return RecordedDiscoveryWorker


@unittest.skipIf(ui is None, SKIP_REASON)
class TestDiscoveryGuard(unittest.TestCase):
    """`run_discovery()` returns promptly, whatever the worker does."""

    def setUp(self):
        qt_application()
        self.workers = []
        self.gates = []
        self.dialogs = []
        RecordingProgressDialog.instances = self.dialogs

        self.patch(ui, "DeviceDiscoveryThread", recording_worker_class(self.workers))
        self.patch(ui, "QProgressDialog", RecordingProgressDialog)
        self.addCleanup(self.release_workers)

    def patch(self, target, attribute, new):
        patcher = mock.patch.object(target, attribute, new)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def patch_search(self, report=None):
        """Replace the search itself; `state["report"]` can be changed later."""
        state = {"report": report if report is not None else yd.DiscoveryReport([], ""), "gate": None}

        def search(timeout=None, interface=False):
            if state["gate"] is not None:
                # Bounded for real, so a broken test can never hang the suite.
                state["gate"].wait(5)
            return state["report"]

        self.patch(ui, "discover_devices", search)
        return state

    def blocked_search(self, report=None):
        """A search that does not return until the test releases its gate."""
        gate = threading.Event()
        self.gates.append(gate)
        state = self.patch_search(report)
        state["gate"] = gate
        return gate, state

    def patch_guard(self, milliseconds=50):
        """Fire the UI guard promptly; its arithmetic is asserted separately."""
        calls = []
        self.patch(ui, "discovery_guard_milliseconds", lambda timeout: calls.append(timeout) or milliseconds)
        return calls

    def release_workers(self):
        """Let every worker finish so no thread outlives its test."""
        for gate in self.gates:
            gate.set()
        for worker in list(self.workers):
            try:
                worker.wait(5000)
            except RuntimeError:  # pragma: no cover - already deleted
                pass
        pump_events(lambda: not any(self.is_retained(worker) for worker in self.workers), timeout_ms=200)

    def retained_ids(self):
        """The handed-out worker objects by identity — never compares wrappers."""
        return [id(worker) for worker in ui.LIVE_DISCOVERY_WORKERS]

    def is_retained(self, worker):
        return id(worker) in self.retained_ids()

    @property
    def progress(self):
        return self.dialogs[0]

    def test_a_reported_result_is_returned_and_closes_the_dialog_promptly(self):
        self.patch_search(report_of(bulb("192.168.1.50")))
        started = time.perf_counter()

        report = ui.run_discovery(timeout=0.5)
        elapsed = time.perf_counter() - started

        self.assertEqual(1, len(report.devices))
        self.assertEqual("", report.error)
        # The guard would have waited 5.5 s; a reported result must not wait for
        # it, and no synchronous `wait()` may be performed at all.
        self.assertLess(elapsed, 1.0)
        self.assertEqual([], self.workers[0].waits)
        self.assertEqual(0, self.workers[0].terminated)
        self.assertEqual(0.5, self.workers[0].timeout)
        self.assertTrue(self.progress.shown)
        self.assertTrue(self.progress.closed)

    def test_zero_results_are_returned_as_usual(self):
        self.patch_search(yd.DiscoveryReport([], ""))

        report = ui.run_discovery(timeout=0.5)

        self.assertEqual([], report.devices)
        self.assertEqual("", report.error)
        self.assertTrue(self.progress.closed)

    def test_a_network_error_is_returned_as_usual(self):
        self.patch_search(yd.DiscoveryReport([], "Windows Firewall is blocking the search."))

        report = ui.run_discovery(timeout=0.5)

        self.assertEqual([], report.devices)
        self.assertIn("Firewall", report.error)
        self.assertTrue(self.progress.closed)

    def test_the_guard_returns_a_timeout_report_without_waiting_again(self):
        self.patch_guard()
        self.blocked_search(report_of(bulb("192.168.1.50")))
        started = time.perf_counter()

        report = ui.run_discovery(timeout=0.5)
        elapsed = time.perf_counter() - started

        self.assertEqual([], report.devices)
        self.assertEqual(ui.DISCOVERY_TIMEOUT_TEXT, report.error)
        # The regression: the old code waited another `timeout + grace` (5.5 s
        # here) on the GUI thread after its guard had already fired.
        self.assertLess(elapsed, 1.0)
        self.assertEqual([], self.workers[0].waits, "the guard waited for the worker anyway")
        self.assertEqual(0, self.workers[0].terminated, "the guard terminated the worker")
        self.assertTrue(self.progress.closed, "the progress dialog was left on screen")

    def test_a_timed_out_worker_stays_alive_and_referenced(self):
        self.patch_guard()
        self.blocked_search()

        ui.run_discovery(timeout=0.5)

        worker = self.workers[0]
        self.assertTrue(self.is_retained(worker), "the abandoned worker was released while running")
        self.assertTrue(worker.isRunning(), "the abandoned worker was not left running")
        # Still a usable object: it was neither destroyed nor terminated.
        self.assertEqual(0.5, worker.timeout)
        self.assertFalse(worker.isFinished())

    def test_a_late_result_of_a_timed_out_worker_cannot_reach_the_ui(self):
        self.patch_guard()
        gate, state = self.blocked_search(report_of(bulb("192.168.1.50")))

        timed_out = ui.run_discovery(timeout=0.5)

        worker = self.workers[0]
        self.assertEqual(0, worker.receivers(worker.result_ready), "the stale result is still wired up")

        # The abandoned search finishes late, reporting a device ...
        gate.set()
        self.assertTrue(
            pump_events(lambda: not self.is_retained(worker)), "the released worker never finished"
        )
        # ... and a *new* discovery still reports only its own result.
        state["report"] = report_of(bulb("192.168.1.51", device_id=YEE_ID_B))
        second = ui.run_discovery(timeout=0.5)

        self.assertEqual([], timed_out.devices)
        self.assertEqual(1, len(second.devices))
        self.assertEqual("192.168.1.51", second.devices[0]["ip"])
        self.assertEqual(2, len(self.workers))

    def test_a_finished_timed_out_worker_is_cleaned_up(self):
        self.patch_guard()
        gate, _ = self.blocked_search()

        ui.run_discovery(timeout=0.5)
        worker = self.workers[0]
        self.assertTrue(self.is_retained(worker))

        gate.set()

        self.assertTrue(
            pump_events(lambda: not self.is_retained(worker)),
            "the finished worker was retained forever",
        )

    def test_the_guard_is_bounded_by_the_clamped_timeout(self):
        grace = ui.DISCOVERY_UI_GRACE_SECONDS
        self.assertEqual(
            int((yd.DISCOVERY_TIMEOUT_SECONDS + grace) * 1000),
            ui.discovery_guard_milliseconds("not-a-number"),
        )
        self.assertEqual(
            int((yd.DISCOVERY_TIMEOUT_SECONDS + grace) * 1000),
            ui.discovery_guard_milliseconds(None),
        )
        self.assertEqual(
            int((yd.DISCOVERY_MAX_TIMEOUT_SECONDS + grace) * 1000),
            ui.discovery_guard_milliseconds(999),
        )
        self.assertEqual(
            int((yd.DISCOVERY_MIN_TIMEOUT_SECONDS + grace) * 1000),
            ui.discovery_guard_milliseconds(0.0001),
        )
        # No caller can create the ~1004-second modal guard of the old code.
        self.assertLess(ui.discovery_guard_milliseconds(999), 1004 * 1000)

    def test_the_same_bounded_timeout_reaches_the_worker_and_the_guard(self):
        self.patch_search(report_of(bulb("192.168.1.50")))
        calls = []
        real_guard = ui.discovery_guard_milliseconds

        def record(timeout):
            value = real_guard(timeout)
            calls.append((timeout, value))
            return value

        self.patch(ui, "discovery_guard_milliseconds", record)
        cases = (
            (999, yd.DISCOVERY_MAX_TIMEOUT_SECONDS),
            (0.0001, yd.DISCOVERY_MIN_TIMEOUT_SECONDS),
            ("nonsense", yd.DISCOVERY_TIMEOUT_SECONDS),
        )

        for requested, expected in cases:
            with self.subTest(timeout=requested):
                calls.clear()

                report = ui.run_discovery(timeout=requested)

                self.assertEqual(1, len(report.devices))
                # The search and the UI guard are given the *same bounded* value.
                self.assertEqual(expected, self.workers[-1].timeout)
                self.assertEqual(1, len(calls))
                self.assertEqual(expected, calls[0][0])
                self.assertEqual(
                    int((expected + ui.DISCOVERY_UI_GRACE_SECONDS) * 1000), calls[0][1]
                )


@unittest.skipIf(ui is None, SKIP_REASON)
class TestDiscoveryInteractiveFlow(unittest.TestCase):
    """The dialog flow adds what was selected and leaves everything else alone."""

    def run_flow(self, raw_bulbs, configured, selection, names=None):
        plan = yd.plan_discovery(yd.normalize_discovery_results(raw_bulbs), configured)
        selection = selection(plan) if callable(selection) else selection
        return yd.apply_discovery_plan(configured, plan, selection, names or {})

    def test_a_new_device_is_added_with_its_discovered_identity(self):
        updated = self.run_flow([bulb("192.168.1.50")], [], None, None)
        self.assertEqual(1, len(updated))
        self.assertEqual(YEE_ID_A, updated[0]["id"])
        self.assertEqual("Yeelight color", updated[0]["name"])

    def test_an_already_configured_device_is_not_duplicated(self):
        configured = [device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A)]
        plan = yd.plan_discovery(yd.normalize_discovery_results([bulb("192.168.1.50")]), configured)
        self.assertFalse(yd.selectable_match(plan[0]))
        updated = yd.apply_discovery_plan(configured, plan, [YEE_ID_A])
        self.assertEqual(1, len(updated))

    def test_the_empty_results_message_is_explicit(self):
        self.assertIn("No Yeelight devices were found", ui.DISCOVERY_EMPTY_TEXT)
        self.assertIn("LAN Control", ui.DISCOVERY_EMPTY_TEXT)
        self.assertIn("same network", ui.DISCOVERY_EMPTY_TEXT)


class TestRealDiscoveryIsNotUsedInTests(unittest.TestCase):
    """A guard: the unit suite must never broadcast on the real network."""

    def test_discovery_is_only_reachable_through_the_patched_call(self):
        calls = []

        def fake_discover(timeout=None, interface=False):
            calls.append(timeout)
            return []

        with mock.patch("yeelight.discover_bulbs", side_effect=fake_discover):
            yd.discover_devices(timeout=1)
        self.assertEqual([1], calls)


@unittest.skipIf(wizard is None, WIZARD_SKIP_REASON)
class TestWizardDevicePage(unittest.TestCase):
    """First-run setup: zero devices is fine, and any number of devices works."""

    class PageStub:
        validatePage = wizard.DevicesPage.validatePage if wizard is not None else None

    def page(self, devices, working=None):
        stub = self.PageStub()
        stub.device_list = types.SimpleNamespace(devices=lambda: [dict(d) for d in devices])
        stub.wizard = types.SimpleNamespace(
            working=working if working is not None else cm.default_config()
        )
        return stub

    def test_setup_can_finish_with_no_yeelight_device(self):
        page = self.page([])
        self.assertTrue(page.validatePage())
        self.assertEqual([], page.wizard.working["lights"]["devices"])
        self.assertEqual([], cm.validate_config(page.wizard.working).errors)

    def test_setup_stores_every_configured_device(self):
        devices = [
            device("Desk Lamp", "192.168.1.50", device_id=YEE_ID_A),
            device("Lightstrip", "192.168.1.51"),
            device("Living Room", "192.168.1.52", enabled=False),
        ]
        page = self.page(devices)
        self.assertTrue(page.validatePage())
        self.assertEqual(devices, page.wizard.working["lights"]["devices"])
        self.assertEqual([], cm.validate_config(page.wizard.working).errors)

    def test_discovered_devices_flow_into_the_wizard_configuration(self):
        """A discovered device and a manually added one end up in the setup."""
        discovered = yd.normalize_discovery_results(
            [bulb("192.168.1.50"), bulb("192.168.1.51", device_id="0x0000000003b0cb6d")]
        )
        plan = yd.plan_discovery(discovered, [])
        with_discovered = yd.apply_discovery_plan([], plan)
        with_manual = yd.add_device(with_discovered, "Hallway", "192.168.1.60")

        page = self.page(with_manual)
        self.assertTrue(page.validatePage())

        stored = page.wizard.working["lights"]["devices"]
        self.assertEqual(3, len(stored))
        self.assertEqual(["192.168.1.50", "192.168.1.51", "192.168.1.60"], cm.enabled_device_ips(page.wizard.working))
        self.assertIn(YEE_ID_A, [entry["id"] for entry in stored])

    def test_a_malformed_device_entry_blocks_the_page(self):
        page = self.page([{"id": "manual:1", "name": "", "ip": "nope", "enabled": True}])
        with mock.patch.object(wizard.QMessageBox, "warning") as warning:
            self.assertFalse(page.validatePage())
        warning.assert_called_once()


class TestShippedExampleConfiguration(TempDirTestCase):
    """The safe template must stay free of real addresses and device ids."""

    def example_path(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            cm.CONFIG_EXAMPLE_FILENAME,
        )

    def test_the_example_is_a_safe_v2_template(self):
        example = self.read_json(self.example_path())
        self.assertEqual(cm.CONFIG_VERSION, example["config_version"])
        self.assertEqual({"devices": []}, example["lights"])
        self.assertEqual(cm.default_config(), example)

    def test_the_example_contains_no_address_or_device_id(self):
        with open(self.example_path(), "r", encoding="utf-8") as handle:
            raw = handle.read()
        for marker in ("192.168", "10.0.0", "manual:", "yeelight:", "0x0000", "fw_ver"):
            self.assertNotIn(marker, raw, marker)
        # No placeholder coordinates that look like a real location either.
        example = self.read_json(self.example_path())
        self.assertEqual("0.0000", example["location"]["latitude"])
        self.assertEqual("0.0000", example["location"]["longitude"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
