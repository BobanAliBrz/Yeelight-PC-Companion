"""Configuration management for Yeelight PC Companion.

This module is the single source of truth for

* the canonical default configuration and configuration schema version,
* where the configuration (and other runtime state) lives on disk,
* loading, validation, version migration, atomic saving and backups,
* importing and exporting user configurations.

The Yeelight device model itself (stable device ids, the configured device list
and LAN discovery) lives in :mod:`yeelight_devices`; this module owns the schema
it is persisted in, including the ``v1 -> v2`` device migration and the
validation of every device entry.

Two read paths exist on purpose:

* :meth:`ConfigManager.load` — the strict path (structure, version, ranges,
  device addresses and integration executable paths). Used by the wizard,
  Settings, import and normal startup.
* :meth:`ConfigManager.load_runtime` — the lightweight path for
  timing-critical/read-only consumers. It never validates and therefore never
  probes integration executables on disk, never writes and never backs up.

It intentionally depends only on the Python standard library so that the
configuration logic stays testable without a GUI or a Windows runtime.

The JSON file remains the internal persistence format, but it is treated as an
implementation detail: users configure the application through the first-run
wizard and the Settings UI.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

from yeelight_devices import (
    DEFAULT_DEVICE_NAME,
    device_summary,
    enabled_devices,
    find_duplicate_ids,
    find_duplicate_ips,
    new_device,
    normalized_ip,
    validate_device_address,
    validate_device_id,
    validate_device_name,
)

# ---------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------
# v0 = any configuration written before versioning existed (no "config_version"
#      key, no "integrations" section).
# v1 = adds "config_version" and per-integration enable flags; still exactly two
#      fixed Yeelight address slots (`lights.bulb_ip` / `lights.lightstrip_ip`).
# v2 = current schema: the two fixed slots become an arbitrary-length list of
#      Yeelight devices (`lights.devices`), each with a stable id, a friendly
#      name, an address and an enable flag.
CONFIG_VERSION = 2
LEGACY_CONFIG_VERSION = 0

# Friendly names for the two devices a v1 configuration could describe.
LEGACY_DEVICE_NAMES = {"bulb_ip": "Yeelight Bulb", "lightstrip_ip": "Yeelight Lightstrip"}
LEGACY_DEVICE_KEYS = ("bulb_ip", "lightstrip_ip")

# ---------------------------------------------------------
# File / directory names
# ---------------------------------------------------------
CONFIG_FILENAME = "config.json"
CONFIG_EXAMPLE_FILENAME = "config.example.json"
BACKUP_SUFFIX = ".bak"
PORTABLE_FLAG_FILENAME = "portable.flag"
DEBUG_LOG_FILENAME = "yeelight_pc_companion_debug.log"
CRASH_LOG_FILENAME = "crash.log"
APP_DATA_DIR_NAME = "Yeelight PC Companion"

# ---------------------------------------------------------
# Integrations
# ---------------------------------------------------------
# One canonical definition keyed by the existing, compatibility-preserved
# `paths` / `integrations` keys. `process` is the executable image name used for
# the native process listing/killing helpers.
INTEGRATIONS = {
    "openrgb": {
        "label": "OpenRGB",
        "path_key": "openrgb",
        "process": "OpenRGB.exe",
        "hint": "OpenRGB.exe",
    },
    "yeelight_connector": {
        "label": "Yeelight Chroma Connector",
        "path_key": "yeelight_connector",
        "process": "Yeelight Chroma Connector.exe",
        "hint": "Yeelight Chroma Connector.exe",
    },
    "razer_synapse": {
        "label": "Razer Synapse",
        "path_key": "razer_synapse",
        "process": "Razer Synapse 3.exe",
        "hint": "Razer Synapse 3.exe",
    },
    "artemis": {
        "label": "Artemis",
        "path_key": "artemis",
        "process": "Artemis.UI.Windows.exe",
        "hint": "Artemis.UI.Windows.exe",
    },
}
INTEGRATION_KEYS = tuple(INTEGRATIONS)

# Keys kept for backwards compatibility. They are preserved, validated as
# booleans and surfaced in the UI, but the runtime does not currently consume
# them (see project_memory.md "Known configuration mismatches").
LEGACY_UNCONSUMED_AUTOMATION_KEYS = ("turn_on_yeelight_on_wake_night", "force_silent_launch")

AUTOMATION_BOOLEAN_KEYS = (
    "close_apps_on_sleep",
    "turn_off_yeelight_on_sleep",
    "restore_apps_on_wake",
    "turn_on_yeelight_on_wake_night",
    "force_silent_launch",
    "launch_razer_synapse",
    "wait_for_razer_synapse",
)

# ---------------------------------------------------------
# Canonical default configuration
# ---------------------------------------------------------
# Safe fresh configuration: blank optional paths, no real coordinates, no real
# LAN addresses, no machine-specific values. Keep this synchronized with
# `config.example.json`.
DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "location": {
        "latitude": "0.0000",
        "longitude": "0.0000",
        "elevation": 0.0,
        "light_buffer_hours": 2.0,
    },
    "lights": {
        "devices": [],
    },
    "paths": {
        "openrgb": "",
        "yeelight_connector": "",
        "razer_synapse": "",
        "artemis": "",
    },
    "integrations": {
        "openrgb": {"enabled": False},
        "yeelight_connector": {"enabled": False},
        "razer_synapse": {"enabled": False},
        "artemis": {"enabled": False},
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


class ConfigError(Exception):
    """Raised for configuration problems that have a user-friendly message."""


class ConfigValidationError(ConfigError):
    """Raised when a configuration is structurally loaded but invalid."""

    def __init__(self, errors, warnings=None):
        self.errors = list(errors or [])
        self.warnings = list(warnings or [])
        message = "Configuration is not valid."
        if self.errors:
            message = "Configuration is not valid: " + " ".join(self.errors)
        super().__init__(message)


class ValidationResult:
    """Errors block saving/importing; warnings are informative only."""

    __slots__ = ("errors", "warnings")

    def __init__(self, errors=None, warnings=None):
        self.errors = list(errors or [])
        self.warnings = list(warnings or [])

    @property
    def ok(self):
        return not self.errors

    def __bool__(self):  # convenient truthiness: a valid result is truthy
        return self.ok

    def __repr__(self):
        return f"ValidationResult(errors={self.errors!r}, warnings={self.warnings!r})"


# ---------------------------------------------------------
# Storage locations
# ---------------------------------------------------------
def is_frozen():
    """True when running from a packaged (PyInstaller) build."""
    return bool(getattr(sys, "frozen", False))


def get_app_dir():
    """Directory the application is installed/run from.

    For a packaged build this is the executable's directory; for a source
    checkout it is the repository root.
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _query_local_appdata():
    """Ask Windows for the LocalAppData folder (never hardcode a username)."""
    try:
        import ctypes

        CSIDL_LOCAL_APPDATA = 0x001C
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_LOCAL_APPDATA, None, 0, buffer) == 0:
            return buffer.value or None
    except Exception:
        return None
    return None


