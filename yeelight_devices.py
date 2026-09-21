"""Yeelight device model and LAN discovery.

This module owns three things:

* the **stable device-identity scheme** — ``yeelight:<device-id>`` when the
  device reports its own Yeelight id, ``manual:<uuid>`` when this application
  assigns the identity (manual add, migrated legacy address slots),
* **construction and structural list operations** for configuration device
  entries (add / update / remove / duplicate detection / summary),
* **LAN discovery** of Yeelight devices plus the matching of discovery results
  against the configured device list.

A configured device entry is a plain dictionary — the persisted shape::

    {"id": "yeelight:0x00000000037073d2",
     "name": "Desk Lamp",
     "ip": "192.168.1.50",
     "enabled": True}

``model``/``firmware`` are optional and only written when discovery provided
them; the discovery response is never dumped into the configuration.

Only the standard library is imported at module level. The third-party
``yeelight`` package is imported lazily inside :func:`discover_devices`, so the
device model and the matching logic stay testable without a network, a GUI or
the discovery protocol.
"""

from __future__ import annotations

import copy
import ipaddress
import re
import uuid
from urllib.parse import urlparse

# ---------------------------------------------------------
# Device identity
# ---------------------------------------------------------
# `yeelight:<device-id>` — the identity the device itself reports over SSDP.
# `manual:<uuid>`        — an identity assigned by this application (a manually
#                          added device, or a device migrated from a legacy
#                          address-only slot that has no known Yeelight id yet).
DEVICE_ID_DISCOVERED_PREFIX = "yeelight:"
DEVICE_ID_LOCAL_PREFIX = "manual:"

DEVICE_ID_MAX_LENGTH = 200
DEVICE_NAME_MAX_LENGTH = 80
DEFAULT_DEVICE_NAME = "Yeelight device"

# ---------------------------------------------------------
# Discovery parameters
# ---------------------------------------------------------
# Discovery is always user-triggered and bounded: the yeelight package reports
# for exactly `timeout` seconds before returning whatever answered.
DISCOVERY_TIMEOUT_SECONDS = 5.0
DISCOVERY_MIN_TIMEOUT_SECONDS = 0.5
DISCOVERY_MAX_TIMEOUT_SECONDS = 30.0
DEVICE_COMMAND_PORT = 55443

# ---------------------------------------------------------
# Matching states used by the discovery dialog
# ---------------------------------------------------------
MATCH_NEW = "new"
MATCH_ALREADY_ADDED = "already_added"
MATCH_IP_CHANGED = "ip_changed"
MATCH_ID_AVAILABLE = "id_available"

MATCH_LABELS = {
    MATCH_NEW: "New",
    MATCH_ALREADY_ADDED: "Already added",
    MATCH_IP_CHANGED: "IP changed",
    MATCH_ID_AVAILABLE: "Already added - device ID available",
}

_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]*$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class DeviceError(Exception):
    """Raised for device-list problems that have a user-friendly message."""


# ---------------------------------------------------------
# Small helpers
# ---------------------------------------------------------
def _clean_text(value):
    """A stripped string, or None when the value is not usable text."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _iter_entries(devices):
    """Yield ``(index, entry)`` for every entry that is a JSON object."""
    if not isinstance(devices, (list, tuple)):
        return
    for index, entry in enumerate(devices):
        if isinstance(entry, dict):
            yield index, entry


def normalized_ip(value):
    """Canonical text form of an IP literal, or None when it is not one.

    Used for duplicate detection and for matching discovery results against the
    configuration, so ``192.168.001.050`` and ``192.168.1.50`` are one device.
    """
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def validate_device_address(value, label="Yeelight device address", required=False):
    """Return an error string for an invalid device address, else None.

    Blank values are only accepted when ``required`` is false; a configured
    device entry always needs an address, while the legacy "optional slot"
    behaviour keeps working for callers that ask for it.
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        return f"{label} must be text."
    text = value.strip()
    if not text:
        if required:
            return (
                f"A {label.lower()} is required. "
                "Enter a LAN address such as 192.168.1.50."
            )
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return (
            f"{label} '{text}' is not a valid IP address. "
            "Enter a LAN address such as 192.168.1.50, or leave it blank."
        )
    return None


