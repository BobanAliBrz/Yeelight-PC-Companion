"""Unit tests for the pure configuration logic in `config_manager`.

These tests deliberately avoid the GUI and any Windows-specific sleep/wake
behavior: they only exercise defaults, migration, validation, safe saves,
backups, and import/export.

Run with:  python -m unittest discover -s tests -t . -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_manager as cm  # noqa: E402
import yeelight_devices as yd  # noqa: E402


def device(name, ip, enabled=True, device_id=None, **extra):
    """One configured device entry (the canonical shape)."""
    device_id = device_id or yd.new_local_device_id()
    entry = {"id": device_id, "name": name, "ip": ip, "enabled": enabled}
    entry.update(extra)
    return entry


def device_config(addresses, enabled=True):
    """A valid configuration with one device per address."""
    config = cm.default_config()
    config["location"].update({"latitude": "11.1111", "longitude": "-22.2222"})
    config["lights"]["devices"] = [
        device(f"Device {index}", address, enabled=enabled)
        for index, address in enumerate(addresses, start=1)
    ]
    return config


def legacy_v0_config():
    """A realistic pre-versioning configuration (maintainer-style setup)."""
    return {
        "location": {
            "latitude": "11.1111",
            "longitude": "-22.2222",
            "elevation": 123.0,
            "light_buffer_hours": 2.0,
        },
        "lights": {
            "bulb_ip": "192.0.2.26",
            "lightstrip_ip": "192.0.2.27",
        },
        "paths": {
            "openrgb": "C:\\Program Files\\OpenRGB\\OpenRGB.exe",
            "yeelight_connector": "C:\\Program Files\\Yeelight\\Connector.exe",
            "razer_synapse": "",
            "artemis": "C:\\Program Files\\Artemis\\Artemis.UI.Windows.exe",
        },
        "automation": {
            "close_apps_on_sleep": True,
            "turn_off_yeelight_on_sleep": True,
            "restore_apps_on_wake": True,
            "turn_on_yeelight_on_wake_night": True,
            "force_silent_launch": True,
            "launch_razer_synapse": False,
            "wait_for_razer_synapse": True,
            "razer_synapse_timeout_seconds": 45.0,
        },
    }


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-config-test-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)

    def write_json(self, path, data):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)
        return path

    def read_raw(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)


class TestDefaults(TempDirTestCase):
    def test_defaults_produce_valid_config(self):
        result = cm.validate_config(cm.default_config())
        self.assertEqual([], result.errors)
        self.assertTrue(result.ok)

    def test_defaults_are_safe_placeholders(self):
        defaults = cm.default_config()
        self.assertEqual(cm.CONFIG_VERSION, defaults["config_version"])
        for value in defaults["paths"].values():
            self.assertEqual("", value)
        # A fresh configuration contains no device at all: no address, no id.
        self.assertEqual({"devices": []}, defaults["lights"])
        self.assertEqual([], cm.enabled_device_ips(defaults))
        for key in cm.INTEGRATION_KEYS:
            self.assertFalse(defaults["integrations"][key]["enabled"])

    def test_default_config_is_a_copy(self):
        first = cm.default_config()
        first["location"]["latitude"] = "12.0"
        self.assertEqual("0.0000", cm.default_config()["location"]["latitude"])

    def test_defaults_match_shipped_example(self):
        example_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            cm.CONFIG_EXAMPLE_FILENAME,
        )
        with open(example_path, "r", encoding="utf-8") as handle:
            example = json.load(handle)
        self.assertEqual(cm.default_config(), example)


class TestMigration(TempDirTestCase):
    def test_legacy_v0_config_migrates_to_the_current_version(self):
        migrated, from_version = cm.migrate_config(legacy_v0_config())
        self.assertEqual(0, from_version)
        self.assertEqual(cm.CONFIG_VERSION, migrated["config_version"])
        self.assertEqual([], cm.validate_config(migrated).errors)
        # Both legacy address slots became devices, and the legacy keys are gone.
        self.assertNotIn("bulb_ip", migrated["lights"])
        self.assertNotIn("lightstrip_ip", migrated["lights"])
        self.assertEqual(["192.0.2.26", "192.0.2.27"], cm.enabled_device_ips(migrated))
        self.assertEqual(
            ["Yeelight Bulb", "Yeelight Lightstrip"],
            [device["name"] for device in cm.configured_devices(migrated)],
        )

    def test_configured_integrations_migrate_enabled(self):
        migrated, _ = cm.migrate_config(legacy_v0_config())
        self.assertTrue(migrated["integrations"]["openrgb"]["enabled"])
        self.assertTrue(migrated["integrations"]["yeelight_connector"]["enabled"])
        self.assertTrue(migrated["integrations"]["artemis"]["enabled"])
        # No executable path was configured, so Razer Synapse stays disabled.
        self.assertFalse(migrated["integrations"]["razer_synapse"]["enabled"])

    def test_blank_integrations_migrate_disabled(self):
        raw = legacy_v0_config()
        raw["paths"] = {key: "" for key in cm.INTEGRATION_KEYS}
        migrated, _ = cm.migrate_config(raw)
        for key in cm.INTEGRATION_KEYS:
            self.assertFalse(migrated["integrations"][key]["enabled"])

    def test_migration_preserves_values_and_unknown_keys(self):
        raw = legacy_v0_config()
        raw["custom_section"] = {"keep": ["me"]}
        raw["paths"]["custom_tool"] = "C:\\Tools\\custom.exe"
        migrated, _ = cm.migrate_config(raw)
        self.assertEqual("11.1111", migrated["location"]["latitude"])
        self.assertEqual("192.0.2.26", cm.enabled_device_ips(migrated)[0])
        self.assertEqual({"keep": ["me"]}, migrated["custom_section"])
        self.assertEqual("C:\\Tools\\custom.exe", migrated["paths"]["custom_tool"])
        # Coordinates, integrations, paths and automation survive untouched.
        self.assertEqual(raw["automation"], migrated["automation"])
        self.assertEqual(raw["paths"]["openrgb"], migrated["paths"]["openrgb"])
        self.assertTrue(migrated["integrations"]["openrgb"]["enabled"])

    def test_migration_fills_missing_automation_keys(self):
        raw = legacy_v0_config()
        del raw["automation"]["force_silent_launch"]
        migrated, _ = cm.migrate_config(raw)
        self.assertIn("force_silent_launch", migrated["automation"])

    def test_newer_config_version_is_rejected(self):
        raw = cm.default_config()
        raw["config_version"] = cm.CONFIG_VERSION + 5
        with self.assertRaises(cm.ConfigError):
            cm.migrate_config(raw)

    def test_non_integer_config_version_is_rejected(self):
        raw = cm.default_config()
        raw["config_version"] = "one"
        self.assertIsNone(cm.detect_config_version(raw))
        with self.assertRaises(cm.ConfigError):
            cm.migrate_config(raw)

    def test_integration_enabled_prefers_explicit_flag(self):
        config = cm.default_config()
        config["paths"]["artemis"] = "C:\\Artemis.exe"
        config["integrations"]["artemis"]["enabled"] = False
        self.assertFalse(cm.integration_enabled(config, "artemis"))

    def test_integration_enabled_falls_back_to_path_inference(self):
        config = cm.default_config()
        del config["integrations"]
        config["paths"]["openrgb"] = "C:\\Program Files\\OpenRGB\\OpenRGB.exe"
        self.assertTrue(cm.integration_enabled(config, "openrgb"))
        self.assertFalse(cm.integration_enabled(config, "artemis"))


class TestValidation(TempDirTestCase):
    def config_with(self, **location):
        config = cm.default_config()
        config["location"].update(location)
        return config

    def test_invalid_latitude_rejected(self):
        for value in ("95", "-91", "abc", ""):
            result = cm.validate_config(self.config_with(latitude=value))
            self.assertTrue(result.errors, f"latitude {value!r} should be rejected")

    def test_invalid_longitude_rejected(self):
        for value in ("181", "-180.5", "not-a-number", ""):
            result = cm.validate_config(self.config_with(longitude=value))
            self.assertTrue(result.errors, f"longitude {value!r} should be rejected")

    def test_valid_latitude_longitude_accepted(self):
        result = cm.validate_config(self.config_with(latitude="11.1111", longitude="-22.2222"))
        self.assertEqual([], result.errors)

    def test_invalid_elevation_rejected(self):
        result = cm.validate_config(self.config_with(elevation="high"))
        self.assertTrue(result.errors)

    def test_invalid_light_buffer_rejected(self):
        for value in ("-1", "25", "two"):
            result = cm.validate_config(self.config_with(light_buffer_hours=value))
            self.assertTrue(result.errors, f"buffer {value!r} should be rejected")

    def test_invalid_device_address_rejected(self):
        for value in ("999.999.999.999", "192.168.0", "not-an-ip"):
            config = device_config(("192.168.1.50",))
            config["lights"]["devices"][0]["ip"] = value
            result = cm.validate_config(config)
            self.assertTrue(result.errors, f"address {value!r} should be rejected")

    def test_surrounding_whitespace_in_an_address_is_tolerated(self):
        config = device_config(("192.168.1.50",))
        config["lights"]["devices"][0]["ip"] = "  192.168.1.50  "
        self.assertEqual([], cm.validate_config(config).errors)

    def test_a_configured_device_needs_an_address(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                config = device_config(("192.168.1.50",))
                config["lights"]["devices"][0]["ip"] = value
                result = cm.validate_config(config)
                self.assertTrue(
                    any("address" in error.lower() for error in result.errors), result.errors
                )

    def test_enabled_integration_requires_a_path(self):
        config = cm.default_config()
        config["integrations"]["openrgb"]["enabled"] = True
        result = cm.validate_config(config)
        self.assertTrue(any("OpenRGB" in error for error in result.errors))

    def test_disabled_integration_does_not_require_a_path(self):
        config = cm.default_config()
        config["integrations"]["openrgb"]["enabled"] = False
        config["paths"]["openrgb"] = ""
        self.assertEqual([], cm.validate_config(config).errors)

    def test_enabled_integration_with_missing_executable_only_warns(self):
        config = cm.default_config()
        config["integrations"]["artemis"]["enabled"] = True
        config["paths"]["artemis"] = self.path("does-not-exist", "Artemis.UI.Windows.exe")
        result = cm.validate_config(config)
        self.assertEqual([], result.errors)
        self.assertTrue(any("Artemis" in warning for warning in result.warnings))

    def test_path_type_is_validated(self):
        config = cm.default_config()
        config["paths"]["openrgb"] = 42
        self.assertTrue(any("OpenRGB" in error for error in cm.validate_config(config).errors))

    def test_boolean_settings_are_validated(self):
        config = cm.default_config()
        config["automation"]["close_apps_on_sleep"] = "yes"
        self.assertTrue(cm.validate_config(config).errors)

    def test_timeout_range_is_validated(self):
        config = cm.default_config()
        config["automation"]["razer_synapse_timeout_seconds"] = 9999
        self.assertTrue(cm.validate_config(config).errors)

    def test_unknown_keys_are_allowed(self):
        config = cm.default_config()
        config["future_option"] = {"anything": True}
        self.assertEqual([], cm.validate_config(config).errors)

    def test_root_must_be_object(self):
        self.assertTrue(cm.validate_config(["not", "an", "object"]).errors)

    def test_validate_device_address_helper(self):
        self.assertIsNone(cm.validate_device_address("192.168.1.7"))
        self.assertIsNone(cm.validate_device_address(""))
        self.assertIsNone(cm.validate_device_address(None))
        self.assertIsNotNone(cm.validate_device_address("300.1.1.1"))


class TestSaveLoad(TempDirTestCase):
    def test_save_load_round_trip(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        config = cm.default_config()
        config["location"]["latitude"] = "51.5074"
        config["location"]["longitude"] = "-0.1278"
        config["lights"]["devices"] = [device("Desk Lamp", "192.168.1.10")]
        config["integrations"]["openrgb"]["enabled"] = True
        config["paths"]["openrgb"] = self.write_json(self.path("OpenRGB.exe"), {})

        manager.save(config)
        self.assertTrue(manager.exists())
        self.assertEqual(config, manager.load())
        self.assertEqual(cm.CONFIG_VERSION, manager.load()["config_version"])
        # A fresh manager (same process, new instance) reads the same config.
        self.assertEqual(config, cm.ConfigManager(manager.config_path).load())

    def test_save_creates_missing_data_directory(self):
        manager = cm.ConfigManager(self.path("nested", "deeper", cm.CONFIG_FILENAME))
        manager.save(cm.default_config())
        self.assertTrue(os.path.isfile(manager.config_path))

    def test_save_refuses_invalid_config(self):
        manager = cm.ConfigManager(self.path(cm.CONFIG_FILENAME))
        config = cm.default_config()
        config["location"]["latitude"] = "999"
        with self.assertRaises(cm.ConfigValidationError):
            manager.save(config)
        self.assertFalse(os.path.exists(manager.config_path))

    def test_load_missing_file_raises_friendly_error(self):
        manager = cm.ConfigManager(self.path(cm.CONFIG_FILENAME))
        with self.assertRaises(cm.ConfigError):
            manager.load()
        self.assertIsNone(manager.load_or_none())

    def test_load_invalid_json_raises_friendly_error(self):
        path = self.path(cm.CONFIG_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        manager = cm.ConfigManager(path)
        with self.assertRaises(cm.ConfigError):
            manager.load()

    def test_load_migrates_in_memory_and_reports_version(self):
        path = self.write_json(self.path(cm.CONFIG_FILENAME), legacy_v0_config())
        manager = cm.ConfigManager(path)
        config = manager.load()
        self.assertEqual(cm.CONFIG_VERSION, config["config_version"])
        self.assertEqual(0, manager.last_load_migrated_from)
        # Migration is not persisted until the caller saves explicitly.
        self.assertEqual(0, cm.detect_config_version(self.read_json(path)))
        self.assertTrue(manager.needs_migration())
        manager.save(config, create_backup=True)
        self.assertEqual(cm.CONFIG_VERSION, cm.detect_config_version(self.read_json(path)))
        self.assertTrue(os.path.isfile(path + cm.BACKUP_SUFFIX))
        self.assertEqual(0, cm.detect_config_version(self.read_json(path + cm.BACKUP_SUFFIX)))

    def test_up_to_date_config_reports_no_migration(self):
        manager = cm.ConfigManager(self.path(cm.CONFIG_FILENAME))
        manager.save(cm.default_config())
        manager.load()
        self.assertIsNone(manager.last_load_migrated_from)
        self.assertFalse(manager.needs_migration())


class TestSafeWrites(TempDirTestCase):
    def test_atomic_write_leaves_no_temporary_files(self):
        target = self.path(cm.CONFIG_FILENAME)
        cm.write_config_file(target, cm.default_config())
        leftovers = [name for name in os.listdir(self.tmpdir) if name.endswith(".tmp")]
        self.assertEqual([], leftovers)
        self.assertEqual(cm.default_config(), self.read_json(target))

    def test_failed_write_keeps_previous_config_intact(self):
        target = self.path(cm.CONFIG_FILENAME)
        original = cm.default_config()
        original["location"]["latitude"] = "12.3456"
        cm.write_config_file(target, original)
        before = self.read_raw(target)

        with self.assertRaises(TypeError):
            cm.write_config_file(target, {"unserializable": {1, 2, 3}})

        self.assertEqual(before, self.read_raw(target))
        self.assertEqual(
            [], [name for name in os.listdir(self.tmpdir) if name.endswith(".tmp")]
        )

    def test_backup_keeps_single_previous_version(self):
        target = self.path(cm.CONFIG_FILENAME)
        first = cm.default_config()
        first["location"]["latitude"] = "1.0"
        cm.write_config_file(target, first)

        second = cm.default_config()
        second["location"]["latitude"] = "2.0"
        cm.write_config_file(target, second, create_backup=True)

        self.assertEqual("1.0", self.read_json(target + cm.BACKUP_SUFFIX)["location"]["latitude"])
        self.assertEqual("2.0", self.read_json(target)["location"]["latitude"])

        third = cm.default_config()
        third["location"]["latitude"] = "3.0"
        cm.write_config_file(target, third, create_backup=True)
        backups = [name for name in os.listdir(self.tmpdir) if name.endswith(cm.BACKUP_SUFFIX)]
        self.assertEqual([cm.CONFIG_FILENAME + cm.BACKUP_SUFFIX], backups)
        self.assertEqual("2.0", self.read_json(target + cm.BACKUP_SUFFIX)["location"]["latitude"])


class TestImportExport(TempDirTestCase):
    def test_import_of_legacy_config(self):
        legacy_path = self.write_json(self.path("legacy.json"), legacy_v0_config())
        legacy_before = self.read_raw(legacy_path)

        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        imported = manager.import_config(legacy_path)

        self.assertEqual(cm.CONFIG_VERSION, imported["config_version"])
        self.assertTrue(imported["integrations"]["openrgb"]["enabled"])
        self.assertEqual(imported, manager.load())
        self.assertEqual(
            cm.CONFIG_VERSION, cm.detect_config_version(self.read_json(manager.config_path))
        )
        self.assertIsNone(manager.last_load_migrated_from)
        # The legacy file is never modified in place.
        self.assertEqual(legacy_before, self.read_raw(legacy_path))
        self.assertFalse(os.path.exists(manager.backup_path))

    def test_load_external_does_not_write_anything(self):
        legacy_path = self.write_json(self.path("legacy.json"), legacy_v0_config())
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))

        config, from_version = manager.load_external(legacy_path)
        self.assertEqual(cm.CONFIG_VERSION, config["config_version"])
        self.assertEqual(0, from_version)
        self.assertFalse(os.path.exists(manager.config_path))
        self.assertFalse(os.path.exists(manager.data_dir))

    def test_load_external_rejects_invalid_config(self):
        path = self.write_json(self.path("bad.json"), self.config_with_bad_latitude())
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        with self.assertRaises(cm.ConfigValidationError):
            manager.load_external(path)

    def test_import_backs_up_existing_config(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        current = cm.default_config()
        current["location"]["latitude"] = "10.0"
        manager.save(current)

        imported_path = self.write_json(
            self.path("import.json"),
            {
                "config_version": cm.CONFIG_VERSION,
                "location": {"latitude": "20.0", "longitude": "20.0", "elevation": 5, "light_buffer_hours": 1.5},
            },
        )
        manager.import_config(imported_path)

        self.assertEqual("10.0", self.read_json(manager.backup_path)["location"]["latitude"])
        self.assertEqual("20.0", manager.load()["location"]["latitude"])

    def test_invalid_import_does_not_replace_valid_current_config(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        manager.save(cm.default_config())
        before = self.read_raw(manager.config_path)

        broken = self.write_json(self.path("broken.json"), self.config_with_bad_latitude())
        with self.assertRaises(cm.ConfigValidationError):
            manager.import_config(broken)
        self.assertEqual(before, self.read_raw(manager.config_path))
        self.assertFalse(os.path.exists(manager.backup_path))

    def test_import_rejects_invalid_json(self):
        broken = self.path("notjson.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("nope{")
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        with self.assertRaises(cm.ConfigError):
            manager.import_config(broken)

    def test_import_rejects_invalid_enabled_integration(self):
        broken = self.write_json(
            self.path("enabled_without_path.json"),
            {"config_version": 1, "integrations": {"openrgb": {"enabled": True}}},
        )
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        with self.assertRaises(cm.ConfigValidationError):
            manager.import_config(broken)

    def test_export_round_trip(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        config = cm.default_config()
        config["location"].update({"latitude": "48.2082", "longitude": "16.3738"})
        manager.save(config)

        export_path = self.path("exported-config.json")
        manager.export_config(export_path)

        exported = self.read_json(export_path)
        self.assertEqual(cm.CONFIG_VERSION, exported["config_version"])
        self.assertEqual("48.2082", exported["location"]["latitude"])
        self.assertEqual([], cm.validate_config(exported).errors)

        # The exported file can be imported again.
        other = cm.ConfigManager(self.path("other", cm.CONFIG_FILENAME))
        other.import_config(export_path)
        self.assertEqual(exported, other.load())

    def test_export_refuses_to_overwrite_active_config(self):
        manager = cm.ConfigManager(self.path(cm.CONFIG_FILENAME))
        manager.save(cm.default_config())
        with self.assertRaises(cm.ConfigError):
            manager.export_config(manager.config_path)

    def config_with_bad_latitude(self):
        config = cm.default_config()
        config["location"]["latitude"] = "123"
        return config


class TestMalformedSections(TempDirTestCase):
    """A known section that is present but malformed must be rejected, not repaired."""

    MALFORMED = (
        ("location", "bad"),
        ("lights", []),
        ("paths", "bad"),
        ("automation", []),
        ("integrations", "bad"),
    )

    def write_malformed(self, section, value):
        raw = cm.default_config()
        raw[section] = value
        return self.write_json(self.path(f"malformed-{section}.json"), raw)

    def test_present_but_malformed_sections_are_rejected(self):
        for section, value in self.MALFORMED:
            with self.subTest(section=section):
                path = self.write_malformed(section, value)
                manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
                with self.assertRaises(cm.ConfigValidationError):
                    manager.load(path)

    def test_malformed_sections_survive_normalization_for_validation(self):
        for section, value in self.MALFORMED:
            with self.subTest(section=section):
                raw = cm.default_config()
                raw[section] = value
                self.assertEqual(value, cm.normalize_config(raw)[section])
                self.assertEqual(value, cm.migrate_config(raw)[0][section])

    def test_malformed_integration_entry_is_rejected(self):
        raw = cm.default_config()
        raw["integrations"]["openrgb"] = "yes"
        result = cm.validate_config(cm.migrate_config(raw)[0])
        self.assertTrue(any("OpenRGB" in error for error in result.errors))

    def test_missing_known_sections_are_still_normalized(self):
        raw = {"config_version": cm.CONFIG_VERSION}
        migrated, from_version = cm.migrate_config(raw)
        self.assertEqual(cm.CONFIG_VERSION, from_version)
        self.assertEqual(cm.default_config(), migrated)
        self.assertEqual([], cm.validate_config(migrated).errors)

    def test_legacy_config_missing_a_section_still_migrates(self):
        raw = legacy_v0_config()
        del raw["automation"]
        migrated, from_version = cm.migrate_config(raw)
        self.assertEqual(0, from_version)
        self.assertEqual(cm.default_config()["automation"], migrated["automation"])
        self.assertEqual("11.1111", migrated["location"]["latitude"])
        self.assertEqual([], cm.validate_config(migrated).errors)

    def test_malformed_import_never_replaces_the_active_config(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        manager.save(cm.default_config())
        before = self.read_raw(manager.config_path)

        for section, value in self.MALFORMED:
            with self.subTest(section=section):
                broken = self.write_malformed(section, value)
                with self.assertRaises(cm.ConfigValidationError):
                    manager.import_config(broken)
                self.assertEqual(before, self.read_raw(manager.config_path))
                self.assertFalse(os.path.exists(manager.backup_path))

        self.assertEqual(cm.default_config(), manager.load())


class TestExecutablePaths(TempDirTestCase):
    """An enabled integration's path must point at a file, never a directory."""

    def executable_config(self, path):
        config = cm.default_config()
        config["paths"]["openrgb"] = path
        config["integrations"]["openrgb"]["enabled"] = True
        return config

    def test_existing_file_is_accepted(self):
        config = self.executable_config(self.write_json(self.path("OpenRGB.exe"), {}))
        config["location"].update({"latitude": "11.1111", "longitude": "-22.2222"})
        result = cm.validate_config(config)
        self.assertEqual([], result.errors)
        self.assertEqual([], result.warnings)

    def test_existing_directory_is_not_a_valid_executable(self):
        os.makedirs(self.path("OpenRGB"))
        result = cm.validate_config(self.executable_config(self.path("OpenRGB")))
        self.assertEqual([], result.errors)
        self.assertTrue(any("OpenRGB" in warning for warning in result.warnings))

    def test_missing_executable_only_warns(self):
        result = cm.validate_config(self.executable_config(self.path("nowhere", "OpenRGB.exe")))
        self.assertEqual([], result.errors)
        self.assertTrue(any("OpenRGB" in warning for warning in result.warnings))

    def test_a_directory_path_still_saves_but_warns(self):
        manager = cm.ConfigManager(self.path("data", cm.CONFIG_FILENAME))
        os.makedirs(self.path("OpenRGB"))
        manager.save(self.executable_config(self.path("OpenRGB")))
        self.assertEqual(self.path("OpenRGB"), manager.load()["paths"]["openrgb"])


