"""Read-only power checks for configured bulbs, including during music mode.

Unicast status requests use the pinned Yeelight library's discovery transport
and parser. They target only saved addresses: no LAN scan, power command or
music-mode change is involved. Silence is never treated as a confirmed OFF.
"""

from contextlib import closing
from dataclasses import dataclass
import time


HEALTH_CHECK_INTERVAL_MS = 30 * 60 * 1000
HEALTH_CHECK_SETTLE_MS = 30 * 1000
HEALTH_PROBE_TIMEOUT_SECONDS = 2.0

NOT_RESPONDING = "not_responding"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class DeviceHealth:
    device_id: str
    name: str
    ip: str
    state: str
    detail: str = ""


def probe_device_power(device):
    """Read one bulb's advertised power, closing its socket within one deadline."""
    from yeelight.main import parse_capabilities, send_discovery_packet

    device_id = device.get("id", "")
    name = device.get("name", "Yeelight device")
    ip = device.get("ip", "").strip()

    def result(state, detail=""):
        return DeviceHealth(device_id, name, ip, state, detail)

    deadline = time.monotonic() + HEALTH_PROBE_TIMEOUT_SECONDS
    try:
        with closing(send_discovery_packet(
            timeout=HEALTH_PROBE_TIMEOUT_SECONDS, ip_address=ip
        )) as sock:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result(NOT_RESPONDING)
                sock.settimeout(remaining)
                payload, peer = sock.recvfrom(65507)
                if peer[0] != ip:
                    continue
                try:
                    capabilities = {
                        key.lower(): value for key, value in parse_capabilities(payload).items()
                    }
                except (ValueError, UnicodeError):
                    return result(UNKNOWN, "Unreadable status reply")
                reported_id = capabilities.get("id")
                if (
                    device_id.startswith("yeelight:")
                    and reported_id
                    and device_id.lower() != "yeelight:" + reported_id.lower()
                ):
                    return result(UNKNOWN, "Different device at this address")
                power = capabilities.get("power", "").lower()
                if power in ("on", "off"):
                    return result(power)
                return result(UNKNOWN, "Power status unavailable")
    except OSError:
        return result(NOT_RESPONDING)