def validate_device_name(value):
    """Return an error string for an invalid friendly name, else None."""
    if not isinstance(value, str):
        return "Every Yeelight device needs a name."
    text = value.strip()
    if not text:
        return "Every Yeelight device needs a name."
    if _CONTROL_CHARACTERS.search(text):
        return "A device name cannot contain control characters."
    if len(text) > DEVICE_NAME_MAX_LENGTH:
        return f"A device name can be at most {DEVICE_NAME_MAX_LENGTH} characters long."
    return None


def validate_device_id(value):
    """Return an error string for an invalid stable device id, else None."""
    if not isinstance(value, str):
        return "Every Yeelight device needs a stable id."
    text = value.strip()
    if not text:
        return "Every Yeelight device needs a stable id."
    if text != value:
        return "A device id cannot start or end with whitespace."
    if len(text) > DEVICE_ID_MAX_LENGTH:
        return f"A device id can be at most {DEVICE_ID_MAX_LENGTH} characters long."
    if not _DEVICE_ID_PATTERN.match(text):
        return (
            "A device id may only contain letters, digits and the characters "
            "':' '.' '_' '-'."
        )
    return None


# ---------------------------------------------------------
# Identity construction
# ---------------------------------------------------------
def discovered_device_id(raw_device_id):
    """``yeelight:<device-id>`` for an id reported by the device itself."""
    text = _clean_text(raw_device_id)
    if text is None:
        return None
    if text.startswith(DEVICE_ID_DISCOVERED_PREFIX):
        return text
    return DEVICE_ID_DISCOVERED_PREFIX + text


def new_local_device_id():
    """A durable identity assigned by this application."""
    return DEVICE_ID_LOCAL_PREFIX + uuid.uuid4().hex


def is_local_device_id(device_id):
    """True when the identity was assigned locally instead of by the device."""
    return isinstance(device_id, str) and device_id.startswith(DEVICE_ID_LOCAL_PREFIX)


def new_device(name, ip, enabled=True, device_id=None, model=None, firmware=None):
    """Build one configured device entry.

    A device id is generated when the caller has none (manual add, legacy
    migration): it is durable, unique and unrelated to the display name, so
    renaming a device never changes its identity.
    """
    entry = {
        "id": _clean_text(device_id) or new_local_device_id(),
        "name": name if isinstance(name, str) else str(name or ""),
        "ip": ip.strip() if isinstance(ip, str) else ip,
        "enabled": bool(enabled),
    }
    model = _clean_text(model)
    if model:
        entry["model"] = model
    firmware = _clean_text(firmware)
    if firmware:
        entry["firmware"] = firmware
    return entry


def copy_devices(devices):
    """A deep copy of a device list (only the entries are copied)."""
    if not isinstance(devices, (list, tuple)):
        return []
    return [copy.deepcopy(entry) for entry in devices if isinstance(entry, dict)]


def find_device(devices, device_id):
    """The entry with this id from a device list, or None."""
    if not isinstance(device_id, str):
        return None
    for _index, entry in _iter_entries(devices):
        if entry.get("id") == device_id:
            return entry
    return None


def find_duplicate_ids(devices):
    """``{device_id: [index, ...]}`` for ids used by more than one entry."""
    seen = {}
    for index, entry in _iter_entries(devices):
        device_id = _clean_text(entry.get("id"))
        if device_id is None:
            continue
        seen.setdefault(device_id, []).append(index)
    return {device_id: indexes for device_id, indexes in seen.items() if len(indexes) > 1}


def find_duplicate_ips(devices):
    """``{normalized_ip: [index, ...]}`` for addresses used more than once."""
    seen = {}
    for index, entry in _iter_entries(devices):
        ip = normalized_ip(entry.get("ip"))
        if ip is None:
            continue
        seen.setdefault(ip, []).append(index)
    return {ip: indexes for ip, indexes in seen.items() if len(indexes) > 1}


