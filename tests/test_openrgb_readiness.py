"""Tests for the OpenRGB detection-complete readiness gate in front of Artemis.

The restore sequence used to request the OpenRGB launch and then sleep a fixed 8
seconds before starting Artemis. That is a race: OpenRGB binds its SDK server
while it is still enumerating controllers, so Artemis' OpenRGB plugin could
connect during detection, see none (or only some) of the devices, and only show
them after Artemis or the plugin was restarted.

The first replacement - a controller count that stayed identical for three
consecutive probes, then a short settle - was itself a false positive, and
everything here is built around the measurement that proves it. From OpenRGB's
own log on the maintainer's PC, milliseconds after the OpenRGB process starts:

| Event                            | Time                            |
| -------------------------------- | ------------------------------- |
| the SDK server answers requests  | 31 ms                           |
| controller #1 registered         | 1 136 ms                        |
| controllers #2-#4 registered     | 11 684 / 11 755 / 11 883 ms     |
| detection completed              | 12 337 ms                       |

The count therefore sits *unchanged* at **one** for 10.5 seconds in the middle
of a 12.3-second startup. Three identical observations at a 0.5 s poll interval
plus a 1.5 s settle declared readiness at ~3.7 s: roughly eight seconds before
controllers 2-4 existed. `TestTheMeasuredPlateauIsNeverReady` is the regression
test for exactly that timeline, and it fails against that implementation (see
`gate_launched_option` for why it can run against both).

The real signal is OpenRGB's own detection event stream (SDK protocol 6):
`NET_PACKET_ID_DETECTION_STARTED` (101),
`NET_PACKET_ID_DETECTION_PROGRESS_CHANGED` (102) and
`NET_PACKET_ID_DETECTION_COMPLETE` (103). A protocol-6 client receives 103 when
detection finishes, however early during detection it connected - the server
broadcasts to the clients that exist at that moment - and protocol 6 has **no**
packet that asks for the current detection state, so a client connecting after
103 was sent never learns on that connection that detection finished. The gate
is therefore explicit about the two situations it can be in; see §3 and §4.

No real OpenRGB, no real socket, no real clock and no real sleeping is used: the
gate talks to an in-memory SDK server that decodes the client's packets
independently, replays the measured timeline as scripted server pushes, and runs
on a clock that only moves when something waits. `socket.create_connection` is
patched in every test that reaches the transport, so the suite never opens a
connection.
"""

import inspect
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_manager as cm  # noqa: E402
import windows_tasks as wt  # noqa: E402

try:
    import yeelight_pc_companion as app  # noqa: E402
except Exception as exc:  # pragma: no cover - depends on the test machine
    app = None
    APP_IMPORT_ERROR = exc
else:
    APP_IMPORT_ERROR = None

SKIP_REASON = f"yeelight_pc_companion is not importable here: {APP_IMPORT_ERROR}"


# ---------------------------------------------------------
# The SDK protocol, spelled out here on purpose
# ---------------------------------------------------------
# The fake server below decodes requests and builds replies from these literals
# instead of the application's constants, so a change to the application's
# framing cannot silently change what it is compared against.
MAGIC = b"ORGB"
HEADER_SIZE = 16
PACKET_REQUEST_CONTROLLER_COUNT = 0
PACKET_REQUEST_CONTROLLER_DATA = 1
PACKET_ACK = 10
PACKET_REQUEST_PROTOCOL_VERSION = 40
PACKET_SET_SERVER_NAME = 51
PACKET_DEVICE_LIST_UPDATED = 100
PACKET_DETECTION_STARTED = 101
PACKET_DETECTION_PROGRESS_CHANGED = 102
PACKET_DETECTION_COMPLETE = 103
PACKET_REQUEST_RESCAN_DEVICES = 140
PACKET_RGBCONTROLLER_SIGNALUPDATE = 1150
SDK_PROTOCOL_VERSION = 6
DETECTION_PROTOCOL_VERSION = 6
DETECTION_EVENT_IDS = (
    PACKET_DETECTION_STARTED,
    PACKET_DETECTION_PROGRESS_CHANGED,
    PACKET_DETECTION_COMPLETE,
)
STATUS_OK = 0
STATUS_UNSUPPORTED = 2
REQUEST_TIMEOUT = 0.6
CONTROLLER_IDS = (0x1000, 0x1001, 0x1002, 0x1003)


# ---------------------------------------------------------
# The measured cold start (OpenRGB 1.0, maintainer's PC, 4 controllers)
# ---------------------------------------------------------
MEASURED_LISTENING_SECONDS = 0.031
MEASURED_DETECTION_STARTED_SECONDS = 0.030
MEASURED_PROGRESS_SECONDS = (0.211, 0.262, 1.025, 1.329, 11.904)
MEASURED_CONTROLLER_SECONDS = (1.136, 11.684, 11.755, 11.883)
MEASURED_DETECTION_COMPLETE_SECONDS = 12.337

# The restore sequence waits 5 s for the system and 3 s for music-mode
# connections to close before it even looks at OpenRGB, so a server whose
# detection completes "during the restore" has to push the completion event
# after that.
SEQUENCE_REACHES_THE_GATE_AT = 8.0


def progress_percent(moment):
    """A plausible detection percentage for a moment of the measured startup."""
    return min(100, int(round(100.0 * moment / MEASURED_DETECTION_COMPLETE_SECONDS)))


def server_packet(packet_id, payload=b"", device_id=0):
    """A framed packet exactly as OpenRGB builds one."""
    return MAGIC + struct.pack("<III", device_id, packet_id, len(payload)) + payload


def event_packet(packet_id, device_id=0):
    """A zero-body server event (detection started/completed, device list)."""
    return server_packet(packet_id, b"", device_id)


def progress_packet(percent, text="Detecting I2C devices", device_id=0, string_length=None):
    """A NET_PACKET_ID_DETECTION_PROGRESS_CHANGED packet."""
    body = text.encode("utf-8") + b"\x00"
    size = 4 + 4 + 2 + len(body)
    length = len(body) if string_length is None else string_length
    return server_packet(
        PACKET_DETECTION_PROGRESS_CHANGED, struct.pack("<IIH", size, percent, length) + body, device_id
    )


def count_payload(count, protocol_version=0, ids=CONTROLLER_IDS):
    """The controller-count reply body for a negotiated protocol version."""
    body = struct.pack("<I", count)
    if protocol_version >= DETECTION_PROTOCOL_VERSION:
        body += b"".join(struct.pack("<I", value) for value in ids[:count])
    return body


def decode_request(raw):
    """Decode a framed SDK packet into (magic, device_id, packet_id, body)."""
    magic, device_id, packet_id, size = struct.unpack("<4sIII", raw[:HEADER_SIZE])
    return magic, device_id, packet_id, raw[HEADER_SIZE : HEADER_SIZE + size]


def request_ids(raw_requests):
    return [decode_request(entry)[2] for entry in raw_requests]


def measured_startup(counts=True, complete_seconds=MEASURED_DETECTION_COMPLETE_SECONDS):
    """The measured startup as (count timeline, scripted pushes)."""
    count_timeline = [(0.0, 0)]
    if counts:
        for index, moment in enumerate(sorted(MEASURED_CONTROLLER_SECONDS)):
            count_timeline.append((moment, index + 1))
    pushes = [
        (MEASURED_DETECTION_STARTED_SECONDS, event_packet(PACKET_DETECTION_STARTED))
    ]
    pushes += [
        (moment, progress_packet(progress_percent(moment)))
        for moment in MEASURED_PROGRESS_SECONDS
    ]
    if complete_seconds is not None:
        pushes.append((complete_seconds, event_packet(PACKET_DETECTION_COMPLETE)))
    return count_timeline, pushes