def get_local_app_data_dir():
    """`%LOCALAPPDATA%\\Yeelight PC Companion`."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = _query_local_appdata()
    if not base:
        base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP_DATA_DIR_NAME)


def is_portable_mode(app_dir=None):
    """Portable mode: an (empty) `portable.flag` sits beside the executable."""
    app_dir = os.path.abspath(app_dir or get_app_dir())
    return os.path.isfile(os.path.join(app_dir, PORTABLE_FLAG_FILENAME))


def get_data_dir(app_dir=None):
    """Directory holding config.json, the debug log, crash.log and backups.

    * source/development run  -> the repository directory (developer friendly)
    * packaged + portable.flag -> the application directory
    * packaged normal install  -> ``%LOCALAPPDATA%\\Yeelight PC Companion``

    Never writes under Program Files for a normal install.
    """
    app_dir = os.path.abspath(app_dir or get_app_dir())
    if is_portable_mode(app_dir) or not is_frozen():
        return app_dir
    return get_local_app_data_dir()


def get_config_path(app_dir=None):
    return os.path.join(get_data_dir(app_dir), CONFIG_FILENAME)


def get_log_path(filename=DEBUG_LOG_FILENAME, app_dir=None):
    return os.path.join(get_data_dir(app_dir), filename)


def ensure_data_dir(path):
    """Create the data directory if needed. Returns the directory path."""
    os.makedirs(path, exist_ok=True)
    return path


def storage_mode(app_dir=None):
    """Human-readable storage mode for logging: 'source', 'portable' or 'localappdata'."""
    app_dir = os.path.abspath(app_dir or get_app_dir())
    if is_portable_mode(app_dir):
        return "portable"
    if not is_frozen():
        return "source"
    return "localappdata"


# ---------------------------------------------------------
# Start-at-logon registration (per-user, unelevated)
# ---------------------------------------------------------
# The application runs as an ordinary unelevated user process; only the OpenRGB
# integration needs administrator rights, and it gets them through its own
# one-time approved task (`windows_tasks.py`). Startup therefore belongs in the
# per-user `Run` key, which needs no UAC and no scheduled task:
#
#   HKCU\Software\Microsoft\Windows\CurrentVersion\Run
#
# The retired design registered a highest-privilege *scheduled task* named
# `YeelightPCCompanion` (see `STARTUP_LEGACY_TASK_NAMES`), which made every
# logon start an elevated tray process for no functional gain.
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: Name of the `Run` value this application owns. Uninstall removes exactly this
#: value and nothing else in the key.
STARTUP_VALUE_NAME = "YeelightPCCompanion"

#: Scheduled tasks created by the retired elevated-startup design. They are
#: removed when startup is (re)configured so that an upgraded machine does not
#: end up launching the application twice at logon.
STARTUP_LEGACY_TASK_NAMES = ("YeelightPCCompanion", "LuminaLightOrchestrator")

#: Flag passed at logon so the application starts in the tray.
STARTUP_ARGUMENTS = ("--tray",)


def _winreg():
    """The stdlib registry module, or None when it is unavailable."""
    try:
        import winreg  # noqa: F401  (Windows only)
    except Exception:
        return None
    return winreg


def _startup_command(target):
    """The exact command line stored in the `Run` value."""
    return " ".join([subprocess.list2cmdline([target])] + list(STARTUP_ARGUMENTS))


def startup_entry():
    """Return the current `Run` command for this app, or None when unset.

    Returns None on a non-Windows host and when the value does not exist. Read
    errors are reported as None rather than raised: this is a status query.
    """
    winreg = _winreg()
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
            return value
    except OSError:
        return None


def is_startup_enabled():
    """Whether this application currently has a per-user logon entry."""
    return startup_entry() is not None


def enable_startup(target=None):
    """Create/update this application's per-user logon entry.

    Only :data:`STARTUP_VALUE_NAME` is written; every other value in the `Run`
    key belongs to another program and is left untouched.

    Args:
        target: executable to start. Defaults to the running executable.

    Returns:
        The command line that was written.

    Raises:
        RuntimeError: when the registry is unavailable or the write failed.
    """
    winreg = _winreg()
    if winreg is None:
        raise RuntimeError("Per-user startup registration is only available on Windows.")

    if target is None:
        target = sys.executable if is_frozen() else os.path.abspath(sys.argv[0])
    target = os.path.abspath(target)

    command = _startup_command(target)
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as exc:
        raise RuntimeError(f"The start-at-logon entry could not be written: {exc}") from exc
    return command


def disable_startup():
    """Remove this application's per-user logon entry.

    Returns True when an entry was removed and False when there was none. Other
    programs' values in the same key are never touched.
    """
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, STARTUP_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def remove_legacy_startup_tasks(run_schtasks=None):
    """Delete the scheduled tasks created by the retired elevated-startup design.

    Migration only, and deliberately **unelevated best-effort**: the retired
    tasks were created by an elevated installer and carry a security descriptor
    that can deny an ordinary user the right to delete them, so this returns only
    the names it actually removed. It is the cheap first step of the real
    migration, which the Windows installer performs at install/upgrade time
    (``installer/YeelightPCCompanion.iss`` -> ``MigrateLegacyStartupTasks()``):
    unelevated deletion first, a fresh ``schtasks /query`` to verify, and only
    then, if a fixed legacy task verifiably survives, one narrowly scoped
    elevation. The application itself never elevates for this.

    The current OpenRGB elevation task is a *different* task and is deliberately
    not in scope here. This never raises: a machine where the tasks are already
    gone, or where Task Scheduler is unavailable, is not an error.

    Returns:
        The list of legacy task names that were actually removed.
    """
    if os.name != "nt":
        return []

    if run_schtasks is None:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        schtasks = os.path.join(system_root, "System32", "schtasks.exe")

        def run_schtasks(args):  # pragma: no cover - exercised through injection
            try:
                return subprocess.run(
                    [schtasks] + list(args),
                    capture_output=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).returncode
            except Exception:
                return 1

    removed = []
    for name in STARTUP_LEGACY_TASK_NAMES:
        if run_schtasks(["/query", "/tn", name]) != 0:
            continue
        if run_schtasks(["/delete", "/tn", name, "/f"]) == 0:
            removed.append(name)
    return removed


# ---------------------------------------------------------
# Defaults / normalization
# ---------------------------------------------------------
def default_config():
    """A fresh copy of the canonical default configuration."""
    return copy.deepcopy(DEFAULT_CONFIG)


def _infer_enabled_from_paths(config, key):
    """Legacy inference: a configured executable path means the integration was on."""
    paths = config.get("paths")
    if not isinstance(paths, dict):
        return False
    value = paths.get(key)
    return isinstance(value, str) and bool(value.strip())


KNOWN_SECTIONS = ("location", "lights", "paths", "automation")


def normalize_config(config):
    """Fill in *missing* known keys without discarding unknown ones.

    Unknown/extra keys (including whole unknown sections) are preserved so a
    migration never silently destroys user data.

    Only missing keys/sections are defaulted. A known section that is *present
    but malformed* (for example ``"location": "garbage"``) is deliberately left
    exactly as it is, so centralized validation can reject it. Silently
    replacing malformed user data with valid defaults would break the guarantee
    that an invalid imported configuration never replaces the active one.
    """
    if not isinstance(config, dict):
        return config

    normalized = copy.deepcopy(config)

    for section in KNOWN_SECTIONS:
        defaults = DEFAULT_CONFIG[section]
        if section not in normalized:
            normalized[section] = copy.deepcopy(defaults)
            continue
        current = normalized[section]
        if isinstance(current, dict):
            for key, value in defaults.items():
                current.setdefault(key, copy.deepcopy(value))
        # Else: present but not a JSON object -> preserved for validation.

    if "integrations" not in normalized:
        normalized["integrations"] = {}
    integrations = normalized["integrations"]
    if isinstance(integrations, dict):
        for key in INTEGRATION_KEYS:
            if key not in integrations:
                integrations[key] = {"enabled": _infer_enabled_from_paths(normalized, key)}
                continue
            entry = integrations[key]
            if isinstance(entry, dict):
                entry.setdefault("enabled", _infer_enabled_from_paths(normalized, key))
            # Else: present but not an object -> preserved for validation.

    normalized.setdefault("config_version", CONFIG_VERSION)
    return normalized


def runtime_config(config):
    """Coerce a configuration into something runtime code can always index.

    Full validation (`validate_config()`) is what *rejects* malformed sections.
    This helper exists only for consumers that must not fail mid-flight — the
    timing-critical suspend callback reads a boolean and a list of device
    addresses out of a configuration it must never raise on.

    It therefore replaces missing *or* malformed known sections with their safe
    defaults, makes the integration entries and the device list readable, and
    keeps every unknown key. It performs no filesystem access, no network
    access, no validation and no writes.
    """
    if not isinstance(config, dict):
        return default_config()

    repaired = copy.deepcopy(config)

    for section in KNOWN_SECTIONS + ("integrations",):
        if not isinstance(repaired.get(section), dict):
            repaired[section] = copy.deepcopy(DEFAULT_CONFIG[section])

    integrations = repaired["integrations"]
    for key in INTEGRATION_KEYS:
        if not isinstance(integrations.get(key), dict):
            integrations[key] = {"enabled": _infer_enabled_from_paths(repaired, key)}

    lights = repaired["lights"]
    if not isinstance(lights.get("devices"), list):
        lights["devices"] = []

    repaired.setdefault("config_version", CONFIG_VERSION)
    return repaired


def detect_config_version(config):
    """Return the declared schema version, or None if it is not an integer.

    A configuration without `config_version` is legacy version 0.
    """
    if not isinstance(config, dict):
        return None
    value = config.get("config_version", LEGACY_CONFIG_VERSION)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _legacy_lights_section(config):
    """The legacy `lights` section of a v1 configuration, or None."""
    lights = config.get("lights")
    if not isinstance(lights, dict):
        return None
    return lights


def _migrate_lights_to_devices(config):
    """``v1 -> v2``: turn the two fixed address slots into a device list.

    Both configured addresses are preserved as devices, blank slots produce
    nothing, and two slots that somehow hold the same address become **one**
    device (a duplicate address can only ever describe one physical device).

    The legacy keys are removed: after this step the device list is the only
    place a Yeelight address lives, so there is no second runtime model to keep
    in step. Existing identity information (a stable Yeelight id) does not exist
    in v1, so each migrated device gets a durable locally assigned id — never
    its display name.
    """
    lights = _legacy_lights_section(config)
    devices = []
    seen_ips = set()

    if lights is not None:
        expected = lights.get("devices")
        for key in LEGACY_DEVICE_KEYS:
            address = lights.get(key)
            if not isinstance(address, str):
                continue
            address = address.strip()
            if not address:
                continue
            normalized = normalized_ip(address)
            if normalized is not None:
                if normalized in seen_ips:
                    continue
                seen_ips.add(normalized)
            devices.append(
                new_device(LEGACY_DEVICE_NAMES.get(key, DEFAULT_DEVICE_NAME), address, enabled=True)
            )
        for key in LEGACY_DEVICE_KEYS:
            lights.pop(key, None)

        if isinstance(expected, list) and expected:
            # A file that already carries a device list (unusual for v1) keeps
            # it: the legacy addresses are appended instead of being dropped.
            known = {
                normalized_ip(entry.get("ip"))
                for entry in expected
                if isinstance(entry, dict)
            }
            expected.extend(
                device for device in devices if normalized_ip(device["ip"]) not in known
            )
        else:
            lights["devices"] = devices
    else:
        # A malformed `lights` section is preserved for validation instead of
        # being repaired; there is nothing sensible to migrate.
        config.setdefault("lights", {"devices": []})

    return config


def migrate_config(config):
    """Upgrade a raw configuration to CONFIG_VERSION.

    Returns ``(migrated_config, from_version)``.
    """
    if not isinstance(config, dict):
        raise ConfigError("The configuration must be a JSON object.")

    from_version = detect_config_version(config)
    if from_version is None:
        raise ConfigError("'config_version' must be an integer number.")

    if from_version > CONFIG_VERSION:
        raise ConfigError(
            f"This configuration was created by a newer version of Yeelight PC Companion "
            f"(config_version {from_version}; this build supports up to {CONFIG_VERSION})."
        )

    migrated = copy.deepcopy(config)

    # --- v0 -> v1 -------------------------------------------------------
    # v0 is "any configuration written before versioning existed": no
    # config_version key and no integrations section. The upgrade adds both.
    # Enablement is inferred from the already configured executable paths
    # (non-empty path -> enabled, blank/absent -> disabled) so an existing,
    # working setup keeps behaving exactly as it did before.
    if from_version < 1:
        migrated["config_version"] = 1

    # --- v1 -> v2 -------------------------------------------------------
    # The two fixed Yeelight address slots (`bulb_ip`, `lightstrip_ip`) become
    # the arbitrary-length `lights.devices` list. Both configured addresses are
    # preserved, a repeated address produces one device, and an existing setup
    # never has to be redone.
    if from_version < 2:
        migrated = _migrate_lights_to_devices(migrated)

    migrated = normalize_config(migrated)
    migrated["config_version"] = CONFIG_VERSION
    return migrated, from_version


def integration_enabled(config, key, default=False):
    """Whether an integration should be acted upon.

    Prefers the explicit ``integrations.<key>.enabled`` flag and falls back to
    the legacy "a path is configured" rule so a hand-written or partially
    migrated configuration still behaves sensibly.
    """
    if not isinstance(config, dict):
        return default
    integrations = config.get("integrations")
    if isinstance(integrations, dict):
        entry = integrations.get(key)
        if isinstance(entry, dict):
            enabled = entry.get("enabled")
            if isinstance(enabled, bool):
                return enabled
    return _infer_enabled_from_paths(config, key)


def integration_path(config, key):
    """Configured executable path for an integration (stripped, '' when unset)."""
    meta = INTEGRATIONS.get(key)
    if not meta or not isinstance(config, dict):
        return ""
    paths = config.get("paths")
    if not isinstance(paths, dict):
        return ""
    value = paths.get(meta["path_key"])
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------
# Yeelight device projection (the runtime view of `lights.devices`)
# ---------------------------------------------------------
def configured_devices(config):
    """Every configured device entry, in configuration order.

    Reads defensively so a timing-critical consumer can never raise: a malformed
    `lights` section or a non-object entry simply produces an empty list. Use
    `validate_config()` for the strict answer about the same data.
    """
    if not isinstance(config, dict):
        return []
    lights = config.get("lights")
    if not isinstance(lights, dict):
        return []
    return [entry for entry in lights.get("devices") or [] if isinstance(entry, dict)]


def enabled_configured_devices(config):
    """The configured devices that are explicitly enabled and have an address."""
    return enabled_devices(configured_devices(config))


def enabled_device_ips(config):
    """Addresses of every enabled Yeelight device, in configuration order.

    This is the **lightweight runtime projection** used by the timing-critical
    suspend callback, and by the wake/day reconciliation: it only walks the
    already-loaded configuration, so it performs no filesystem or network
    access, no validation, no discovery and no writes. It never raises.
    """
    addresses = []
    seen = set()
    for entry in enabled_configured_devices(config):
        address = entry.get("ip")
        if not isinstance(address, str):
            continue
        address = address.strip()
        if not address or address in seen:
            continue
        seen.add(address)
        addresses.append(address)
    return addresses


def device_count_summary(config):
    """``"3 enabled / 4 configured"`` for the configured device list."""
    return device_summary(configured_devices(config))


# ---------------------------------------------------------
# Validation
# ---------------------------------------------------------
# `validate_device_address` (imported from yeelight_devices) returns an error
# string for an invalid address and None for a valid one. A *configured device*
# always needs an address; the optional form is kept for callers that still ask
# whether a value could be an address at all.


def _device_label(entry, position):
    """User-facing label for one device entry in validation messages."""
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            return f"Yeelight device '{name.strip()}'"
    return f"Yeelight device #{position}"


def validate_device_entry(entry, position=1):
    """Validate one configured device entry. Returns a list of error strings.

    Every device must be a JSON object with a valid stable id, a non-empty
    friendly name, a valid address and a boolean `enabled` flag. Optional
    discovery metadata (`model`, `firmware`) is only checked when present.
    """
    if not isinstance(entry, dict):
        return [
            f"Yeelight device #{position} must be a JSON object with "
            "'id', 'name', 'ip' and 'enabled'."
        ]

    label = _device_label(entry, position)
    errors = []

    error = validate_device_id(entry.get("id"))
    if error:
        errors.append(f"{label}: {error}")

    error = validate_device_name(entry.get("name"))
    if error:
        errors.append(f"{label}: {error}")

    error = validate_device_address(entry.get("ip"), "address", required=True)
    if error:
        errors.append(f"{label}: {error}")

    if not isinstance(entry.get("enabled"), bool):
        errors.append(f"{label}: the 'enabled' setting must be true or false.")

    for field in ("model", "firmware"):
        if field in entry and not isinstance(entry[field], str):
            errors.append(f"{label}: '{field}' must be text.")

    return errors


def _validate_lights(config, result):
    lights = config.get("lights")
    if not isinstance(lights, dict):
        result.errors.append("The 'lights' section must be a JSON object.")
        return

    devices = lights.get("devices")
    if not isinstance(devices, list):
        result.errors.append(
            "The 'lights' section must contain a 'devices' list of Yeelight devices."
        )
        return

    for position, entry in enumerate(devices, start=1):
        result.errors.extend(validate_device_entry(entry, position))

    # A device id is the identity: two entries claiming it means a device was
    # added twice. An address describes one physical device, so it may only
    # appear once as well.
    for device_id, indexes in sorted(find_duplicate_ids(devices).items()):
        names = ", ".join(f"#{index + 1}" for index in indexes)
        result.errors.append(
            f"The device id '{device_id}' is used by more than one entry ({names}). "
            "Each Yeelight device must be listed once."
        )

    for address, indexes in sorted(find_duplicate_ips(devices).items()):
        names = ", ".join(f"#{index + 1}" for index in indexes)
        result.errors.append(
            f"The address {address} is used by more than one Yeelight device ({names}). "
            "Each device needs its own address."
        )

    if devices and not enabled_device_ips(config):
        result.warnings.append(
            "No Yeelight device is enabled, so the PC's sleep/wake automation will not "
            "switch any light until at least one device is enabled."
        )


def _as_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _validate_number(
    result,
    container,
    key,
    label,
    minimum=None,
    maximum=None,
    required=True,
    unit="",
):
    value = container.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            result.errors.append(f"{label} is required.")
        return None
    number = _as_float(value)
    if number is None:
        result.errors.append(f"{label} must be a number{unit}.")
        return None
    if minimum is not None and (number < minimum or number > maximum):
        result.errors.append(f"{label} must be between {minimum} and {maximum}{unit}.")
        return None
    return number


def _validate_version(config, result):
    version = detect_config_version(config)
    if version is None:
        result.errors.append("'config_version' must be an integer number.")
    elif version < 0:
        result.errors.append("'config_version' cannot be negative.")
    elif version > CONFIG_VERSION:
        result.errors.append(
            f"This configuration was created by a newer version of Yeelight PC Companion "
            f"(config_version {version}; this build supports up to {CONFIG_VERSION})."
        )
    elif version < CONFIG_VERSION:
        result.warnings.append(
            f"Configuration version {version} will be upgraded to version {CONFIG_VERSION} when it is saved."
        )


def _validate_location(config, result):
    location = config.get("location")
    if not isinstance(location, dict):
        result.errors.append("The 'location' section must be a JSON object.")
        return

    latitude = _validate_number(result, location, "latitude", "Latitude", -90, 90)
    longitude = _validate_number(result, location, "longitude", "Longitude", -180, 180)
    _validate_number(
        result, location, "elevation", "Elevation", -500, 12000, unit=" meters"
    )
    _validate_number(
        result, location, "light_buffer_hours", "Sunrise/sunset buffer", 0, 24, unit=" hours"
    )

    if latitude == 0.0 and longitude == 0.0:
        result.warnings.append(
            "Latitude and longitude are both 0 (a point in the Atlantic Ocean). "
            "Day/night switching will not match your real sunrise and sunset until you set your location."
        )


def _validate_integrations(config, result):
    paths = config.get("paths")
    if not isinstance(paths, dict):
        result.errors.append("The 'paths' section must be a JSON object.")
        paths = {}

    integrations = config.get("integrations")
    if not isinstance(integrations, dict):
        result.errors.append("The 'integrations' section must be a JSON object.")
        integrations = {}

    for key, meta in INTEGRATIONS.items():
        label = meta["label"]

        raw_path = paths.get(meta["path_key"], "")
        if raw_path is None:
            raw_path = ""
        if not isinstance(raw_path, str):
            result.errors.append(f"The {label} executable path must be text.")
            raw_path = ""
        path = raw_path.strip()

        entry = integrations.get(key)
        enabled = None
        if entry is not None and not isinstance(entry, dict):
            result.errors.append(
                f"The {label} integration setting must be a JSON object with an 'enabled' flag."
            )
        if isinstance(entry, dict):
            flag = entry.get("enabled")
            if isinstance(flag, bool):
                enabled = flag
            elif flag is not None:
                result.errors.append(f"The {label} 'enabled' setting must be true or false.")
        if enabled is None:
            enabled = bool(path)

        if enabled and not path:
            result.errors.append(
                f"{label} is enabled but no executable path is set. "
                "Set the executable path, or disable the integration."
            )
        elif enabled and not os.path.isfile(path):
            result.warnings.append(
                f"The {label} executable was not found on this computer. "
                "The integration is enabled but will be skipped at runtime until it is installed "
                "or the path is corrected."
            )


def _validate_automation(config, result):
    automation = config.get("automation")
    if not isinstance(automation, dict):
        result.errors.append("The 'automation' section must be a JSON object.")
        return

    for key in AUTOMATION_BOOLEAN_KEYS:
        if key in automation and not isinstance(automation[key], bool):
            result.errors.append(f"The automation setting '{key}' must be true or false.")

    if "razer_synapse_timeout_seconds" in automation:
        _validate_number(
            result,
            automation,
            "razer_synapse_timeout_seconds",
            "Razer Synapse detection timeout",
            0,
            600,
            required=False,
            unit=" seconds",
        )


def validate_config(config):
    """Validate a configuration. Returns a ValidationResult (errors + warnings).

    This is the strict path used by the wizard, the Settings UI, import and
    normal loading. Besides structure, version, ranges and types it also checks
    the external environment (whether enabled integrations' executables exist
    on this machine, using `os.path.isfile`). It must therefore never be called
    from timing-critical code — use `ConfigManager.load_runtime()` there.
    """
    result = ValidationResult()
    if not isinstance(config, dict):
        result.errors.append("The configuration root must be a JSON object.")
        return result

    _validate_version(config, result)
    _validate_location(config, result)
    _validate_lights(config, result)
    _validate_integrations(config, result)
    _validate_automation(config, result)
    return result


# ---------------------------------------------------------
# Safe file I/O
# ---------------------------------------------------------
def read_config_file(path, check_exists=True):
    """Read a JSON configuration with user-friendly error messages.

    ``check_exists=False`` skips the `os.path.isfile()` pre-check so that
    timing-critical callers touch the filesystem no more than the read itself.
    The error messages are identical either way.
    """
    if check_exists and not os.path.isfile(path):
        raise ConfigError(f"Configuration file not found: {os.path.basename(path)}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {os.path.basename(path)}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"That file is not a valid JSON configuration (line {exc.lineno}, column {exc.colno})."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"The configuration file could not be read: {exc}") from exc


def write_config_file(path, config, create_backup=False):
    """Write a configuration atomically, optionally backing up the previous one.

    Pattern: write a temporary file in the destination directory, flush it to
    disk, verify it parses back, then ``os.replace()`` it over the target. A
    failed write can therefore never truncate a valid configuration.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    if create_backup and os.path.isfile(path):
        backup_config_file(path)

    text = json.dumps(config, indent=4, ensure_ascii=False) + "\n"

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Verify the temporary file is readable JSON before replacing the target.
        with open(tmp_path, "r", encoding="utf-8") as handle:
            json.load(handle)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return path


