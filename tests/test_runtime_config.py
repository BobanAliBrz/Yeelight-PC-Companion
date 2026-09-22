"""Regression tests for the *runtime* configuration paths of the application.

These cover the boundary between `config_manager` and `yeelight_pc_companion`:

* the timing-critical suspend callback reads configuration without full
  validation, so it must never probe integration executable paths on disk,
* a failed runtime reload keeps the last known-good in-memory configuration
  instead of silently falling back to defaults,
* an enabled integration whose "executable" is a directory is not usable,
* the suspend-time Yeelight OFF fan-out is bounded by **one** global network
  budget instead of one socket timeout per configured device.

No window is created and no power event is triggered: the real window/thread
methods are exercised through minimal stub objects. The fan-out additionally runs
against a fake socket layer with a fake clock, so its timing properties are
asserted deterministically rather than by sleeping for real.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_manager as cm  # noqa: E402
import windows_tasks as wt  # noqa: E402
import yeelight_devices as yd  # noqa: E402


def device(name, ip, enabled=True):
    """One configured device entry."""
    return {"id": yd.new_local_device_id(), "name": name, "ip": ip, "enabled": enabled}

try:
    import yeelight_pc_companion as app  # noqa: E402
except Exception as exc:  # pragma: no cover - depends on the test machine
    app = None
    APP_IMPORT_ERROR = exc
else:
    APP_IMPORT_ERROR = None

SKIP_REASON = f"yeelight_pc_companion is not importable here: {APP_IMPORT_ERROR}"


def window_stub(config_manager):
    """A minimal stand-in exposing the real window methods (no GUI)."""

    class WindowStub:
        load_config = app.YeelightPCCompanionWindow.load_config
        _execute_suspend_actions = app.YeelightPCCompanionWindow._execute_suspend_actions

    stub = WindowStub()
    stub.config_manager = config_manager
    stub._last_suspend_exec_time = 0.0
    stub._sleep_transition_active = False
    stub._system_sleeping = False
    stub.restore_thread = None
    stub.turned_off = []
    return stub


def record_off_targets(stub):
    """Patch the suspend OFF fan-out so a test can see the addresses it got.

    The suspend sequence must reach the devices through the *batched* sender, so
    the stub records the one batch it was handed instead of one call per device.
    """

    def record(device_ips, **kwargs):
        stub.turned_off.extend(device_ips)
        return {}

    return mock.patch.object(app, "fire_and_forget_off_devices", side_effect=record)


def restore_stub():
    """A minimal stand-in for the restore sequencer thread (no QThread)."""

    class RestoreStub:
        integration_path_available = app.RestoreEngineThread.integration_path_available
        launch_process = app.RestoreEngineThread.launch_process

    stub = RestoreStub()
    stub.running = True
    stub.messages = []
    stub.progress_update = types.SimpleNamespace(emit=stub.messages.append)
    return stub


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-runtime-test-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)

    def write_json(self, path, data):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)
        return path


class TestSuspendCallbackConfigurationRead(TempDirTestCase):
    """The <1.5 s suspend path must not run validation against the filesystem."""

    def enabled_integration_config(self):
        config = cm.default_config()
        for key in cm.INTEGRATION_KEYS:
            config["paths"][key] = self.path("Program Files", key, "tool.exe")
            config["integrations"][key]["enabled"] = True
        config["lights"]["devices"] = [device("Desk Lamp", "192.168.1.50")]
        return config

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_suspend_actions_never_probe_executable_paths(self):
        config = self.enabled_integration_config()
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))

        stub = window_stub(manager)
        stub.config = config

        # Record *every* filesystem probe instead of asserting nothing raises:
        # the suspend callback swallows read errors on purpose, so a probe would
        # only be observable by its side effect on this list.
        probed = []
        real_exists, real_isfile = os.path.exists, os.path.isfile

        def recording(real):
            def probe(candidate, *args, **kwargs):
                probed.append(os.fspath(candidate))
                return real(candidate, *args, **kwargs)

            return probe

        with mock.patch.object(cm.os.path, "isfile", side_effect=recording(real_isfile)), mock.patch.object(
            cm.os.path, "exists", side_effect=recording(real_exists)
        ), mock.patch.object(app, "terminate_processes_win32", return_value=1), mock.patch.object(
            app.time, "sleep"
        ), record_off_targets(stub):
            stub._execute_suspend_actions()

        self.assertEqual([], probed, "the suspend callback performed filesystem probes")
        for key in cm.INTEGRATION_KEYS:
            self.assertNotIn(config["paths"][key], probed)
        self.assertEqual(["192.168.1.50"], stub.turned_off)
        self.assertTrue(stub._system_sleeping)
        self.assertTrue(stub._sleep_transition_active)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_suspend_actions_fall_back_to_the_in_memory_config(self):
        stub = window_stub(cm.ConfigManager(self.path("missing", cm.CONFIG_FILENAME)))
        stub.config = cm.default_config()
        stub.config["lights"]["devices"] = [device("Desk Lamp", "10.0.0.9")]

        with mock.patch.object(app, "terminate_processes_win32", return_value=0), mock.patch.object(
            app.time, "sleep"
        ), record_off_targets(stub):
            stub._execute_suspend_actions()

        # The on-disk read fails, so the already-loaded config is used.
        self.assertEqual(["10.0.0.9"], stub.turned_off)


    @unittest.skipIf(app is None, SKIP_REASON)
    def test_suspend_targets_every_enabled_device(self):
        config = cm.default_config()
        config["lights"]["devices"] = [
            device("Desk Lamp", "192.168.1.50"),
            device("Lightstrip", "192.168.1.51"),
            device("Living Room", "192.168.1.52", enabled=False),
            device("Study", "192.168.1.53"),
        ]
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))

        stub = window_stub(manager)
        stub.config = config
        with mock.patch.object(app, "terminate_processes_win32", return_value=1), mock.patch.object(
            app.time, "sleep"
        ), record_off_targets(stub):
            stub._execute_suspend_actions()

        self.assertEqual(["192.168.1.50", "192.168.1.51", "192.168.1.53"], stub.turned_off)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_suspend_with_no_devices_still_completes(self):
        config = cm.default_config()
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))

        stub = window_stub(manager)
        stub.config = config
        with mock.patch.object(app, "terminate_processes_win32", return_value=1), mock.patch.object(
            app.time, "sleep"
        ), record_off_targets(stub):
            stub._execute_suspend_actions()

        self.assertEqual([], stub.turned_off)
        self.assertTrue(stub._system_sleeping)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_suspend_never_runs_discovery(self):
        """Discovery is user-triggered only: it must never appear in this path."""
        config = cm.default_config()
        config["lights"]["devices"] = [device("Desk Lamp", "192.168.1.50")]
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))

        stub = window_stub(manager)
        stub.config = config

        def explode(*args, **kwargs):
            raise AssertionError("the suspend callback ran a network discovery")

        with mock.patch.object(yd, "discover_devices", side_effect=explode), mock.patch.object(
            app, "terminate_processes_win32", return_value=1
        ), mock.patch.object(app.time, "sleep"), record_off_targets(stub):
            stub._execute_suspend_actions()

        self.assertEqual(["192.168.1.50"], stub.turned_off)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_the_connector_dies_before_the_fan_out_and_the_controllers_after_it(self):
        """The suspend order is a contract: Connector, wait, fan-out, controllers.

        The task-aware OpenRGB stop is deliberately stubbed here: this test
        pins the *relative order* of connector / release-wait / fan-out /
        controller terminate. On a machine where an OpenRGB.exe process is
        actually present (the real-world case that motivated the service
        work), `end_openrgb_task()` legitimately polls inside its 0.4 s budget
        and those verification sleeps are recorded by the shared `time.sleep`
        patch. That behaviour is covered by `test_runtime_config`'s budget
        tests and `test_startup_and_uninstall`'s deadline tests; it is not
        what this ordering assertion is about.
        """
        config = self.enabled_integration_config()
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))

        stub = window_stub(manager)
        stub.config = config
        events = []

        def record_terminate(*names):
            events.append(("kill", tuple(names)))
            return 1

        def record_sleep(seconds):
            events.append(("sleep", seconds))

        def record_off(device_ips, **kwargs):
            events.append(("off", tuple(device_ips)))

        with mock.patch.object(app, "terminate_processes_win32", side_effect=record_terminate), mock.patch.object(
            app.time, "sleep", side_effect=record_sleep
        ), mock.patch.object(app, "fire_and_forget_off_devices", side_effect=record_off), mock.patch.object(
            app, "end_openrgb_task", return_value="not_running"
        ):
            stub._execute_suspend_actions()

        self.assertEqual(["kill", "sleep", "off", "kill"], [event[0] for event in events])
        # The Connector has to release Yeelight music mode before the bulbs can
        # accept commands, so it dies first and its 0.35 s release wait is kept.
        self.assertEqual(("Yeelight Chroma Connector.exe",), events[0][1])
        self.assertEqual(0.35, events[1][1])
        self.assertIn("Yeelight Chroma Connector.exe", cm.INTEGRATIONS["yeelight_connector"]["process"])
        # ... and the fan-out happens in between, not after the controllers.
        self.assertEqual(("192.168.1.50",), events[2][1])
        self.assertEqual(
            (
                cm.INTEGRATIONS["artemis"]["process"],
                cm.INTEGRATIONS["openrgb"]["process"],
            ),
            events[3][1],
        )

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_the_suspend_fan_out_gets_every_enabled_device_in_one_batch(self):
        """The real batched sender runs inside the real suspend sequence."""
        config = cm.default_config()
        config["lights"]["devices"] = [
            device("Desk Lamp", "192.168.1.50"),
            device("Lightstrip", "192.168.1.51"),
            device("Living Room", "192.168.1.52", enabled=False),
        ]
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))

        stub = window_stub(manager)
        stub.config = config
        network = FakeSuspendNetwork({"192.168.1.50": "accepts", "192.168.1.51": "accepts"})

        def explode(*args, **kwargs):
            raise AssertionError("the suspend fan-out resolved a name")

        with network, mock.patch.object(app, "terminate_processes_win32", return_value=1), mock.patch.object(
            app.time, "sleep"
        ), mock.patch.object(app.socket, "getaddrinfo", side_effect=explode):
            stub._execute_suspend_actions()

        # Only the enabled devices, each addressed exactly once, in one batch.
        self.assertEqual(["192.168.1.50", "192.168.1.51"], [sock.address for sock in network.sockets])
        self.assertTrue(network.all_closed)
        for sock in network.sockets:
            self.assertEqual(app.SUSPEND_YEELIGHT_OFF_PAYLOAD, sock.sent)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_the_suspend_path_needs_no_yeelight_library(self):
        """No `yeelight.Bulb` is constructed: the command is raw TCP."""
        config = cm.default_config()
        config["lights"]["devices"] = [device("Desk Lamp", "192.168.1.50")]
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))

        stub = window_stub(manager)
        stub.config = config
        network = FakeSuspendNetwork({"192.168.1.50": "accepts"})

        with network, mock.patch.object(app, "terminate_processes_win32", return_value=1), mock.patch.object(
            app.time, "sleep"
        ), mock.patch.dict(sys.modules, {"yeelight": None}):
            stub._execute_suspend_actions()

        # The raw payload still went out, with the Yeelight library unimportable.
        self.assertEqual(
            [app.SUSPEND_YEELIGHT_OFF_PAYLOAD], [sock.sent for sock in network.sockets]
        )


class FakeSuspendSocket:
    """One socket the suspend fan-out opens, with a scripted outcome.

    Behaviours (per device address):

    ``accepts``      the connection completes and the whole payload is accepted
    ``partial``      connected, but the kernel takes the payload in small pieces
    ``refused``      the connection attempt fails once the socket is writable
    ``unreachable``  the connection attempt never completes
    ``send-fails``   connected, then the send raises
    """

    def __init__(self, family, kind, network):
        self.family = family
        self.kind = kind
        self.network = network
        self.address = None
        self.connected_to = None
        self.closed = False
        self.sent = b""

    @property
    def behaviour(self):
        return self.network.behaviour_for(self.address)

    @property
    def selectable(self):
        """Would `select()` report this socket writable?"""
        return self.behaviour != "unreachable"

    def setblocking(self, flag):
        self.non_blocking = not flag

    def connect_ex(self, address):
        self.address, self.connected_to = address[0], address
        return 0 if self.behaviour == "accepts" else 10035

    def getsockopt(self, level, option):
        return 10061 if self.behaviour == "refused" else 0

    def send(self, payload):
        if self.behaviour == "send-fails":
            raise OSError("connection reset by peer")
        if self.behaviour == "partial":
            piece = payload[:10]
            self.sent += piece
            return len(piece)
        self.sent += payload
        return len(payload)

    def close(self):
        self.closed = True


class FakeSuspendNetwork:
    """Fake sockets, fake `select()` and a fake clock for one fan-out run.

    Installed as a context manager, so the real socket machinery is replaced for
    exactly one call. The clock only moves when the fan-out *waits*: `select()`
    returns ready sockets immediately and costs its timeout otherwise, which is
    what makes the timing assertions deterministic and independent of the machine.
    """

    def __init__(self, behaviours=None, default="unreachable", start=1000.0):
        self.behaviours = dict(behaviours or {})
        self.default_behaviour = default
        self.sockets = []
        self.waits = []
        self.outcomes = {}
        self.now = start
        self._start = start
        self._patches = []

    # -- the fake socket layer ---------------------------------------------
    def behaviour_for(self, address):
        return self.behaviours.get(address, self.default_behaviour)

    def socket_factory(self, family, kind):
        sock = FakeSuspendSocket(family, kind, self)
        self.sockets.append(sock)
        return sock

    def select(self, rlist, wlist, xlist, timeout=0):
        self.waits.append(timeout)
        writable = [sock for sock in wlist if sock.selectable]
        if not writable:
            self.now += timeout
        return [], writable, []

    def monotonic(self):
        return self.now

    def __enter__(self):
        self._patches = [
            mock.patch.object(app.socket, "socket", new=self.socket_factory),
            mock.patch.object(app.select, "select", new=self.select),
            mock.patch.object(app.time, "monotonic", new=self.monotonic),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for patch in reversed(self._patches):
            patch.stop()
        self._patches = []
        return False

    # -- what the fake layer observed --------------------------------------
    @property
    def elapsed(self):
        """How much time the fan-out spent waiting, according to the fake clock."""
        return self.now - self._start

    @property
    def all_closed(self):
        return all(sock.closed for sock in self.sockets)


class TestSuspendSequenceStaysInsideItsBudget(TempDirTestCase):
    """`_execute_suspend_actions()` must fit the ~1.5 s freeze contract.

    The OpenRGB stop is the phase that regressed: it used to run a 30 s
    `schtasks /query`, then a 5 s `/end`, then a 2 s poll. These tests run the
    **real** suspend sequence with a real (but clock-controlled) task stop and a
    real (but network-free) OFF fan-out, and fail loudly on any unbounded wait
    the sequence may grow back.
    """

    def openrgb_config(self):
        config = cm.default_config()
        config["paths"]["openrgb"] = self.path("Program Files", "OpenRGB", "OpenRGB.exe")
        config["integrations"]["openrgb"]["enabled"] = True
        config["lights"]["devices"] = [device("Desk Lamp", "192.168.1.50")]
        return config

    def connector_config(self):
        """OpenRGB *and* the Connector enabled: the full suspend sequence."""
        config = self.openrgb_config()
        config["paths"]["yeelight_connector"] = self.path(
            "Program Files", "Connector", "Yeelight Chroma Connector.exe"
        )
        config["integrations"]["yeelight_connector"]["enabled"] = True
        return config

    def suspend_with_openrgb(self, schtasks_result, pids_script, config=None):
        """Run the real suspend sequence; return (stub, sleep_seconds, clock)."""
        config = config or self.openrgb_config()
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))
        stub = window_stub(manager)
        stub.config = config

        clock = {"now": 5000.0}
        sleeps = []
        remaining = list(pids_script)

        def fake_sleep(seconds):
            # No real sleeping: the suspend path's waits are recorded, not taken.
            sleeps.append(seconds)
            if seconds > 0:
                clock["now"] += float(seconds)

        def fake_pids():
            if remaining:
                value = remaining.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value
            return []

        def fake_schtasks(args, timeout=None):
            if isinstance(schtasks_result, Exception):
                raise schtasks_result
            return schtasks_result

        with mock.patch.object(app, "terminate_processes_win32", return_value=1), mock.patch.object(
            app.time, "sleep", side_effect=fake_sleep
        ), mock.patch.object(app, "fire_and_forget_off_devices", return_value={}), mock.patch.object(
            wt, "_end_task_monotonic", lambda: clock["now"]
        ), mock.patch.object(
            wt, "_end_task_sleep", fake_sleep
        ), mock.patch.object(
            wt, "_openrgb_process_ids", fake_pids
        ), mock.patch.object(
            wt, "_run_schtasks", fake_schtasks
        ):
            stub._execute_suspend_actions()

        return stub, sleeps, clock

    def test_the_budgeted_phases_fit_the_target_with_headroom(self):
        """The declared phase budgets are what keeps the sequence inside 1.5 s."""
        self.assertLess(
            app.suspend_budgeted_seconds(),
            app.SUSPEND_SEQUENCE_TARGET_SECONDS,
            "the budgeted suspend phases no longer leave headroom",
        )
        self.assertLessEqual(app.SUSPEND_NATIVE_TERMINATE_SECONDS, 1.0)
        self.assertEqual(0.35, app.SUSPEND_CONNECTOR_RELEASE_SECONDS)
        self.assertEqual(0.35, app.SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS)
        self.assertLessEqual(app.END_TASK_STOP_BUDGET_SECONDS, 0.5)
        # The task stop is a fraction of the sequence, never a query plus a
        # timeout plus a poll.
        self.assertLess(app.END_TASK_STOP_BUDGET_SECONDS, app.SUSPEND_SEQUENCE_TARGET_SECONDS / 3)

    def test_the_suspend_sequence_never_sleeps_beyond_its_declared_budgets(self):
        """No step may wait longer than the phase budget it belongs to."""
        _stub, sleeps, clock = self.suspend_with_openrgb(
            (0, "SUCCESS", ""), [[111], []], config=self.connector_config()
        )

        # The whole explicit wait budget is the Connector release plus the task
        # stop's own single verification interval — never a per-step poll.
        self.assertEqual(
            [app.SUSPEND_CONNECTOR_RELEASE_SECONDS, wt.END_TASK_VERIFY_INTERVAL_SECONDS],
            sleeps,
        )
        self.assertLess(sum(sleeps), app.SUSPEND_SEQUENCE_TARGET_SECONDS)
        self.assertAlmostEqual(clock["now"] - 5000.0, sum(sleeps))

    def test_openrgb_enabled_reaches_every_later_step(self):
        """OpenRGB is stopped first, then the native terminate still runs."""
        config = self.connector_config()
        manager = cm.ConfigManager(self.write_json(self.path(cm.CONFIG_FILENAME), config))
        stub = window_stub(manager)
        stub.config = config
        events = []

        clock = {"now": 7000.0}
        pids = [[111], []]

        def fake_pids():
            return pids.pop(0) if pids else []

        def fake_sleep(seconds):
            if seconds > 0:
                clock["now"] += float(seconds)

        def record_terminate(*names):
            events.append(("kill", tuple(names)))
            return 1

        def record_off(device_ips, **kwargs):
            events.append(("off", tuple(device_ips)))
            return {}

        with mock.patch.object(app, "terminate_processes_win32", side_effect=record_terminate), mock.patch.object(
            app.time, "sleep", side_effect=fake_sleep
        ), mock.patch.object(app, "fire_and_forget_off_devices", side_effect=record_off), mock.patch.object(
            wt, "_end_task_monotonic", lambda: clock["now"]
        ), mock.patch.object(
            wt, "_end_task_sleep", fake_sleep
        ), mock.patch.object(
            wt, "_openrgb_process_ids", fake_pids
        ), mock.patch.object(
            wt, "_run_schtasks", lambda *a, **k: (0, "SUCCESS", "")
        ):
            with self.assertLogs(level="INFO") as captured:
                stub._execute_suspend_actions()

        # The Connector die -> release wait -> OFF fan-out -> (task stop) ->
        # controller terminate order is preserved.
        self.assertEqual(["kill", "off", "kill"], [event[0] for event in events])
        self.assertEqual(("192.168.1.50",), events[1][1])
        self.assertIn("Yeelight Chroma Connector.exe", events[0][1][0])

        text = "\n".join(captured.output)
        self.assertIn("OpenRGB elevation-task stop: stopped", text)
        self.assertIn("Suspend sequence completed successfully", text)

        # The task stop ran, and it never went near a task-existence query.
        self.assertNotIn("[OPENRGB] No elevation task to stop.", text)

    def test_a_deadline_expired_openrgb_stop_does_not_stop_the_sequence(self):
        """A failed task stop is reported and the sequence still completes."""
        import subprocess

        def hang(args, timeout=None):
            raise subprocess.TimeoutExpired(cmd="schtasks.exe", timeout=timeout or 0.0)

        stub, _sleeps, clock = self.suspend_with_openrgb(hang, [[111]] * 100)

        self.assertAlmostEqual(
            clock["now"] - 5000.0,
            0.0,
            places=6,
            msg="the sequence waited longer than the Connector release",
        )
        self.assertTrue(stub._system_sleeping)
        self.assertTrue(stub._sleep_transition_active)

    def test_a_stop_that_never_takes_effect_cannot_stretch_the_sequence(self):
        """`/end` accepted but the process survives: still inside the budget."""
        stub, sleeps, clock = self.suspend_with_openrgb((0, "SUCCESS", ""), [[111]] * 500)

        # Only the task stop's own verification interval is spent — never a
        # multi-second poll — and the Connector release does not even run here.
        self.assertLessEqual(sum(sleeps), wt.END_TASK_STOP_BUDGET_SECONDS)
        self.assertLess(clock["now"] - 5000.0, app.SUSPEND_SEQUENCE_TARGET_SECONDS)
        self.assertTrue(stub._system_sleeping)


@unittest.skipIf(app is None, SKIP_REASON)
class TestSuspendOffFanOut(unittest.TestCase):
    """Sending OFF to N devices must not cost N socket timeouts.

    The old per-device loop blocked up to 0.25 s per device inside
    `socket.create_connection()`: fine for exactly two fixed devices, fatal for
    an arbitrary device list, because 8 unreachable bulbs already spent ~2 s
    before the rest of the suspend sequence was even considered. These tests pin
    the property that replaced it: **one** global deadline for the whole batch.
    """

    def fan_out(self, addresses, behaviours=None, **kwargs):
        network = FakeSuspendNetwork(behaviours, **kwargs)
        with network:
            network.outcomes = app.fire_and_forget_off_devices(addresses)
        return network

    def budget(self):
        return app.SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS

    def test_a_single_reachable_device_receives_the_raw_off_command(self):
        network = self.fan_out(["192.168.1.50"], {"192.168.1.50": "accepts"})

        self.assertEqual({"192.168.1.50": app.SUSPEND_FANOUT_SENT}, network.outcomes)
        self.assertEqual(1, len(network.sockets))
        self.assertEqual(("192.168.1.50", app.SUSPEND_YEELIGHT_PORT), network.sockets[0].connected_to)
        self.assertEqual(app.SUSPEND_YEELIGHT_OFF_PAYLOAD, network.sockets[0].sent)
        self.assertEqual(
            b'{"id":1,"method":"set_power","params":["off","sudden",0]}\r\n',
            network.sockets[0].sent,
        )
        self.assertTrue(network.all_closed)

    def test_a_partially_sent_payload_is_completed_before_the_deadline(self):
        network = self.fan_out(["192.168.1.50"], {"192.168.1.50": "partial"})

        self.assertEqual({"192.168.1.50": app.SUSPEND_FANOUT_SENT}, network.outcomes)
        self.assertEqual(app.SUSPEND_YEELIGHT_OFF_PAYLOAD, network.sockets[0].sent)

    def test_many_reachable_devices_are_all_attempted(self):
        addresses = [f"192.168.1.{number}" for number in range(20, 30)]
        network = self.fan_out(addresses, {address: "accepts" for address in addresses})

        self.assertEqual(10, len(network.sockets))
        self.assertEqual(
            {address: app.SUSPEND_FANOUT_SENT for address in addresses}, network.outcomes
        )
        # Everything connected at once: no waiting was needed at all.
        self.assertEqual(0.0, network.elapsed)

    def test_the_wait_does_not_grow_with_the_number_of_unreachable_devices(self):
        """1, 2 and 20 dead devices cost exactly the same single budget."""
        elapsed = {}
        for count in (1, 2, 20):
            addresses = [f"192.168.1.{number}" for number in range(20, 20 + count)]
            with self.subTest(count=count):
                network = self.fan_out(addresses)

                self.assertEqual(count, len(network.sockets))
                self.assertEqual(
                    [app.SUSPEND_FANOUT_TIMED_OUT] * count, list(network.outcomes.values())
                )
                self.assertEqual(1, len(network.waits), "the batch waited more than once")
                elapsed[count] = round(network.elapsed, 6)
                self.assertTrue(network.all_closed)

        self.assertEqual(
            {round(self.budget(), 6)},
            set(elapsed.values()),
            "the fan-out no longer costs one fixed budget regardless of device count",
        )
        # The regression this replaces: 20 devices x 0.25 s per socket timeout.
        self.assertLess(elapsed[20], 20 * 0.25)

    def test_the_batch_uses_one_explicit_global_network_deadline(self):
        timeout = self.budget()
        self.assertGreaterEqual(timeout, 0.30)
        self.assertLessEqual(timeout, 0.40)
        # Safely below the documented ~1.5 s target for the whole suspend path.
        self.assertLess(timeout, 1.5)

        network = self.fan_out(["192.168.1.50", "192.168.1.51", "2001:db8::5"])

        self.assertEqual(1, len(network.waits))
        self.assertAlmostEqual(timeout, network.waits[0], places=6)
        self.assertAlmostEqual(timeout, network.elapsed, places=6)

    def test_a_dead_device_does_not_prevent_the_others(self):
        network = self.fan_out(
            ["192.168.1.50", "192.168.1.51", "192.168.1.52", "192.168.1.53"],
            {
                "192.168.1.50": "unreachable",
                "192.168.1.51": "accepts",
                "192.168.1.52": "refused",
                "192.168.1.53": "send-fails",
            },
        )

        self.assertEqual(
            {
                "192.168.1.50": app.SUSPEND_FANOUT_TIMED_OUT,
                "192.168.1.51": app.SUSPEND_FANOUT_SENT,
                "192.168.1.52": app.SUSPEND_FANOUT_FAILED,
                "192.168.1.53": app.SUSPEND_FANOUT_FAILED,
            },
            network.outcomes,
        )
        sent = [sock for sock in network.sockets if sock.address == "192.168.1.51"]
        self.assertEqual(app.SUSPEND_YEELIGHT_OFF_PAYLOAD, sent[0].sent)
        self.assertTrue(network.all_closed)

    def test_every_socket_is_closed_on_every_path(self):
        network = self.fan_out(
            ["192.168.1.50", "192.168.1.51", "192.168.1.52", "192.168.1.53"],
            {
                "192.168.1.50": "unreachable",
                "192.168.1.51": "accepts",
                "192.168.1.52": "refused",
                "192.168.1.53": "send-fails",
            },
        )

        self.assertEqual(4, len(network.sockets))
        self.assertTrue(network.all_closed, "a socket outlived the fan-out")

    def test_a_socket_that_cannot_be_created_is_reported_not_fatal(self):
        def refuse(family, kind):
            raise OSError(10024, "no more file descriptors")

        with mock.patch.object(app.socket, "socket", side_effect=refuse), mock.patch.object(
            app.time, "monotonic", return_value=1000.0
        ), mock.patch.object(app.select, "select", return_value=([], [], [])):
            outcomes = app.fire_and_forget_off_devices(["192.168.1.50"])

        self.assertEqual({"192.168.1.50": app.SUSPEND_FANOUT_FAILED}, outcomes)

    def test_zero_devices_returns_immediately(self):
        for addresses in ([], None, ["", "   "]):
            with self.subTest(addresses=addresses):
                network = self.fan_out(addresses)

                self.assertEqual({}, network.outcomes)
                self.assertEqual([], network.sockets)
                self.assertEqual([], network.waits)
                self.assertEqual(0.0, network.elapsed)

    def test_addresses_that_are_not_ip_literals_are_skipped(self):
        network = self.fan_out(["not-an-ip", "", "192.168.1.50"], {"192.168.1.50": "accepts"})

        self.assertEqual(["192.168.1.50"], [sock.address for sock in network.sockets])
        self.assertEqual({"192.168.1.50": app.SUSPEND_FANOUT_SENT}, network.outcomes)

    def test_a_repeated_address_is_addressed_once(self):
        network = self.fan_out(
            ["192.168.1.50", "192.168.1.50", " 192.168.1.50 "], default="accepts"
        )

        self.assertEqual(1, len(network.sockets))
        self.assertEqual({"192.168.1.50": app.SUSPEND_FANOUT_SENT}, network.outcomes)

    def test_ipv4_and_ipv6_literals_choose_their_own_address_family(self):
        network = self.fan_out(["192.168.1.50", "2001:0db8:0000::5"], default="accepts")

        self.assertEqual([socket.AF_INET, socket.AF_INET6], [sock.family for sock in network.sockets])
        # The canonical form is what gets used, without ever resolving a name.
        self.assertEqual(
            [("192.168.1.50", app.SUSPEND_YEELIGHT_PORT), ("2001:db8::5", app.SUSPEND_YEELIGHT_PORT)],
            [sock.connected_to for sock in network.sockets],
        )

    def test_the_fan_out_never_resolves_a_name_or_uses_the_yeelight_library(self):
        def explode(*args, **kwargs):
            raise AssertionError("the suspend fan-out resolved a name")

        network = FakeSuspendNetwork({"192.168.1.50": "accepts", "192.168.1.51": "accepts"})
        with network, mock.patch.object(app.socket, "getaddrinfo", side_effect=explode), mock.patch.object(
            app.socket, "gethostbyname", side_effect=explode
        ), mock.patch.object(app.socket, "create_connection", side_effect=explode):
            network.outcomes = app.fire_and_forget_off_devices(["192.168.1.50", "192.168.1.51"])

        self.assertEqual(
            {
                "192.168.1.50": app.SUSPEND_FANOUT_SENT,
                "192.168.1.51": app.SUSPEND_FANOUT_SENT,
            },
            network.outcomes,
        )

    def test_the_single_device_helper_uses_the_same_bounded_path(self):
        class SuspendStub:
            fire_and_forget_off = app.YeelightPCCompanionWindow.fire_and_forget_off

        network = FakeSuspendNetwork({"192.168.1.50": "accepts"})
        with network:
            outcomes = SuspendStub().fire_and_forget_off("192.168.1.50")

        self.assertEqual({"192.168.1.50": app.SUSPEND_FANOUT_SENT}, outcomes)
        self.assertEqual(app.SUSPEND_YEELIGHT_OFF_PAYLOAD, network.sockets[0].sent)

    def test_an_empty_address_opens_no_socket(self):
        class SuspendStub:
            fire_and_forget_off = app.YeelightPCCompanionWindow.fire_and_forget_off

        network = FakeSuspendNetwork()
        with network:
            outcomes = SuspendStub().fire_and_forget_off("")

        self.assertEqual({}, outcomes)
        self.assertEqual([], network.sockets)


class TestRuntimeReloadKeepsLastKnownGoodConfig(TempDirTestCase):
    """A failed reload must never replace a working in-memory configuration."""

    def working_config(self):
        executable = self.write_json(self.path("OpenRGB.exe"), {})
        config = cm.default_config()
        config["location"]["latitude"] = "11.1111"
        config["location"]["longitude"] = "-22.2222"
        config["lights"]["devices"] = [device("Desk Lamp", "192.168.1.50")]
        config["paths"]["openrgb"] = executable
        config["integrations"]["openrgb"]["enabled"] = True
        return config

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_first_load_failure_falls_back_to_defaults(self):
        stub = window_stub(cm.ConfigManager(self.path(cm.CONFIG_FILENAME)))

        self.assertFalse(hasattr(stub, "config"))
        self.assertFalse(stub.load_config())
        self.assertEqual(cm.default_config(), stub.config)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_reload_failure_keeps_the_previous_config(self):
        manager = cm.ConfigManager(self.path(cm.CONFIG_FILENAME))
        good = self.working_config()
        manager.save(good)

        stub = window_stub(manager)
        self.assertTrue(stub.load_config())
        self.assertEqual("11.1111", stub.config["location"]["latitude"])

        for broken in ('{ "config_version": 1,', json.dumps({"config_version": 1, "location": "oops"})):
            with self.subTest(broken=broken):
                with open(manager.config_path, "w", encoding="utf-8") as handle:
                    handle.write(broken)

                self.assertFalse(stub.load_config())

                self.assertEqual("11.1111", stub.config["location"]["latitude"])
                self.assertEqual(["192.168.1.50"], cm.enabled_device_ips(stub.config))
                self.assertTrue(stub.config["integrations"]["openrgb"]["enabled"])
                self.assertEqual(self.path("OpenRGB.exe"), stub.config["paths"]["openrgb"])

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_a_successful_reload_still_replaces_the_config(self):
        manager = cm.ConfigManager(self.path(cm.CONFIG_FILENAME))
        manager.save(self.working_config())

        stub = window_stub(manager)
        self.assertTrue(stub.load_config())

        updated = self.working_config()
        updated["location"]["latitude"] = "48.2082"
        manager.save(updated)

        self.assertTrue(stub.load_config())
        self.assertEqual("48.2082", stub.config["location"]["latitude"])


class TestIntegrationExecutableAvailability(TempDirTestCase):
    """An executable path must point at a file, not at an existing directory."""

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_existing_file_is_a_usable_executable(self):
        executable = self.write_json(self.path("OpenRGB.exe"), {})
        stub = restore_stub()
        self.assertTrue(stub.integration_path_available("openrgb", executable))
        self.assertEqual([], stub.messages)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_existing_directory_is_not_a_usable_executable(self):
        os.makedirs(self.path("OpenRGB"))
        stub = restore_stub()
        self.assertFalse(stub.integration_path_available("openrgb", self.path("OpenRGB")))
        self.assertTrue(any("OpenRGB" in message for message in stub.messages))

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_missing_executable_is_skipped_with_a_warning(self):
        stub = restore_stub()
        self.assertFalse(stub.integration_path_available("artemis", self.path("nowhere", "Artemis.exe")))
        self.assertTrue(any("Artemis" in message for message in stub.messages))

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_blank_path_is_skipped_with_a_warning(self):
        stub = restore_stub()
        self.assertFalse(stub.integration_path_available("openrgb", ""))
        self.assertTrue(any("OpenRGB" in message for message in stub.messages))

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_launching_a_directory_is_refused(self):
        os.makedirs(self.path("OpenRGB"))
        stub = restore_stub()
        with self.assertRaises(FileNotFoundError):
            stub.launch_process(self.path("OpenRGB"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