def yeelight_device(name, ip, enabled=True):
    """One configured Yeelight device entry (the v2 shape)."""
    return {
        "id": "manual:readiness-%s" % ip.replace(".", "-"),
        "name": name,
        "ip": ip,
        "enabled": enabled,
    }


# ---------------------------------------------------------
# A scripted, in-memory OpenRGB SDK server on a fake clock
# ---------------------------------------------------------
class FakeClock:
    """A monotonic clock that only moves when something waits on it."""

    def __init__(self, start=0.0):
        self.now = float(start)

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += max(0.0, float(seconds))
        return self.now

    def sleep(self, seconds):
        return self.advance(seconds)


class FakeSdkSocket:
    """One accepted SDK connection: decodes requests, serves scripted pushes.

    Reading models blocking honestly: when nothing is buffered the clock moves
    to the next scripted push, or by the whole read timeout when the server has
    nothing to say, and only then does the read fail with `socket.timeout`.
    """

    def __init__(self, server, accepted_at):
        self.server = server
        self.clock = server.clock
        self.timeout = REQUEST_TIMEOUT
        self.accepted_at = accepted_at
        self.client_protocol_version = 0
        self.sent = b""
        self.buffer = b""
        self.closed = False
        self.closed_at = None
        self.requests = []
        # Only pushes that happen after this client connected can reach it: the
        # server broadcasts an event to the clients that exist at that moment.
        self.pending = [entry for entry in server.pushes if entry[0] >= accepted_at]

    # --- client side of the socket -------------------------------------
    def settimeout(self, value):
        self.timeout = float(value)

    def sendall(self, data):
        if self.closed:
            raise OSError("send on a closed socket")
        self.sent += data
        while len(self.sent) >= HEADER_SIZE:
            header = self.sent[:HEADER_SIZE]
            _magic, _device_id, _packet_id, size = struct.unpack("<4sIII", header)
            if len(self.sent) < HEADER_SIZE + size:
                return
            request = self.sent[: HEADER_SIZE + size]
            self.sent = self.sent[HEADER_SIZE + size :]
            self.requests.append(request)
            self.server.requests.append(request)
            self.server.request_log.append((self.clock.now, request))
            self._reply_to(request)

    def recv(self, size):
        if self.closed:
            raise OSError("recv on a closed socket")
        if self.server.dies_at is not None and self.clock.now >= self.server.dies_at:
            raise ConnectionResetError("OpenRGB went away")
        if not self.buffer and not self._fill():
            raise socket.timeout("no SDK packet within the read timeout")
        chunk = self.buffer[:size]
        self.buffer = self.buffer[size:]
        return chunk

    def close(self):
        if not self.closed:
            self.closed = True
            self.closed_at = self.clock.now
            self.server.sockets_closed.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    # --- server side of the socket -------------------------------------
    def _fill(self):
        """Move the clock to the next push (or by the read timeout) and deliver."""
        if self._deliver_due():
            return True
        now = self.clock.now
        next_push = self.pending[0][0] if self.pending else None
        if next_push is not None and next_push <= now + self.timeout:
            self.clock.advance(next_push - now)
        else:
            self.clock.advance(self.timeout)
        return self._deliver_due()

    def _deliver_due(self):
        due = []
        while self.pending and self.pending[0][0] <= self.clock.now:
            _moment, raw = self.pending.pop(0)
            if self._may_receive(raw):
                due.append(raw)
        if not due:
            return False
        self.buffer += b"".join(due)
        return True

    def _may_receive(self, raw):
        """The server sends detection events only to protocol-6 clients.

        `NetworkServer::SendRequest_DetectionCompleted()` returns immediately
        for a client whose `client_protocol_version` is below 6, so an older
        connection never sees them.
        """
        if len(raw) < HEADER_SIZE:
            return True
        packet_id = struct.unpack("<4sIII", raw[:HEADER_SIZE])[2]
        if packet_id in DETECTION_EVENT_IDS:
            return self.client_protocol_version >= DETECTION_PROTOCOL_VERSION
        return True

    def _reply_to(self, request):
        _magic, device_id, packet_id, body = decode_request(request)
        if packet_id == PACKET_REQUEST_PROTOCOL_VERSION:
            requested = struct.unpack("<I", body)[0] if len(body) == 4 else 0
            version = self.server.protocol_version
            if version is None:
                # A server without protocol versioning answers nothing at all.
                self._ack(device_id, packet_id, STATUS_UNSUPPORTED)
                return
            self.client_protocol_version = min(requested, version)
            self.buffer += server_packet(
                PACKET_REQUEST_PROTOCOL_VERSION, struct.pack("<I", version)
            )
            self.buffer += server_packet(PACKET_SET_SERVER_NAME, b"OpenRGB Test Server\x00")
            self._ack(device_id, packet_id, STATUS_OK)
        elif packet_id == PACKET_REQUEST_CONTROLLER_COUNT:
            count = self.server.count_at(self.clock.now)
            self.buffer += server_packet(
                PACKET_REQUEST_CONTROLLER_COUNT,
                count_payload(count, self.client_protocol_version),
            )
            self._ack(device_id, packet_id, STATUS_OK)
        else:
            self._ack(device_id, packet_id, STATUS_UNSUPPORTED)

    def _ack(self, device_id, packet_id, status):
        self.buffer += server_packet(
            PACKET_ACK, struct.pack("<II", packet_id, status), device_id
        )


class FakeSdkServer:
    """An in-memory stand-in for the OpenRGB SDK server on 127.0.0.1:6742.

    `counts` is a timeline of controller counts (the count in effect at a given
    moment), `pushes` a timeline of packets the server sends on its own
    (detection events, device-list updates, controller updates), and
    `protocol_version` the version the server reports - `None` models a server
    without versioning at all (protocol 0).
    """

    def __init__(
        self,
        clock,
        counts=((0.0, 4),),
        pushes=(),
        protocol_version=SDK_PROTOCOL_VERSION,
        listening_at=0.0,
        dies_at=None,
    ):
        self.clock = clock
        self.counts = sorted(counts, key=lambda entry: entry[0])
        self.pushes = sorted(pushes, key=lambda entry: entry[0])
        self.protocol_version = protocol_version
        self.listening_at = listening_at
        self.dies_at = dies_at
        self.connections = 0
        self.requests = []
        self.request_log = []
        self.sockets_closed = []

    def count_at(self, moment):
        count = 0
        for registered_at, value in self.counts:
            if registered_at <= moment:
                count = value
        return count

    def create_connection(self, address, timeout=None):
        self.connections += 1
        if self.clock.now < self.listening_at:
            raise ConnectionRefusedError(f"nothing listening on {address} yet")
        if self.dies_at is not None and self.clock.now >= self.dies_at:
            raise ConnectionRefusedError("OpenRGB is gone")
        return FakeSdkSocket(self, self.clock.now)

    def request_ids(self):
        return request_ids(self.requests)

    def request_times(self):
        return [moment for moment, _request in self.request_log]


def settled_server(clock, count=4, pushes=(), **kwargs):
    """A server that finished detecting long ago: no events, a stable count."""
    return FakeSdkServer(clock, counts=[(0.0, count)], pushes=list(pushes), **kwargs)


def measured_server(clock, counts=None, pushes=None, **kwargs):
    """The measured cold start, replayed exactly as it was logged."""
    default_counts, default_pushes = measured_startup()
    kwargs.setdefault("listening_at", MEASURED_LISTENING_SECONDS)
    return FakeSdkServer(
        clock,
        counts=default_counts if counts is None else counts,
        pushes=default_pushes if pushes is None else pushes,
        **kwargs,
    )