def _is_enabled(entry):
    """Explicit enablement only: true/false, with 0/1 tolerated."""
    value = entry.get("enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return False


def enabled_devices(devices):
    """Every entry that is explicitly enabled and has an address."""
    return [
        entry
        for _index, entry in _iter_entries(devices)
        if _is_enabled(entry) and _clean_text(entry.get("ip")) is not None
    ]


def device_summary(devices):
    """``"3 enabled / 4 configured"`` (or a friendly text for an empty list)."""
    total = 0
    enabled = 0
    for _index, entry in _iter_entries(devices):
        total += 1
        if _is_enabled(entry) and _clean_text(entry.get("ip")) is not None:
            enabled += 1
    if not total:
        return "none configured"
    return f"{enabled} enabled / {total} configured"


def duplicate_ip_error(devices, ip, ignore_device_id=None):
    """A friendly error when `ip` is already used by another entry, else None."""
    candidate = normalized_ip(ip)
    if candidate is None:
        return None
    for _index, entry in _iter_entries(devices):
        if ignore_device_id is not None and entry.get("id") == ignore_device_id:
            continue
        if normalized_ip(entry.get("ip")) == candidate:
            name = _clean_text(entry.get("name")) or "another device"
            return (
                f"The address {candidate} is already used by '{name}'. "
                "Every Yeelight device needs its own address."
            )
    return None


# ---------------------------------------------------------
# List operations (used by the device list UI)
# ---------------------------------------------------------
def add_device(devices, name, ip, enabled=True, device_id=None):
    """Return a new list with one device appended.

    Raises :class:`DeviceError` when the entry is unusable, when the address is
    already configured, or when the requested identity already exists.
    """
    name = name if isinstance(name, str) else ""
    error = validate_device_name(name)
    if error:
        raise DeviceError(error)
    error = validate_device_address(ip, required=True)
    if error:
        raise DeviceError(error)

    updated = copy_devices(devices)
    if device_id is not None:
        error = validate_device_id(device_id)
        if error:
            raise DeviceError(error)
        if find_device(updated, device_id) is not None:
            raise DeviceError("That device is already in the list.")
    error = duplicate_ip_error(updated, ip)
    if error:
        raise DeviceError(error)

    updated.append(new_device(name.strip(), ip.strip(), enabled, device_id=device_id))
    return updated


def update_device(devices, device_id, name=None, ip=None, enabled=None):
    """Return a new list with one device edited.

    Only the given fields change. The stable id is never touched, so a rename
    cannot change a device's identity.
    """
    updated = copy_devices(devices)
    entry = find_device(updated, device_id)
    if entry is None:
        raise DeviceError("That device is no longer in the list.")

    if name is not None:
        error = validate_device_name(name)
        if error:
            raise DeviceError(error)
        entry["name"] = name.strip()
    if ip is not None:
        error = validate_device_address(ip, required=True)
        if error:
            raise DeviceError(error)
        error = duplicate_ip_error(updated, ip, ignore_device_id=device_id)
        if error:
            raise DeviceError(error)
        entry["ip"] = ip.strip()
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    return updated


def remove_device(devices, device_id):
    """Return a new list without the entry that has this id."""
    kept = [
        entry
        for _index, entry in _iter_entries(devices)
        if entry.get("id") != device_id
    ]
    return kept


# ---------------------------------------------------------
# Discovery
# ---------------------------------------------------------
class DiscoveryReport:
    """Outcome of one user-triggered LAN discovery.

    ``devices`` is the normalized result list and ``error`` is a user-safe
    message ('' on success). Zero devices is **not** an error: an empty list
    with an empty error is the normal "nothing answered" outcome.
    """

    __slots__ = ("devices", "error")

    def __init__(self, devices=None, error=""):
        self.devices = list(devices or [])
        self.error = error or ""

    def __bool__(self):
        return bool(self.devices)

    def __repr__(self):
        return f"DiscoveryReport(devices={len(self.devices)}, error={self.error!r})"


def default_device_name(reported_name=None, model=None):
    """A friendly starting name for a device the user has not renamed yet."""
    name = _clean_text(reported_name)
    if name:
        return name[:DEVICE_NAME_MAX_LENGTH]
    model = _clean_text(model)
    if model:
        return f"Yeelight {model}"[:DEVICE_NAME_MAX_LENGTH]
    return DEFAULT_DEVICE_NAME


def _location_parts(capabilities):
    """``(ip, port)`` from a discovery ``Location`` header, if it has one."""
    location = _clean_text(capabilities.get("Location") or capabilities.get("location"))
    if not location:
        return None, None
    try:
        parsed = urlparse(location)
    except ValueError:
        return None, None
    port = None
    try:
        port = parsed.port
    except ValueError:
        port = None
    return parsed.hostname, port


def _normalize_discovery_entry(raw):
    """One raw ``discover_bulbs()`` result as a discovery device, or None."""
    if not isinstance(raw, dict):
        return None

    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}

    ip = normalized_ip(raw.get("ip"))
    port = raw.get("port")
    if ip is None or not isinstance(port, int) or port <= 0:
        location_ip, location_port = _location_parts(capabilities)
        if ip is None:
            ip = normalized_ip(location_ip)
        if not isinstance(port, int) or port <= 0:
            port = location_port if isinstance(location_port, int) and location_port > 0 else None
    if ip is None:
        return None

    device_id = _clean_text(capabilities.get("id"))
    return {
        # Selection key used by the discovery dialog and by `apply_discovery_plan`.
        "key": discovered_device_id(device_id) or f"ip:{ip}",
        # Application-form stable id, or None when the device reported none.
        "id": discovered_device_id(device_id),
        # The raw identity the device itself reported (kept out of the config).
        "device_id": device_id,
        "ip": ip,
        "port": port or DEVICE_COMMAND_PORT,
        "model": _clean_text(capabilities.get("model")),
        "firmware": _clean_text(capabilities.get("fw_ver")),
        "name": _clean_text(capabilities.get("name")),
        "power": _clean_text(capabilities.get("power")),
    }