class TestRuntimeLoad(TempDirTestCase):
    """The timing-critical/read-only loader must be cheap and must never validate."""

    def executable_config(self):
        config = cm.default_config()
        config["paths"]["openrgb"] = self.path("Program Files", "OpenRGB", "OpenRGB.exe")
        config["integrations"]["openrgb"]["enabled"] = True
        config["paths"]["artemis"] = self.path("Program Files", "Artemis", "Artemis.UI.Windows.exe")
        config["integrations"]["artemis"]["enabled"] = True
        return config

    def test_runtime_load_never_touches_the_filesystem_for_executables(self):
        path = self.write_json(self.path(cm.CONFIG_FILENAME), self.executable_config())
        manager = cm.ConfigManager(path)

        def explode(*args, **kwargs):
            raise AssertionError("the runtime loader probed the filesystem")

        with mock.patch.object(cm.os.path, "isfile", side_effect=explode), mock.patch.object(
            cm.os.path, "exists", side_effect=explode
        ):
            config = manager.load_runtime()

        self.assertEqual(self.path("Program Files", "OpenRGB", "OpenRGB.exe"), config["paths"]["openrgb"])
        self.assertTrue(config["integrations"]["openrgb"]["enabled"])
        self.assertTrue(config["integrations"]["artemis"]["enabled"])
        # The runtime read is side-effect free and does not report a migration.
        self.assertIsNone(manager.last_load_migrated_from)

    def test_strict_load_still_probes_executable_paths(self):
        executable = self.write_json(self.path("OpenRGB.exe"), {})
        config = cm.default_config()
        config["paths"]["openrgb"] = executable
        config["integrations"]["openrgb"]["enabled"] = True
        path = self.write_json(self.path(cm.CONFIG_FILENAME), config)
        manager = cm.ConfigManager(path)

        probed = []
        real_isfile = os.path.isfile

        def recording(candidate, *args, **kwargs):
            probed.append(os.path.abspath(candidate))
            return real_isfile(candidate, *args, **kwargs)

        with mock.patch.object(cm.os.path, "isfile", side_effect=recording):
            loaded = manager.load()

        self.assertEqual(executable, loaded["paths"]["openrgb"])
        self.assertIn(os.path.abspath(executable), probed)

    def test_runtime_load_repairs_malformed_sections_for_runtime_readers(self):
        raw = cm.default_config()
        raw["lights"] = "garbage"
        raw["automation"] = ["not", "an", "object"]
        raw["integrations"] = "garbage"
        raw["paths"] = 7
        raw["location"] = None
        path = self.write_json(self.path(cm.CONFIG_FILENAME), raw)

        config = cm.ConfigManager(path).load_runtime()

        self.assertEqual(cm.default_config()["location"], config["location"])
        self.assertEqual(cm.default_config()["paths"], config["paths"])
        self.assertEqual([], config["lights"]["devices"])
        self.assertEqual([], cm.enabled_device_ips(config))
        self.assertTrue(config["automation"]["close_apps_on_sleep"])
        for key in cm.INTEGRATION_KEYS:
            self.assertIsInstance(config["integrations"][key], dict)
            self.assertIsInstance(config["integrations"][key]["enabled"], bool)

    def test_runtime_load_reports_friendly_errors(self):
        manager = cm.ConfigManager(self.path(cm.CONFIG_FILENAME))
        with self.assertRaises(cm.ConfigError):
            manager.load_runtime()

        with open(manager.config_path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        with self.assertRaises(cm.ConfigError):
            manager.load_runtime()

    def test_runtime_load_migrates_a_legacy_config(self):
        path = self.write_json(self.path(cm.CONFIG_FILENAME), legacy_v0_config())
        config = cm.ConfigManager(path).load_runtime()
        self.assertEqual(cm.CONFIG_VERSION, config["config_version"])
        self.assertTrue(config["integrations"]["openrgb"]["enabled"])
        self.assertEqual(["192.0.2.26", "192.0.2.27"], cm.enabled_device_ips(config))


class TestStorageLocations(TempDirTestCase):
    def test_source_run_uses_application_directory(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(self.tmpdir, cm.get_data_dir(self.tmpdir))
            self.assertEqual("source", cm.storage_mode(self.tmpdir))

    def test_portable_flag_uses_application_directory(self):
        open(self.path(cm.PORTABLE_FLAG_FILENAME), "w").close()
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", self.path("YeelightPCCompanion.exe")
        ):
            self.assertTrue(cm.is_portable_mode(self.tmpdir))
            self.assertEqual(self.tmpdir, cm.get_data_dir(self.tmpdir))
            self.assertEqual("portable", cm.storage_mode(self.tmpdir))

    def test_frozen_install_uses_local_app_data(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", self.path("YeelightPCCompanion.exe")
        ), mock.patch.dict(os.environ, {"LOCALAPPDATA": self.path("LocalAppData")}):
            data_dir = cm.get_data_dir(self.tmpdir)
            self.assertEqual(
                os.path.join(self.path("LocalAppData"), cm.APP_DATA_DIR_NAME), data_dir
            )
            self.assertNotEqual(self.tmpdir, data_dir)
            self.assertEqual("localappdata", cm.storage_mode(self.tmpdir))

    def test_legacy_discovery_in_development_layout(self):
        repo = self.path("repo")
        dist = os.path.join(repo, "dist", "YeelightPCCompanion")
        os.makedirs(dist)
        for marker in ("yeelight_pc_companion.py", cm.CONFIG_EXAMPLE_FILENAME):
            open(os.path.join(repo, marker), "w").close()
        legacy = self.write_json(os.path.join(repo, cm.CONFIG_FILENAME), legacy_v0_config())

        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", os.path.join(dist, "YeelightPCCompanion.exe")
        ), mock.patch.dict(os.environ, {"LOCALAPPDATA": self.path("LocalAppData")}):
            manager = cm.ConfigManager()
            self.assertEqual(legacy, manager.find_legacy_config())

            imported = manager.import_config(manager.find_legacy_config())
            self.assertEqual(cm.CONFIG_VERSION, imported["config_version"])
            self.assertEqual(
                os.path.join(self.path("LocalAppData"), cm.APP_DATA_DIR_NAME, cm.CONFIG_FILENAME),
                manager.config_path,
            )
            # The import lands in the (sandboxed) user data directory and the
            # development checkout's config file is untouched.
            self.assertTrue(os.path.isfile(manager.config_path))
            self.assertNotEqual(legacy, manager.config_path)

    def test_unsafe_parent_layout_is_not_treated_as_legacy(self):
        dist = self.path("ProgramFiles", "YeelightPCCompanion")
        os.makedirs(dist)
        self.write_json(self.path("ProgramFiles", cm.CONFIG_FILENAME), legacy_v0_config())
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", os.path.join(dist, "YeelightPCCompanion.exe")
        ), mock.patch.dict(os.environ, {"LOCALAPPDATA": self.path("LocalAppData")}):
            manager = cm.ConfigManager()
            self.assertIsNone(manager.find_legacy_config())


if __name__ == "__main__":
    unittest.main(verbosity=2)