def message_state(line):
    """Which readiness state a published restore-log line belongs to."""
    if line.startswith("WARNING: OpenRGB detection readiness could not be confirmed"):
        return "timeout"
    if line.startswith("OpenRGB SDK ready"):
        return "ready"
    if line.startswith("OpenRGB detection completed"):
        return "complete"
    if line.startswith("OpenRGB detection progress:"):
        return "progress"
    if line.startswith("OpenRGB is detecting controllers"):
        return "detecting"
    if line.startswith("OpenRGB SDK detected "):
        return "detected"
    if line.startswith("OpenRGB SDK controller count changed:"):
        return "count-changed"
    if "does not report detection completion" in line:
        return "unsupported"
    if "connection lost" in line:
        return "reconnecting"
    if "cancelled" in line:
        return "cancelled"
    return "other"


def gate_launched_option(launched):
    """The `launched` option, when the installed gate has it.

    The implementation this regression test was written against could not tell
    an OpenRGB that this restore had just started from one that was already
    running - that distinction is part of the fix. The regression test must fail
    *there* for the real reason (readiness declared during the one-controller
    plateau) instead of with a `TypeError`, so the option is passed only when
    the installed gate supports it.
    """
    try:
        parameters = inspect.signature(app.wait_for_openrgb_ready).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {}
    return {"launched": launched} if "launched" in parameters else {}


class GateHarness:
    """Run the real gate against a scripted SDK server on a fake clock."""

    def __init__(self, server, clock=None, cancelled=None):
        self.clock = clock if clock is not None else server.clock
        self.server = server
        self.cancelled = (lambda: False) if cancelled is None else cancelled
        self.messages = []

    @property
    def lines(self):
        return [line for _moment, line in self.messages]

    def publish(self, line):
        self.messages.append((self.clock.now, line))

    def run(self, launched=True, **options):
        options.setdefault("sleep", self.clock.sleep)
        options.setdefault("elapsed", self.clock.monotonic)
        options.setdefault("cancelled", self.cancelled)
        options.setdefault("publish", self.publish)
        options.update(gate_launched_option(launched))
        with mock.patch.object(
            app.socket, "create_connection", new=self.server.create_connection
        ):
            return app.wait_for_openrgb_ready(**options)

    def time_of(self, state):
        for moment, line in self.messages:
            if message_state(line) == state:
                return moment
        return None

    def states(self):
        return [message_state(line) for line in self.lines]

    def first_state_line(self, state):
        for line in self.lines:
            if message_state(line) == state:
                return line
        return None


# ---------------------------------------------------------
# 1. The regression test: the measured one-controller plateau
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestTheMeasuredPlateauIsNeverReady(unittest.TestCase):
    """The exact cold startup that produced the original false positive.

    One controller from 1.1 s to 11.9 s, four controllers at 11.9 s, detection
    complete at 12.3 s. Readiness must never be declared during the plateau:
    "the count has not changed for a while" cannot mean "OpenRGB finished
    detecting".
    """

    def setUp(self):
        self.clock = FakeClock()

    def test_the_gate_is_not_ready_before_detection_completed(self):
        harness = GateHarness(measured_server(self.clock))

        self.assertTrue(harness.run(launched=True))

        ready_at = harness.time_of("ready")
        self.assertIsNotNone(ready_at, harness.lines)
        self.assertGreaterEqual(
            ready_at,
            MEASURED_DETECTION_COMPLETE_SECONDS,
            f"readiness was declared at {ready_at}s, but detection only "
            f"completed at {MEASURED_DETECTION_COMPLETE_SECONDS}s",
        )

    def test_the_gate_never_accepts_the_one_controller_plateau(self):
        harness = GateHarness(measured_server(self.clock))

        self.assertTrue(harness.run(launched=True))

        early = [
            (moment, line)
            for moment, line in harness.messages
            if message_state(line) == "ready" and moment < MEASURED_DETECTION_COMPLETE_SECONDS
        ]
        self.assertEqual(early, [], "readiness during the plateau is the bug")
        self.assertIsNotNone(harness.time_of("complete"), harness.lines)

    def test_the_scripted_timeline_really_contains_the_plateau(self):
        # Guards the two tests above: if the timeline ever stopped containing a
        # long plateau, they would prove nothing.
        server = measured_server(self.clock)

        self.assertEqual(server.count_at(1.2), 1)
        self.assertEqual(server.count_at(11.5), 1)
        self.assertEqual(server.count_at(12.0), 4)
        self.assertGreater(11.5 - 1.2, 10.0)


# ---------------------------------------------------------
# 2. The protocol pieces: decoding, framing, validation
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestOpenRgbControllerCountPayload(unittest.TestCase):
    """The count reply is parsed with the protocol that was negotiated."""

    def decode(self, count, protocol_version, ids=CONTROLLER_IDS, body=None):
        payload = count_payload(count, protocol_version, ids) if body is None else body
        return app.decode_openrgb_controller_count(payload, protocol_version)

    def test_protocol_zero_carries_the_count_alone(self):
        self.assertEqual(self.decode(4, 0), (4, ()))

    def test_protocol_six_carries_the_count_and_the_controller_ids(self):
        self.assertEqual(self.decode(4, 6), (4, CONTROLLER_IDS))

    def test_zero_controllers_in_protocol_six_is_a_four_byte_body(self):
        self.assertEqual(self.decode(0, 6), (0, ()))

    def test_a_body_that_does_not_match_the_negotiated_protocol_is_rejected(self):
        # A 4-byte body is what a protocol-0 connection gets: reading it as
        # protocol 6 would mean the negotiated version is not being honoured.
        self.assertIsNone(self.decode(4, 6, body=count_payload(4, 0)))
        # And a protocol-6 body read as protocol 0 is just as wrong.
        self.assertIsNone(self.decode(4, 0, body=count_payload(4, 6)))

    def test_a_truncated_or_oversized_id_list_is_rejected(self):
        self.assertIsNone(self.decode(4, 6, body=struct.pack("<I", 4) + b"\x00" * 8))
        self.assertIsNone(self.decode(3, 6, body=struct.pack("<I", 3) + b"\x00" * 16))

    def test_a_short_body_is_rejected(self):
        for body in (b"", b"\x01", b"\x01\x02\x03"):
            self.assertIsNone(self.decode(0, 6, body=body))

    def test_the_count_is_unsigned_32_bit_little_endian(self):
        self.assertEqual(self.decode(3, 6, ids=(1, 2, 3)), (3, (1, 2, 3)))
        self.assertEqual(self.decode(1, 6, ids=(0xFFFFFFFF,)), (1, (0xFFFFFFFF,)))