def normalize_discovery_results(raw_bulbs):
    """Normalize and de-duplicate raw discovery responses.

    Duplicate responses (the same device answering twice, or twice with the same
    address) are collapsed into one entry. Cosmetic metadata is taken from the
    response only — no follow-up RPC call is made for any device.
    """
    devices = []
    seen_ids = set()
    seen_addresses = set()

    for raw in raw_bulbs or []:
        device = _normalize_discovery_entry(raw)
        if device is None:
            continue
        device_id = device.get("device_id")
        if device_id and device_id in seen_ids:
            continue
        address = (device["ip"], device["port"])
        if address in seen_addresses:
            continue
        if device_id:
            seen_ids.add(device_id)
        seen_addresses.add(address)
        devices.append(device)

    return devices


def _clamp_timeout(timeout):
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return DISCOVERY_TIMEOUT_SECONDS
    return min(max(value, DISCOVERY_MIN_TIMEOUT_SECONDS), DISCOVERY_MAX_TIMEOUT_SECONDS)


def clamped_discovery_timeout(timeout=DISCOVERY_TIMEOUT_SECONDS):
    """The timeout ``discover_devices()`` will actually use for *timeout*.

    Public because every caller that *waits* for a discovery has to be bounded by
    the same value the search itself is bounded by: a UI guard derived from an
    unclamped request would let a caller passing ``999`` show a modal progress
    dialog for ~1004 seconds while the search behind it stops after 30. An
    unusable value (missing, text, ``None``) becomes the default, so no caller
    has to validate it and none of them can raise.
    """
    return _clamp_timeout(timeout)


def _network_error_message(exc):
    detail = _clean_text(str(exc)) or exc.__class__.__name__
    return (
        "The network search could not be completed "
        f"({detail}). Windows Firewall or the network configuration may be "
        "blocking the search."
    )

def discover_devices(timeout=DISCOVERY_TIMEOUT_SECONDS, interface=False):
    """Search the LAN for Yeelight devices. Never raises.

    Uses the supported discovery function of the installed ``yeelight`` package
    (an SSDP ``M-SEARCH`` for ``wifi_bulb``) instead of a second protocol stack.
    The call blocks for the whole (bounded) timeout, so it must be run off the
    GUI thread and must never run during suspend.
    """
    try:
        from yeelight import discover_bulbs
    except Exception as exc:  # pragma: no cover - the package is a dependency
        return DiscoveryReport([], f"The Yeelight library is not available ({exc}).")

    try:
        raw_bulbs = discover_bulbs(timeout=_clamp_timeout(timeout), interface=interface)
    except Exception as exc:
        return DiscoveryReport([], _network_error_message(exc))

    return DiscoveryReport(normalize_discovery_results(raw_bulbs), "")