def backup_config_file(path):
    """Copy `path` to a single latest backup (`config.json.bak`).

    Deliberately keeps exactly one backup instead of an ever-growing pile of
    timestamped files, so the data directory cannot grow without bound.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return None
    backup_path = path + BACKUP_SUFFIX
    shutil.copy2(path, backup_path)
    return backup_path


# ---------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------
class ConfigManager:
    """Owns loading, validating, migrating, saving, importing and exporting."""

    def __init__(self, config_path=None):
        self.app_dir = get_app_dir()
        self.config_path = os.path.abspath(config_path) if config_path else get_config_path(self.app_dir)
        self.data_dir = os.path.dirname(self.config_path) or "."
        self.portable = is_portable_mode(self.app_dir)
        # Schema version the last successful load() had to migrate from.
        self.last_load_migrated_from = None

    # --- paths -----------------------------------------------------------
    @property
    def backup_path(self):
        return self.config_path + BACKUP_SUFFIX

    def path_for(self, filename):
        return os.path.join(self.data_dir, filename)

    def default_config(self):
        return default_config()

    def ensure_data_dir(self):
        return ensure_data_dir(self.data_dir)

    # --- load / save -----------------------------------------------------
    def exists(self):
        return os.path.isfile(self.config_path)

    def load(self, path=None, validate=True):
        """Load, migrate and validate a configuration.

        Raises ConfigError / ConfigValidationError with user-friendly messages.
        Migrations are only applied in memory; `last_load_migrated_from` records
        the source version so the caller can persist (and back up) explicitly.
        """
        path = os.path.abspath(path or self.config_path)
        raw = read_config_file(path)
        config, from_version = migrate_config(raw)
        self.last_load_migrated_from = from_version if from_version != CONFIG_VERSION else None

        result = validate_config(config)
        if validate and result.errors:
            raise ConfigValidationError(result.errors, result.warnings)
        return config

    def load_runtime(self, path=None):
        """Read the configuration for timing-critical/read-only consumers.

        This is the *lightweight* loader used by the suspend callback: it reads
        the JSON file, applies migration/normalization and returns a defensively
        repaired copy. It deliberately performs

        * no validation — and therefore **no integration-executable filesystem
          probing** (`os.path.exists()` / `os.path.isfile()`),
        * no writes, no backups and no import/export logic,
        * no other side effects (`last_load_migrated_from` is left untouched).

        Malformed sections are tolerated here (see :func:`runtime_config`) so
        runtime code can always read the keys it expects. The strict path
        (:meth:`load`) stays the gate for the wizard, Settings, import and
        normal startup. Raises ConfigError only when the file cannot be read or
        parsed.
        """
        path = os.path.abspath(path or self.config_path)
        raw = read_config_file(path, check_exists=False)
        config, _from_version = migrate_config(raw)
        return runtime_config(config)

    def load_or_none(self):
        """Best-effort load: returns the configuration or None (never raises)."""
        try:
            return self.load()
        except ConfigError:
            return None

    def load_external(self, source_path):
        """Parse/migrate/validate a file without writing anything.

        Used by the wizard's "import existing configuration" step, where a user
        may still cancel and must not be left with a partially written config.
        Returns ``(config, from_version)``.
        """
        source_path = os.path.abspath(source_path)
        raw = read_config_file(source_path)
        if not isinstance(raw, dict):
            raise ConfigError("The selected file is not a Yeelight PC Companion configuration object.")
        config, from_version = migrate_config(raw)
        result = validate_config(config)
        if result.errors:
            raise ConfigValidationError(result.errors, result.warnings)
        return config, from_version

    def save(self, config, create_backup=False, path=None):
        """Validate and atomically write a configuration."""
        path = os.path.abspath(path or self.config_path)
        result = validate_config(config)
        if result.errors:
            raise ConfigValidationError(result.errors, result.warnings)
        self.ensure_data_dir()
        return write_config_file(path, config, create_backup=create_backup)

    def backup(self, path=None):
        return backup_config_file(os.path.abspath(path or self.config_path))

    def needs_migration(self):
        """True when the on-disk configuration is older than CONFIG_VERSION."""
        if not self.exists():
            return False
        try:
            version = detect_config_version(read_config_file(self.config_path))
        except ConfigError:
            return False
        return version is not None and version < CONFIG_VERSION

    # --- import / export ------------------------------------------------
    def import_config(self, source_path, target_path=None, create_backup=True):
        """Validate an external JSON config and install it only if it is valid.

        The currently active configuration is never replaced before the
        imported one has been parsed, migrated and validated. Returns the
        imported configuration; raises ConfigError/ConfigValidationError with
        user-friendly messages otherwise.
        """
        source_path = os.path.abspath(source_path)
        target_path = os.path.abspath(target_path or self.config_path)
        if source_path == target_path:
            raise ConfigError("The selected file is already the active configuration.")

        config, _from_version = self.load_external(source_path)

        if os.path.isfile(target_path):
            backup_config_file(target_path)
        ensure_data_dir(os.path.dirname(target_path) or ".")
        write_config_file(target_path, config, create_backup=False)
        return config

    def export_config(self, destination, config=None):
        """Write the complete current configuration to a portable JSON file."""
        destination = os.path.abspath(destination)
        if os.path.abspath(self.config_path) == destination:
            raise ConfigError("Choose a different file name; that is the active configuration file.")

        if config is None:
            config = self.load()
        exported = normalize_config(config)
        exported["config_version"] = CONFIG_VERSION
        try:
            write_config_file(destination, exported, create_backup=False)
        except OSError as exc:
            raise ConfigError(f"The configuration could not be exported: {exc}") from exc
        return destination

    # --- legacy discovery ------------------------------------------------
    def legacy_candidates(self):
        """Plausible locations of a pre-versioning configuration.

        * beside the executable (old packaged builds stored it there),
        * the development layout where the packaged build lives in
          ``<repo>\\dist\\YeelightPCCompanion`` while the personal config sits at
          the repository root.
        """
        candidates = [os.path.join(self.app_dir, CONFIG_FILENAME)]
        directory = self.app_dir
        for _ in range(3):
            parent = os.path.dirname(directory)
            if not parent or parent == directory:
                break
            directory = parent
            # Only accept an ancestor directory that is safely identifiable as
            # the source checkout (never e.g. a Program Files directory).
            if _looks_like_dev_root(directory):
                candidates.append(os.path.join(directory, CONFIG_FILENAME))
                break
        return [c for c in candidates if os.path.abspath(c) != os.path.abspath(self.config_path)]

    def find_legacy_config(self):
        for candidate in self.legacy_candidates():
            if os.path.isfile(candidate):
                return candidate
        return None


def _looks_like_dev_root(directory):
    """Safely recognise the source checkout the packaged build was produced from."""
    if not directory or not os.path.isdir(directory):
        return False
    markers = ("yeelight_pc_companion.py", CONFIG_EXAMPLE_FILENAME)
    return all(os.path.isfile(os.path.join(directory, marker)) for marker in markers)