@unittest.skipIf(app is None, SKIP_REASON)
class TestOpenRgbSdkConnection(unittest.TestCase):
    """One connection against the scripted server: framing, validation, state."""

    def setUp(self):
        self.clock = FakeClock()
        self.connections = []

    def tearDown(self):
        for connection in self.connections:
            connection.close()

    def connect(self, server, timeout=REQUEST_TIMEOUT):
        with mock.patch.object(
            app.socket, "create_connection", new=server.create_connection
        ):
            connection = app.open_openrgb_sdk_connection(timeout=timeout)
        if connection is not None:
            self.connections.append(connection)
        return connection

    def read_until(self, connection, predicate, attempts=12):
        for _ in range(attempts):
            connection.read(REQUEST_TIMEOUT)
            if predicate():
                return True
        return False

    def drain(self, connection, attempts=12):
        """Read until the connection stops being usable (or `attempts` reads)."""
        for _ in range(attempts):
            if not connection.alive:
                return
            connection.read(REQUEST_TIMEOUT)

    def test_a_refused_connection_is_none_and_never_raises(self):
        server = FakeSdkServer(self.clock, listening_at=5.0)

        self.assertIsNone(self.connect(server))

    def test_the_connection_negotiates_protocol_six_and_keeps_it(self):
        server = FakeSdkServer(self.clock, counts=[(0.0, 4)])
        connection = self.connect(server)

        self.assertEqual(connection.protocol_version, SDK_PROTOCOL_VERSION)
        self.assertEqual(connection.server_protocol_version, SDK_PROTOCOL_VERSION)
        self.assertEqual(
            server.request_ids(),
            [PACKET_REQUEST_PROTOCOL_VERSION],
            "negotiation is one version request and nothing else",
        )

    def test_a_server_without_versioning_is_protocol_zero(self):
        server = FakeSdkServer(self.clock, protocol_version=None)
        connection = self.connect(server)

        self.assertEqual(connection.protocol_version, 0)
        self.assertIsNone(connection.server_protocol_version)

    def test_an_older_server_is_negotiated_down_to_its_version(self):
        server = FakeSdkServer(self.clock, protocol_version=4)
        connection = self.connect(server)

        self.assertEqual(connection.protocol_version, 4)

    def test_a_newer_server_is_capped_at_the_version_this_client_speaks(self):
        server = FakeSdkServer(self.clock, protocol_version=99)
        connection = self.connect(server)

        self.assertEqual(connection.protocol_version, SDK_PROTOCOL_VERSION)

    def test_the_count_request_and_reply_are_the_documented_packets(self):
        server = FakeSdkServer(self.clock, counts=[(0.0, 2)])
        connection = self.connect(server)

        connection.request_controller_count()
        self.assertTrue(
            self.read_until(connection, lambda: connection.controller_count is not None)
        )
        self.assertEqual(connection.controller_count, 2)
        sent = [entry for entry in connection.sock.requests]
        magic, device_id, packet_id, body = decode_request(sent[-1])
        self.assertEqual((magic, device_id, packet_id, body), (MAGIC, 0, PACKET_REQUEST_CONTROLLER_COUNT, b""))

    def test_the_id_list_of_a_negotiated_reply_is_parsed(self):
        server = FakeSdkServer(self.clock, counts=[(0.0, 3)])
        connection = self.connect(server)

        connection.request_controller_count()
        self.assertTrue(
            self.read_until(connection, lambda: connection.controller_count is not None)
        )
        self.assertEqual(connection.controller_ids, CONTROLLER_IDS[:3])

    def test_a_protocol_zero_reply_is_read_as_four_bytes(self):
        server = FakeSdkServer(self.clock, counts=[(0.0, 2)], protocol_version=None)
        connection = self.connect(server)

        connection.request_controller_count()
        self.assertTrue(
            self.read_until(connection, lambda: connection.controller_count is not None)
        )
        self.assertEqual((connection.protocol_version, connection.controller_count), (0, 2))

    def test_the_detection_events_are_counted_and_decoded(self):
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[
                (0.1, event_packet(PACKET_DETECTION_STARTED)),
                (0.2, progress_packet(42, "Detecting I2C devices")),
                (0.3, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        connection = self.connect(server)

        self.assertTrue(
            self.read_until(connection, lambda: connection.detection_complete_count > 0)
        )
        self.assertEqual(connection.detection_started_count, 1)
        self.assertEqual(connection.detection_progress_count, 1)
        self.assertEqual(connection.detection_complete_count, 1)
        self.assertEqual(connection.detection_percent, 42)
        self.assertEqual(connection.detection_string, "Detecting I2C devices")

    def test_a_packet_this_client_did_not_ask_for_is_consumed_in_full(self):
        # The server pushes RGBController updates to every protocol-6 client.
        # Ignoring such a packet must not desynchronise the stream: the
        # completion event behind it still has to be seen.
        big = server_packet(PACKET_RGBCONTROLLER_SIGNALUPDATE, b"\x00" * 20000)
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[(0.1, big), (0.2, event_packet(PACKET_DETECTION_COMPLETE))],
        )
        connection = self.connect(server)

        self.assertTrue(
            self.read_until(connection, lambda: connection.detection_complete_count > 0)
        )
        self.assertEqual(connection.detection_complete_count, 1)

    def test_a_wrong_magic_ends_the_stream_instead_of_being_trusted(self):
        server = FakeSdkServer(
            self.clock, counts=[(0.0, 4)], pushes=[(0.1, b"XXXX" + b"\x00" * 20)]
        )
        connection = self.connect(server)

        self.drain(connection)

        self.assertFalse(connection.alive)

    def test_an_impossible_payload_size_ends_the_stream(self):
        # 0xFFFFFFFF bytes of payload cannot come from a working server, and the
        # read must not try to allocate for it.
        header = MAGIC + struct.pack("<III", 0, PACKET_DETECTION_COMPLETE, 0xFFFFFFFF)
        server = FakeSdkServer(self.clock, counts=[(0.0, 4)], pushes=[(0.1, header)])
        connection = self.connect(server)

        self.drain(connection)

        self.assertFalse(connection.alive)

    def test_a_truncated_push_ends_the_stream(self):
        truncated = progress_packet(50)[:-3]
        server = FakeSdkServer(
            self.clock, counts=[(0.0, 4)], pushes=[(0.1, truncated)]
        )
        connection = self.connect(server)

        self.drain(connection)

        self.assertFalse(connection.alive)

    def test_a_count_reply_for_another_device_is_ignored(self):
        server = FakeSdkServer(self.clock, counts=[(0.0, 4)])
        connection = self.connect(server)
        connection.sock.buffer += server_packet(
            PACKET_REQUEST_CONTROLLER_COUNT, count_payload(4, 6), device_id=7
        )

        connection.read(REQUEST_TIMEOUT)

        self.assertIsNone(connection.controller_count)

    def test_a_malformed_progress_event_never_raises(self):
        # A progress event with a nonsense string length is still a detection
        # event; only its text is unusable.
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[
                (0.1, progress_packet(10, "x", string_length=60000)),
                (0.2, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        connection = self.connect(server)

        self.assertTrue(
            self.read_until(connection, lambda: connection.detection_complete_count > 0)
        )
        self.assertEqual(connection.detection_progress_count, 1)

    def test_a_truncated_progress_event_never_raises(self):
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[
                (0.1, server_packet(PACKET_DETECTION_PROGRESS_CHANGED, b"\x01\x02")),
                (0.2, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        connection = self.connect(server)

        self.assertTrue(
            self.read_until(connection, lambda: connection.detection_complete_count > 0)
        )
        self.assertEqual(connection.detection_progress_count, 1)

    def test_closing_is_idempotent_and_closes_the_socket(self):
        server = FakeSdkServer(self.clock)
        connection = self.connect(server)
        sock = connection.sock

        connection.close()
        connection.close()

        self.assertTrue(sock.closed)
        self.assertEqual(len(server.sockets_closed), 1)

    def test_a_read_on_a_closed_connection_is_harmless(self):
        server = FakeSdkServer(self.clock)
        connection = self.connect(server)
        connection.close()

        self.assertIsNone(connection.read(REQUEST_TIMEOUT))
        self.assertFalse(connection.request_controller_count())
        self.assertFalse(connection.alive)


# ---------------------------------------------------------
# 3. The gate: an OpenRGB this restore launched
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestOpenRgbReadinessGateOnALaunchedInstance(unittest.TestCase):
    """OpenRGB started by this restore: only detection complete can accept."""

    def setUp(self):
        self.clock = FakeClock()

    def test_detection_complete_makes_a_launched_instance_ready(self):
        harness = GateHarness(measured_server(self.clock))

        self.assertTrue(harness.run(launched=True))

        self.assertIsNotNone(harness.first_state_line("ready"), harness.lines)
        self.assertEqual(harness.states()[-1], "ready")

    def test_the_detection_started_event_is_reported(self):
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 0), (1.0, 4)],
            pushes=[
                (0.5, event_packet(PACKET_DETECTION_STARTED)),
                (1.0, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True))
        self.assertIsNotNone(harness.first_state_line("detecting"), harness.lines)

    def test_the_detection_progress_event_is_reported_and_never_accepts(self):
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[
                (0.5, progress_packet(30, "Detecting I2C devices")),
                (1.0, progress_packet(60, "Detecting I2C DRAM modules")),
                (1.5, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True))

        progress = [
            line for line in harness.lines if message_state(line) == "progress"
        ]
        self.assertEqual(len(progress), 2, harness.lines)
        self.assertIn("30%", progress[0])
        self.assertIn("Detecting I2C devices", progress[0])
        self.assertIn("60%", progress[1])
        self.assertEqual(harness.states()[-2:], ["complete", "ready"])

    def test_a_progress_burst_is_logged_as_a_bounded_heartbeat(self):
        # One real cold start emitted 991 progress events. Logging each of them
        # is what the throttle exists for: same percentage, many events, one
        # line per interval.
        pushes = [
            (0.2 + 0.1 * index, progress_packet(0, "Detector %d" % index))
            for index in range(60)
        ]
        pushes.append((6.2, event_packet(PACKET_DETECTION_COMPLETE)))
        server = FakeSdkServer(self.clock, counts=[(0.0, 4)], pushes=pushes)
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True, progress_log_seconds=2.0))

        progress = [line for line in harness.lines if message_state(line) == "progress"]
        self.assertLessEqual(len(progress), 4, progress)
        self.assertGreaterEqual(len(progress), 1, progress)

    def test_a_percentage_change_is_always_logged(self):
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[
                (0.2, progress_packet(0, "Detecting I2C devices")),
                (0.3, progress_packet(50, "Detecting I2C DRAM modules")),
                (0.4, progress_packet(100, "Detection completed")),
                (1.0, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True, progress_log_seconds=30.0))

        progress = [line for line in harness.lines if message_state(line) == "progress"]
        self.assertEqual(len(progress), 3, progress)
        self.assertIn("0%", progress[0])
        self.assertIn("50%", progress[1])
        self.assertIn("100%", progress[2])

    def test_a_progress_event_is_progress_only_until_detection_completes(self):
        # The percentage reaching 100 must not accept anything by itself.
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[
                (0.5, progress_packet(100, "Detection completed")),
                (6.0, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True))
        self.assertGreaterEqual(
            harness.time_of("ready"), 6.0 + app.OPENRGB_READINESS_SETTLE_SECONDS
        )

    def test_a_client_connecting_while_detection_runs_still_gets_the_completion(self):
        # Requirement: the eventual DETECTION_COMPLETE reaches a client that
        # connected in the middle of the detection it is waiting for.
        harness = GateHarness(measured_server(self.clock))

        self.assertTrue(harness.run(launched=True))

        self.assertLess(harness.messages[0][0], 6.0, "the wait started inside the plateau")
        self.assertGreaterEqual(
            harness.time_of("ready"), MEASURED_DETECTION_COMPLETE_SECONDS
        )

    def test_the_ready_line_names_the_final_controller_count(self):
        harness = GateHarness(measured_server(self.clock))

        self.assertTrue(harness.run(launched=True))

        self.assertIn("4 controller(s)", harness.first_state_line("ready"))
        self.assertIn("detection completed", harness.first_state_line("ready"))

    def test_the_controller_count_is_refreshed_after_completion(self):
        server = measured_server(self.clock)
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True))

        count_requests = [
            moment
            for moment, request in zip(server.request_times(), server.requests)
            if decode_request(request)[2] == PACKET_REQUEST_CONTROLLER_COUNT
        ]
        self.assertTrue(count_requests)
        # The device list is the *result* of the detection that just finished,
        # so it is asked for once more the moment completion is seen - and the
        # readiness line names that reply, not a pre-completion one.
        self.assertGreaterEqual(
            max(count_requests), harness.time_of("complete")
        )
        self.assertIn("4 controller(s)", harness.first_state_line("ready"))

    def test_the_ready_line_is_published_after_the_settle_delay(self):
        server = settled_server(
            self.clock, count=4, pushes=[(0.0, event_packet(PACKET_DETECTION_COMPLETE))]
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True, settle_seconds=1.5))

        self.assertGreaterEqual(
            harness.time_of("ready") - harness.time_of("complete"), 1.5 - 1e-9
        )

    def test_detection_completing_with_no_controllers_is_still_the_truth(self):
        # The server confirmed completion and genuinely has nothing to show:
        # waiting longer would not produce a controller, and a half-filled list
        # is the thing this gate exists to prevent.
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 0)],
            pushes=[(1.0, event_packet(PACKET_DETECTION_COMPLETE))],
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True))
        self.assertIn("0 controller(s)", harness.first_state_line("ready"))

    def test_an_older_server_cannot_confirm_and_times_out_non_fatally(self):
        # An OpenRGB without detection events (protocol 4) has nothing that can
        # confirm completion, and a count that merely stopped moving cannot.
        server = measured_server(self.clock, protocol_version=4)
        harness = GateHarness(server)

        self.assertFalse(harness.run(launched=True))

        self.assertIsNotNone(harness.first_state_line("unsupported"), harness.lines)
        self.assertEqual(harness.states()[-1], "timeout")

    def test_no_completion_event_at_all_means_no_readiness(self):
        server = measured_server(self.clock, counts=[(0.0, 1)], pushes=[])
        harness = GateHarness(server)

        self.assertFalse(harness.run(launched=True))

        self.assertEqual(harness.states()[-1], "timeout")
        self.assertIsNone(harness.first_state_line("ready"))

    def test_the_timeout_budget_is_the_documented_one(self):
        server = measured_server(self.clock, counts=[(0.0, 1)], pushes=[])
        harness = GateHarness(server)

        self.assertFalse(harness.run(launched=True))

        budget = app.OPENRGB_READINESS_TIMEOUT_SECONDS
        self.assertLessEqual(self.clock.now, budget + app.OPENRGB_READINESS_POLL_SECONDS + REQUEST_TIMEOUT)

    def test_a_disconnect_before_completion_never_becomes_ready(self):
        server = measured_server(self.clock, dies_at=2.0)
        harness = GateHarness(server)

        self.assertFalse(harness.run(launched=True))

        self.assertIsNone(harness.first_state_line("ready"))
        self.assertEqual(harness.states()[-1], "timeout")

    def test_a_lost_connection_is_reported_and_retried(self):
        server = measured_server(self.clock, dies_at=2.0)
        harness = GateHarness(server)

        self.assertFalse(harness.run(launched=True))

        self.assertIsNotNone(harness.first_state_line("reconnecting"), harness.lines)
        self.assertGreater(server.connections, 1, "a lost connection is retried")

    def test_malformed_noise_never_raises_and_the_completion_still_arrives(self):
        server = FakeSdkServer(
            self.clock,
            counts=[(0.0, 4)],
            pushes=[
                (0.2, progress_packet(10, string_length=60000)),
                (0.4, server_packet(PACKET_DEVICE_LIST_UPDATED, b"\x01\x02\x03")),
                (0.6, progress_packet(20)),
                (1.0, event_packet(PACKET_DETECTION_COMPLETE)),
            ],
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True))
        self.assertEqual(harness.states()[-1], "ready")

    def test_poll_noise_is_not_logged(self):
        server = settled_server(
            self.clock, count=4, pushes=[(1.0, event_packet(PACKET_DETECTION_COMPLETE))]
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=True))

        self.assertLessEqual(len(harness.lines), 3, harness.lines)
        self.assertNotIn("other", harness.states(), harness.lines)

    def test_only_read_only_requests_are_ever_sent(self):
        server = measured_server(self.clock)
        harness = GateHarness(server)

        harness.run(launched=True)

        self.assertTrue(server.requests, "the gate must have talked to the server")
        sent = set(server.request_ids())
        self.assertEqual(
            sent,
            {PACKET_REQUEST_PROTOCOL_VERSION, PACKET_REQUEST_CONTROLLER_COUNT},
            f"unexpected SDK requests: {sorted(sent)}",
        )
        self.assertNotIn(PACKET_REQUEST_RESCAN_DEVICES, sent)
        self.assertNotIn(PACKET_REQUEST_CONTROLLER_DATA, sent)
        for request in server.requests:
            self.assertEqual(decode_request(request)[1], 0, "no request may name a device")

    def test_the_connection_is_closed_when_the_gate_returns(self):
        for server in (
            measured_server(FakeClock()),
            settled_server(FakeClock(), count=4),
            FakeSdkServer(FakeClock(), listening_at=99.0),
        ):
            harness = GateHarness(server)
            harness.run(launched=True, timeout=1.0)
            for sock in server.sockets_closed:
                self.assertTrue(sock.closed)
            self.assertTrue(
                all(sock.closed for sock in server.sockets_closed)
                or not server.sockets_closed
            )