# ---------------------------------------------------------
# Matching discovery results against the configuration
# ---------------------------------------------------------
def plan_discovery(discovered, configured):
    """Pair every discovered device with the configured device list.

    Each result is a dict describing what would happen if it were accepted::

        {"key": ..., "state": MATCH_*, "discovered": {...},
         "configured": {...} | None, "configured_index": int | None,
         "configured_id": str | None, "previous_ip": str | None}

    Matching prefers the **stable device id**, so a device whose address changed
    is recognized instead of being offered as a second device. A device that is
    only found by address is "already added"; when such an entry still carries a
    locally assigned id and the device now reports its own Yeelight id, the
    result is ``MATCH_ID_AVAILABLE`` so the user can adopt the real identity.
    """
    by_id = {}
    by_ip = {}
    for index, entry in _iter_entries(configured):
        device_id = _clean_text(entry.get("id"))
        if device_id is not None and device_id not in by_id:
            by_id[device_id] = (index, entry)
        ip = normalized_ip(entry.get("ip"))
        if ip is not None and ip not in by_ip:
            by_ip[ip] = (index, entry)

    plan = []
    for device in discovered or []:
        if not isinstance(device, dict):
            continue
        ip = normalized_ip(device.get("ip"))
        if ip is None:
            continue

        device_id = _clean_text(device.get("id"))
        index = None
        entry = None
        previous_ip = None
        state = MATCH_NEW

        if device_id is not None and device_id in by_id:
            index, entry = by_id[device_id]
            previous_ip = normalized_ip(entry.get("ip"))
            state = MATCH_ALREADY_ADDED if previous_ip == ip else MATCH_IP_CHANGED
        elif ip in by_ip:
            index, entry = by_ip[ip]
            previous_ip = normalized_ip(entry.get("ip"))
            if device_id is not None and is_local_device_id(entry.get("id")):
                state = MATCH_ID_AVAILABLE
            else:
                state = MATCH_ALREADY_ADDED

        plan.append(
            {
                "key": device.get("key") or f"ip:{ip}",
                "state": state,
                "discovered": device,
                "configured": entry,
                "configured_index": index,
                "configured_id": _clean_text(entry.get("id")) if entry else None,
                "previous_ip": previous_ip,
            }
        )
    return plan


def selectable_match(item):
    """True when accepting this plan result changes the configuration."""
    return item is not None and item.get("state") in (
        MATCH_NEW,
        MATCH_IP_CHANGED,
        MATCH_ID_AVAILABLE,
    )


def apply_discovery_plan(configured, plan, selected_keys=None, names=None):
    """Return the device list with the selected discovery results applied.

    ``selected_keys=None`` accepts every actionable result. ``names`` maps a
    plan key to a name the user typed in the results dialog. Existing entries
    keep their id (except when adopting a device-reported identity) and their
    enablement, and never lose a name.

    Raises :class:`DeviceError` when applying the selection would produce a
    duplicate address.
    """
    devices = copy_devices(configured)
    names = names if isinstance(names, dict) else {}

    for item in plan or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if selected_keys is not None and key not in selected_keys:
            continue

        device = item.get("discovered")
        if not isinstance(device, dict):
            continue
        ip = normalized_ip(device.get("ip"))
        if ip is None:
            continue

        state = item.get("state")
        if state == MATCH_NEW:
            devices.append(
                new_device(
                    _clean_text(names.get(key))
                    or default_device_name(device.get("name"), device.get("model")),
                    ip,
                    enabled=True,
                    device_id=device.get("id"),
                    model=device.get("model"),
                    firmware=device.get("firmware"),
                )
            )
            continue

        if state not in (MATCH_IP_CHANGED, MATCH_ID_AVAILABLE):
            continue

        index = item.get("configured_index")
        if not isinstance(index, int) or not (0 <= index < len(devices)):
            continue
        entry = devices[index]
        if not isinstance(entry, dict):
            continue
        # Defensive: the copied list must still hold the same entry.
        if item.get("configured_id") and entry.get("id") != item.get("configured_id"):
            continue

        if state == MATCH_ID_AVAILABLE:
            entry["id"] = device.get("id")
        entry["ip"] = ip
        for field in ("model", "firmware"):
            value = _clean_text(device.get(field))
            if value:
                entry[field] = value
        chosen_name = _clean_text(names.get(key))
        if chosen_name:
            entry["name"] = chosen_name

    duplicates = find_duplicate_ips(devices)
    if duplicates:
        address = sorted(duplicates)[0]
        raise DeviceError(
            f"The address {address} would be used by more than one Yeelight device. "
            "Update the affected device instead of adding a second entry for it."
        )
    return devices