# ---------------------------------------------------------
# 4. The gate: an OpenRGB that was already running
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestOpenRgbReadinessGateOnAnAlreadyRunningInstance(unittest.TestCase):
    """A process that predates the restore: its count is credible, events are not."""

    def setUp(self):
        self.clock = FakeClock()

    def test_a_settled_instance_becomes_ready_after_a_quiet_window(self):
        harness = GateHarness(settled_server(self.clock, count=4))

        self.assertTrue(harness.run(launched=False))

        self.assertGreaterEqual(
            harness.time_of("ready"), app.OPENRGB_READINESS_ALREADY_RUNNING_SECONDS
        )
        self.assertIn("unchanged for", harness.first_state_line("ready"))

    def test_the_quiet_window_must_really_elapse(self):
        harness = GateHarness(
            settled_server(self.clock, count=4),
            cancelled=lambda: self.clock.now >= 5.0,
        )

        self.assertFalse(harness.run(launched=False))

        self.assertIsNone(harness.first_state_line("ready"))

    def test_a_detection_event_voids_the_quiet_window_and_the_completion_decides(self):
        # Detection is still running when this client connects, so the count may
        # not be trusted: the completion event is what accepts.
        harness = GateHarness(measured_server(self.clock))

        self.assertTrue(harness.run(launched=False))

        self.assertIsNotNone(harness.time_of("complete"), harness.lines)
        self.assertGreaterEqual(
            harness.time_of("ready"), MEASURED_DETECTION_COMPLETE_SECONDS
        )

    def test_a_changing_count_restarts_the_quiet_window(self):
        server = FakeSdkServer(
            self.clock, counts=[(0.0, 1), (3.0, 2), (6.0, 4)], pushes=[]
        )
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=False, already_running_seconds=8.0))

        self.assertGreaterEqual(
            harness.time_of("ready") - app.OPENRGB_READINESS_SETTLE_SECONDS, 6.0
        )

    def test_a_zero_count_is_never_ready_on_the_count_path(self):
        server = FakeSdkServer(self.clock, counts=[(0.0, 0)], pushes=[])
        harness = GateHarness(server)

        self.assertFalse(harness.run(launched=False))

        self.assertIsNone(harness.first_state_line("ready"))

    def test_an_older_server_cannot_confirm_and_times_out(self):
        server = FakeSdkServer(self.clock, counts=[(0.0, 4)], protocol_version=4)
        harness = GateHarness(server)

        self.assertFalse(harness.run(launched=False))

        self.assertEqual(harness.states()[-1], "timeout")

    def test_the_already_running_path_sends_no_rescan_and_no_rgb_data(self):
        server = settled_server(self.clock, count=4)
        harness = GateHarness(server)

        self.assertTrue(harness.run(launched=False))

        sent = set(server.request_ids())
        self.assertEqual(
            sent, {PACKET_REQUEST_PROTOCOL_VERSION, PACKET_REQUEST_CONTROLLER_COUNT}
        )

    def test_a_connection_made_after_detection_completed_decides_on_launched(self):
        # Protocol 6 has no way to ask for the current detection state, so a
        # client that connects after 103 was sent cannot tell "detection already
        # finished" from "detection is running silently". That is exactly why
        # the two paths are treated differently, and this test pins both halves
        # of that decision on the same server.
        def late_server():
            return FakeSdkServer(
                self.clock,
                counts=[(0.0, 4)],
                pushes=[(1.0, event_packet(PACKET_DETECTION_COMPLETE))],
                listening_at=5.0,
            )

        # Already running when the restore began: its count is credible, so the
        # quiet window accepts and no completion event is needed.
        late = GateHarness(late_server())
        self.assertTrue(late.run(launched=False, already_running_seconds=2.0))
        self.assertIsNone(late.time_of("complete"), late.lines)
        self.assertIn("unchanged for", late.first_state_line("ready"))

        # Started by this restore: the completion event was missed, so nothing
        # may be accepted - not the count, and not its stability.
        started = GateHarness(late_server())
        self.assertFalse(started.run(launched=True))
        self.assertIsNone(started.first_state_line("ready"), started.lines)


# ---------------------------------------------------------
# 5. Cancellation, and the thread wrapper
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestOpenRgbReadinessCancellation(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_cancellation_stops_the_wait_promptly(self):
        harness = GateHarness(measured_server(self.clock))
        state = {"cancelled": False}
        harness.cancelled = lambda: state["cancelled"]
        original = harness.publish

        def cancel_after_one_line(line):
            original(line)
            state["cancelled"] = True

        harness.publish = cancel_after_one_line

        self.assertFalse(harness.run(launched=True))

        self.assertEqual(harness.states()[-1], "cancelled")
        self.assertLess(
            self.clock.now,
            app.OPENRGB_READINESS_POLL_SECONDS + REQUEST_TIMEOUT + 1.0,
            "cancellation must not wait for the readiness budget",
        )

    def test_a_cancellation_during_the_settling_delay_still_stands(self):
        server = settled_server(
            self.clock, count=4, pushes=[(0.0, event_packet(PACKET_DETECTION_COMPLETE))]
        )
        harness = GateHarness(server)
        state = {"cancelled": False}
        harness.cancelled = lambda: state["cancelled"]

        def sleep(seconds):
            if seconds >= app.OPENRGB_READINESS_SETTLE_SECONDS:
                state["cancelled"] = True
            return self.clock.advance(seconds)

        self.assertFalse(harness.run(launched=True, sleep=sleep))

        self.assertIsNone(harness.first_state_line("ready"), harness.lines)

    def test_an_already_cancelled_gate_never_claims_readiness(self):
        harness = GateHarness(measured_server(self.clock), cancelled=lambda: True)

        self.assertFalse(harness.run(launched=True))

        self.assertIsNone(harness.first_state_line("ready"))
        self.assertEqual(harness.states()[-1], "cancelled")


@unittest.skipIf(app is None, SKIP_REASON)
class TestRestoreEngineThreadBindsTheGate(unittest.TestCase):
    """The thread wrapper: its own sleep, its own cancellation, its own log."""

    def make_thread(self, clock, messages):
        return types.SimpleNamespace(
            running=True,
            msleep=lambda milliseconds: clock.advance(milliseconds / 1000.0),
            progress_update=types.SimpleNamespace(emit=messages.append),
        )

    def bound(self, thread, server):
        with mock.patch.object(
            app.socket, "create_connection", new=server.create_connection
        ):
            with mock.patch.object(app.time, "monotonic", new=server.clock.monotonic):
                method = app.RestoreEngineThread.wait_for_openrgb_ready.__get__(thread)
                return method(launched=True)

    def test_the_thread_wrapper_reports_its_own_cancellation_state(self):
        clock = FakeClock()
        messages = []
        thread = self.make_thread(clock, messages)
        thread.running = False

        self.assertFalse(self.bound(thread, measured_server(clock)))

        self.assertIn("cancelled", " ".join(messages).lower())

    def test_the_thread_wrapper_uses_the_thread_s_own_sleep(self):
        # Requirement: the thread's own sleep must be used instead of the
        # module-level `time.sleep`, and the wrapper must not raise if it is.
        clock = FakeClock()
        messages = []
        thread = self.make_thread(clock, messages)

        with mock.patch.object(
            app.time, "sleep", side_effect=AssertionError("time.sleep was used")
        ):
            self.assertTrue(self.bound(thread, measured_server(clock)))

        self.assertTrue(
            any(line.startswith("OpenRGB SDK ready") for line in messages), messages
        )

    def test_the_thread_wrapper_publishes_every_state_change(self):
        clock = FakeClock()
        messages = []
        thread = self.make_thread(clock, messages)

        self.assertTrue(self.bound(thread, measured_server(clock)))

        states = [message_state(line) for line in messages]
        self.assertIn("detected", states)
        self.assertIn("complete", states)
        self.assertEqual(states[-1], "ready")

    def test_the_thread_wrapper_is_cancelled_promptly(self):
        clock = FakeClock()
        messages = []
        thread = self.make_thread(clock, messages)
        server = measured_server(clock)

        with mock.patch.object(
            app.socket, "create_connection", new=server.create_connection
        ):
            with mock.patch.object(app.time, "monotonic", new=clock.monotonic):
                method = app.RestoreEngineThread.wait_for_openrgb_ready.__get__(thread)
                thread.running = False
                self.assertFalse(method(launched=True))

        self.assertLess(clock.now, app.OPENRGB_READINESS_TIMEOUT_SECONDS)


# ---------------------------------------------------------
# 6. The restore sequence around the gate
# ---------------------------------------------------------
class RestoreSequenceHarness:
    """Runs the real `RestoreEngineThread.run()` against the scripted SDK server.

    Every wait goes through the fake clock and every SDK connection to the fake
    server, so the whole sequence - the real readiness gate included - runs
    instantly and touches nothing real.
    """

    ARTEMIS = "Artemis.UI.Windows.exe"
    CONNECTOR = "Yeelight Chroma Connector.exe"

    run = app.RestoreEngineThread.run if app is not None else None
    launch_openrgb = app.RestoreEngineThread.launch_openrgb if app is not None else None
    integration_path_available = (
        app.RestoreEngineThread.integration_path_available if app is not None else None
    )
    # The real sequence reports a service-owned OpenRGB on the already-running
    # path (a read-only diagnostic). The harness records it instead of talking
    # to the real SCM.
    _report_openrgb_service_owned_instance = (
        app.RestoreEngineThread._report_openrgb_service_owned_instance
        if app is not None
        else None
    )
    power_devices = app.RestoreEngineThread.power_devices if app is not None else None

    def __init__(self, config_manager, server, clock, is_dark=True, openrgb_running=False):
        self.config_manager = config_manager
        self.server = server
        self.clock = clock
        self.is_dark = is_dark
        self.openrgb_running = openrgb_running
        self.running = True
        self.messages = []
        self.finished = []
        self.sleeps = []
        self.killed = []
        self.launch_calls = []
        self.gate_calls = []
        self.launched_flags = []
        self.gates_before_launch = []
        self.launch_times = []
        self.progress_update = types.SimpleNamespace(emit=self._publish)
        self.finished_sequence = types.SimpleNamespace(
            emit=lambda: self.finished.append(True)
        )

    # --- the sequence's collaborators -----------------------------------
    def _publish(self, line):
        self.messages.append((self.clock.now, line))

    def wait_for_openrgb_ready(self, launched=False):
        """The real gate, on the fake clock and the scripted transport."""
        self.gate_calls.append(len(self.messages))
        self.launched_flags.append(launched)
        with mock.patch.object(
            app.socket, "create_connection", new=self.server.create_connection
        ):
            return app.wait_for_openrgb_ready(
                sleep=self.clock.sleep,
                elapsed=self.clock.monotonic,
                cancelled=lambda: not self.running,
                publish=self.progress_update.emit,
                **gate_launched_option(launched),
            )

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        return self.clock.advance(seconds)

    def kill_process(self, name):
        self.killed.append(name)

    def _report_openrgb_service_owned_instance(self):
        self.service_reports = getattr(self, "service_reports", 0) + 1

    def is_process_running(self, name):
        if name == "OpenRGB.exe":
            return self.openrgb_running
        return False

    def launch_process(self, path, args=None, hidden=True, cwd=None):
        self.launch_calls.append((path, list(args or []), hidden, cwd))
        self.launch_times.append((self.clock.now, os.path.basename(path)))
        self.gates_before_launch.append(len(self.gate_calls))

    def evaluate_solar_state(self, config):
        return self.is_dark

    def safe_turn_on(self, ip):
        pass

    def safe_turn_off(self, ip):
        pass

    # --- what the tests look at ----------------------------------------
    @property
    def lines(self):
        return [line for _moment, line in self.messages]

    def launched_names(self):
        return [os.path.basename(entry[0]) for entry in self.launch_calls]

    def time_of(self, state):
        for moment, line in self.messages:
            if message_state(line) == state:
                return moment
        return None

    def artemis_launch_times(self):
        return [
            moment for moment, name in self.launch_times if name == self.ARTEMIS
        ]


@unittest.skipIf(app is None, SKIP_REASON)
class TestRestoreSequenceWaitsForOpenRgbReadiness(unittest.TestCase):
    """Artemis must not start before OpenRGB's detection is confirmed complete."""

    ARTEMIS = RestoreSequenceHarness.ARTEMIS
    CONNECTOR = RestoreSequenceHarness.CONNECTOR

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-openrgb-readiness-")
        self.clock = FakeClock()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)

    def make_executable(self, name):
        path = self.path(name)
        with open(path, "wb") as handle:
            handle.write(b"MZ")
        return path

    def write_config(self, openrgb=True, artemis=True, connector=False):
        config = cm.default_config()
        config["location"].update({"latitude": "11.1111", "longitude": "-22.2222"})
        config["lights"]["devices"] = [yeelight_device("Desk Lamp", "192.168.1.50")]
        for key, name, enabled in (
            ("openrgb", "OpenRGB.exe", openrgb),
            ("yeelight_connector", self.CONNECTOR, connector),
            ("artemis", self.ARTEMIS, artemis),
        ):
            config["paths"][key] = self.make_executable(name)
            config["integrations"][key]["enabled"] = enabled
        config["automation"]["wait_for_razer_synapse"] = False
        config["automation"]["launch_razer_synapse"] = False
        config_path = self.path(cm.CONFIG_FILENAME)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
        return cm.ConfigManager(config_path)

    def run_sequence(self, server, openrgb_running=False, elevated=True, **config_kwargs):
        harness = RestoreSequenceHarness(
            self.write_config(**config_kwargs),
            server,
            self.clock,
            is_dark=True,
            openrgb_running=openrgb_running,
        )
        if elevated:
            with mock.patch.object(app, "is_process_elevated", return_value=True):
                harness.run()
        else:
            with mock.patch.object(app, "is_process_elevated", return_value=False):
                with mock.patch.object(
                    app,
                    "openrgb_elevation_status",
                    return_value=wt.OpenRgbElevationStatus(
                        wt.STATUS_NEEDS_SETUP, "Needs setup", ""
                    ),
                ):
                    with mock.patch.object(app, "run_openrgb_task") as run_task:
                        harness.run()
            harness.run_task = run_task
        return harness

    def completed_server(self, **kwargs):
        """A settled server that also reports the completion of its detection.

        The completion is pushed after the sequence's own waits are over, so the
        launched gate really still has a detection to wait for.
        """
        kwargs.setdefault("count", 4)
        return settled_server(
            self.clock,
            pushes=[(SEQUENCE_REACHES_THE_GATE_AT + 1.0, event_packet(PACKET_DETECTION_COMPLETE))],
            **kwargs,
        )

    def test_artemis_launches_only_after_detection_completed(self):
        # The end-to-end regression test: the sequence must not reach Artemis
        # while OpenRGB is still in the ten-second one-controller plateau.
        harness = self.run_sequence(measured_server(self.clock))

        ready_at = harness.time_of("ready")
        self.assertIsNotNone(ready_at, harness.lines)
        self.assertGreaterEqual(ready_at, MEASURED_DETECTION_COMPLETE_SECONDS)
        artemis_at = min(harness.artemis_launch_times())
        self.assertGreaterEqual(
            artemis_at,
            MEASURED_DETECTION_COMPLETE_SECONDS,
            f"Artemis was started at {artemis_at}s, before the controllers existed",
        )

    def test_the_gate_runs_once_between_the_openrgb_launch_and_artemis(self):
        harness = self.run_sequence(self.completed_server())

        self.assertEqual(harness.gate_calls, [5], harness.lines)
        self.assertEqual(
            harness.launched_names(), ["OpenRGB.exe", self.ARTEMIS, self.ARTEMIS]
        )
        for position, entry in enumerate(harness.launch_calls):
            if os.path.basename(entry[0]) == self.ARTEMIS:
                self.assertEqual(harness.gates_before_launch[position], 1)
        self.assertEqual(harness.finished, [True])

    def test_the_sequence_tells_the_gate_that_it_launched_openrgb(self):
        harness = self.run_sequence(self.completed_server())

        self.assertEqual(harness.launched_flags, [True])

    def test_the_sequence_tells_the_gate_that_openrgb_was_already_running(self):
        harness = self.run_sequence(
            self.completed_server(), openrgb_running=True
        )

        self.assertEqual(harness.launched_flags, [False])

    def test_artemis_launches_after_the_readiness_gate_succeeds(self):
        harness = self.run_sequence(self.completed_server())

        self.assertIn(
            "OpenRGB launched. Waiting for OpenRGB to finish detecting controllers...",
            harness.lines,
        )
        self.assertIsNotNone(harness.time_of("ready"), harness.lines)
        self.assertGreaterEqual(min(harness.artemis_launch_times()), harness.time_of("ready"))

    def test_artemis_still_launches_after_a_readiness_timeout(self):
        # A protocol-4 server cannot confirm anything: the gate times out and
        # the restore continues anyway.
        server = measured_server(self.clock, protocol_version=4)
        harness = self.run_sequence(server)

        self.assertEqual(harness.gate_calls, [5], harness.lines)
        self.assertIsNone(harness.time_of("ready"))
        self.assertIn("Restoration sequence successfully completed!", harness.lines)
        self.assertEqual(
            harness.launched_names(), ["OpenRGB.exe", self.ARTEMIS, self.ARTEMIS]
        )
        self.assertEqual(harness.finished, [True])

    def test_an_already_running_openrgb_is_still_gated(self):
        harness = self.run_sequence(self.completed_server(), openrgb_running=True)

        self.assertEqual(
            len(harness.gate_calls), 1, "a running process is not a ready SDK server"
        )
        self.assertIn("OpenRGB is already running.", harness.lines)
        self.assertIn(
            "Waiting for the running OpenRGB to confirm its controller list...",
            harness.lines,
        )
        self.assertNotIn("OpenRGB.exe", harness.launched_names())

    def test_artemis_does_not_wait_for_openrgb_when_that_integration_is_disabled(self):
        harness = self.run_sequence(self.completed_server(), openrgb=False)

        self.assertEqual(harness.gate_calls, [], "no OpenRGB means no readiness gate")
        self.assertIn("OpenRGB integration is disabled. Skipping.", harness.lines)
        self.assertEqual(harness.launched_names(), [self.ARTEMIS, self.ARTEMIS])

    def test_a_skipped_openrgb_never_opens_the_gate(self):
        harness = self.run_sequence(self.completed_server(), elevated=False)

        self.assertEqual(harness.gate_calls, [])
        self.assertIn("Skipping OpenRGB for this restore", " ".join(harness.lines))
        self.assertIn(self.ARTEMIS, harness.launched_names())
        self.assertFalse(harness.run_task.called, "no scheduled task may be started")

    def test_the_fixed_eight_second_wait_is_still_gone(self):
        harness = self.run_sequence(self.completed_server())

        self.assertNotIn(8, harness.sleeps)
        self.assertEqual(harness.sleeps, [5, 3, 3, 3])

    def test_the_artemis_launch_site_is_unchanged(self):
        source = inspect.getsource(app.RestoreEngineThread)

        self.assertEqual(
            2,
            source.count('self.launch_process(artemis_path, ["--minimized"], hidden=False)'),
            "the launch and its single retry must both be there",
        )

    def test_one_openrgb_process_and_no_second_instance(self):
        harness = self.run_sequence(self.completed_server())

        self.assertEqual(
            1,
            len([name for name in harness.launched_names() if name == "OpenRGB.exe"]),
            "the gate must never start another OpenRGB",
        )

    def test_the_gate_never_touches_the_elevation_model(self):
        # Requirement: the readiness client is an ordinary unprivileged
        # localhost SDK client. Nothing in it may launch anything privileged, so
        # the privileged-launch APIs are what is looked for (a comment that
        # mentions the elevation model in prose is not a call).
        for source in (
            inspect.getsource(app.wait_for_openrgb_ready),
            inspect.getsource(app.open_openrgb_sdk_connection),
            inspect.getsource(app.OpenRgbSdkConnection),
            inspect.getsource(app.RestoreEngineThread.wait_for_openrgb_ready),
        ):
            for forbidden in ("runas", "shellexecute", "schtasks", "provision"):
                self.assertNotIn(forbidden, source.lower())

    def test_the_scheduled_task_arguments_are_untouched(self):
        self.assertEqual(
            list(wt.OPENRGB_TASK_ARGS), ["--gui", "--startminimized", "--server"]
        )
        harness = self.run_sequence(self.completed_server())

        args = [
            entry[1]
            for entry in harness.launch_calls
            if os.path.basename(entry[0]) == "OpenRGB.exe"
        ][0]
        self.assertEqual(args, ["--gui", "--startminimized", "--server"])

    def test_no_extra_process_is_started(self):
        harness = self.run_sequence(self.completed_server())

        self.assertEqual(
            harness.launched_names(), ["OpenRGB.exe", self.ARTEMIS, self.ARTEMIS]
        )


if __name__ == "__main__":
    unittest.main()
