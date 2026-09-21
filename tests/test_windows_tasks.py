"""Tests for the Windows scheduled-task OpenRGB elevation broker.

These cover the whole seamless-elevation design without ever creating a real
elevated task, triggering a power event or suspending the PC:

* process elevation detection,
* the wake-time launch decision (direct launch / scheduled task / skip),
* the fixed task definition (exact path, fixed arguments, explicit working
  directory, no triggers),
* quoting of paths with spaces and non-ASCII characters,
* stale-task detection and repair reporting,
* the extended task identity (working directory, logon type, principal, no
  triggers),
* the protected-location check for an executable a privileged task may launch
  (effective rights, ownership, fail-closed paths, refusal before any approval),
* the configuration-driven task lifecycle (create on enable/path change,
  remove on disable),
* the narrow elevated provisioning command line,
* and the guarantee that the automatic restore path contains no interactive
  elevation call.

Every Task Scheduler interaction goes through `subprocess` and is mocked: the
normal unit-test suite never mutates the real Task Scheduler. The ACL inspection
is exercised for real in a few tests, but it only ever reads security
descriptors - it never changes permissions.
"""

import ctypes
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_manager as cm  # noqa: E402
import windows_tasks as wt  # noqa: E402
import yeelight_devices as app_yd  # noqa: E402

try:
    import yeelight_pc_companion as app  # noqa: E402
except Exception as exc:  # pragma: no cover - depends on the test machine
    app = None
    APP_IMPORT_ERROR = exc
else:
    APP_IMPORT_ERROR = None

try:
    import first_run_wizard as wizard  # noqa: E402
except Exception as exc:  # pragma: no cover - depends on the test machine
    wizard = None
    WIZARD_IMPORT_ERROR = exc
else:
    WIZARD_IMPORT_ERROR = None

SKIP_REASON = f"yeelight_pc_companion is not importable here: {APP_IMPORT_ERROR}"
WIZARD_SKIP_REASON = f"first_run_wizard is not importable here: {WIZARD_IMPORT_ERROR}"
WINDOWS_ONLY = "Windows Task Scheduler behaviour is Windows only"

TASK_XML_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def yeelight_device(name, ip, enabled=True):
    """One configured Yeelight device entry (the v2 shape)."""
    return {
        "id": "manual:test-device-%s" % ip.replace(".", "-"),
        "name": name,
        "ip": ip,
        "enabled": enabled,
    }


def task_definition_xml(
    command,
    arguments=wt.OPENRGB_TASK_ARGS_STRING,
    run_level="HighestAvailable",
    enabled="true",
    working_directory=None,
    logon_type="InteractiveToken",
    user_id="",
    triggers="<Triggers />",
):
    """A Task Scheduler XML document shaped like ``schtasks /query /xml`` output.

    ``working_directory=None`` means "the folder containing the executable", and
    an empty ``logon_type``/``user_id`` means "Task Scheduler omitted the value",
    which is how the real export behaves for values equal to its own defaults.
    """
    if working_directory is None:
        working_directory = os.path.dirname(command)
    principal_user = f"<UserId>{user_id}</UserId>" if user_id else ""
    logon = f"<LogonType>{logon_type}</LogonType>" if logon_type else ""
    working_dir = (
        f"<WorkingDirectory>{working_directory}</WorkingDirectory>"
        if working_directory
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>'
        f'<Task version="1.2" xmlns="{TASK_XML_NS}">'
        f"{triggers}"
        "<Principals>"
        '<Principal id="Author">'
        f"{principal_user}"
        f"{logon}"
        f"<RunLevel>{run_level}</RunLevel>"
        "</Principal>"
        "</Principals>"
        f"<Settings><Enabled>{enabled}</Enabled></Settings>"
        '<Actions Context="Author"><Exec>'
        f"<Command>{command}</Command>"
        f"<Arguments>{arguments}</Arguments>"
        f"{working_dir}"
        "</Exec></Actions>"
        "</Task>"
    )


def probe(command, **overrides):
    values = {
        "exists": True,
        "command": command,
        "arguments": wt.OPENRGB_TASK_ARGS_STRING,
        "run_level": "HighestAvailable",
        "enabled": True,
        # The identity this application itself writes into the task.
        "working_directory": os.path.dirname(command) if command else "",
        "logon_type": "InteractiveToken",
        "user_id": wt.current_task_principal(),
        "has_triggers": False,
    }
    values.update(overrides)
    return wt.TaskProbe(**values)


def status(state, label="x", detail=""):
    return wt.OpenRgbElevationStatus(state, label, detail)


def secure_target():
    """A target security result that allows provisioning (tests use this default)."""
    return wt.ElevationTargetSecurity(True, wt.SECURITY_OK, "protected in this test")


def insecure_target(code=wt.SECURITY_USER_WRITABLE):
    return wt.ElevationTargetSecurity(False, code, "not protected in this test")


def require_live_standard_user_token(testcase):
    """Skip a live ACL assertion when Windows exposes no standard-user token.

    GitHub-hosted Windows runners use a service/admin context that may not have
    the linked limited token a normal interactive UAC session provides. The
    deterministic ACL/security-policy tests still run everywhere; only the
    machine-integration assertions are skipped when that prerequisite is absent.
    """
    token = wt._open_standard_user_token()
    if not token:
        testcase.skipTest("this Windows account exposes no usable standard-user token")
    wt._close_token(token)


def restore_stub():
    """Minimal stand-in exposing the real launch decision (no QThread, no GUI)."""

    class RestoreStub:
        launch_openrgb = app.RestoreEngineThread.launch_openrgb

        def __init__(self):
            self.running = True
            self.messages = []
            self.launched = []
            self.progress_update = types.SimpleNamespace(emit=self.messages.append)

        def launch_process(self, path, args=None, hidden=True, cwd=None):
            self.launched.append((path, list(args or []), hidden, cwd))

    stub = RestoreStub()
    stub.messages = []
    stub.launched = []
    stub.progress_update = types.SimpleNamespace(emit=stub.messages.append)
    return stub


class SettingsStub:
    """Minimal stand-in for the Settings-side task synchronisation (no GUI)."""

    _sync_openrgb_elevation_task = (
        app.YeelightPCCompanionWindow._sync_openrgb_elevation_task if app is not None else None
    )

    def _confirm_openrgb_approval(self):
        return True


def disabled_openrgb_config(openrgb_path):
    config = cm.default_config()
    config["paths"]["openrgb"] = openrgb_path
    config["integrations"]["openrgb"]["enabled"] = False
    return config


# ---------------------------------------------------------
# 1. Elevated-process detection
# ---------------------------------------------------------
class ProvisioningDiagnosticsTestCase(unittest.TestCase):
    """Base class: never let a test write to the real application data.

    Anything that exercises the provisioning CLI also writes its diagnostics
    files, so those are redirected to a per-test temporary directory.
    """

    def setUp(self):
        self.diagnostics_dir = tempfile.mkdtemp(prefix="yeelight-provisioning-")
        patch = mock.patch.object(
            wt, "provisioning_data_dir", return_value=self.diagnostics_dir
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.diagnostics_dir, ignore_errors=True)


class TestElevatedProcessDetection(unittest.TestCase):
    def test_token_reports_elevated(self):
        with mock.patch.object(wt, "_read_process_elevation", return_value=1):
            self.assertTrue(wt.is_process_elevated())

    def test_token_reports_not_elevated(self):
        with mock.patch.object(wt, "_read_process_elevation", return_value=0):
            self.assertFalse(wt.is_process_elevated())

    def test_falls_back_to_is_user_an_admin_when_the_token_check_fails(self):
        fake_shell32 = types.SimpleNamespace(IsUserAnAdmin=mock.Mock(return_value=1))
        with mock.patch.object(wt, "_read_process_elevation", side_effect=OSError("no token")):
            with mock.patch.object(
                wt.ctypes, "windll", types.SimpleNamespace(shell32=fake_shell32), create=True
            ):
                self.assertTrue(wt.is_process_elevated())

    def test_every_check_failing_is_reported_as_not_elevated(self):
        failing = types.SimpleNamespace(
            IsUserAnAdmin=mock.Mock(side_effect=OSError("no shell32"))
        )
        with mock.patch.object(wt, "_read_process_elevation", side_effect=OSError("no token")):
            with mock.patch.object(
                wt.ctypes, "windll", types.SimpleNamespace(shell32=failing), create=True
            ):
                self.assertFalse(wt.is_process_elevated())


# ---------------------------------------------------------
# 2-8. Wake-time launch decision
# ---------------------------------------------------------
class TestRestoreOpenRgbLaunch(unittest.TestCase):
    OPENRGB = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_elevated_app_launches_openrgb_directly(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=True):
            with mock.patch.object(app, "run_openrgb_task") as run_task:
                self.assertTrue(stub.launch_openrgb(self.OPENRGB))

        self.assertEqual(
            stub.launched,
            [
                (
                    self.OPENRGB,
                    ["--gui", "--startminimized", "--server"],
                    True,
                    os.path.dirname(self.OPENRGB),
                )
            ],
        )
        run_task.assert_not_called()

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_the_direct_launch_pins_the_working_directory_to_openrgb(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=True):
            stub.launch_openrgb(self.OPENRGB)

        path, args, hidden, cwd = stub.launched[0]
        self.assertEqual(path, self.OPENRGB)
        self.assertEqual(args, ["--gui", "--startminimized", "--server"])
        self.assertEqual(cwd, os.path.dirname(self.OPENRGB))
        # Never the application's own working directory.
        self.assertNotEqual(cwd, os.getcwd())

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_the_direct_launch_keeps_the_fixed_openrgb_arguments(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=True):
            stub.launch_openrgb(self.OPENRGB)

        self.assertEqual(stub.launched[0][1], list(wt.OPENRGB_TASK_ARGS))

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_unelevated_app_with_ready_task_triggers_the_task(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(
                app, "openrgb_elevation_status", return_value=status(wt.STATUS_READY, "Ready")
            ):
                with mock.patch.object(app, "run_openrgb_task") as run_task:
                    self.assertTrue(stub.launch_openrgb(self.OPENRGB))

        run_task.assert_called_once_with()
        self.assertEqual(stub.launched, [])

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_unelevated_app_with_missing_task_never_requests_elevation(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(
                app,
                "openrgb_elevation_status",
                return_value=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
            ):
                with mock.patch.object(app, "run_openrgb_task") as run_task:
                    self.assertFalse(stub.launch_openrgb(self.OPENRGB))

        run_task.assert_not_called()
        self.assertEqual(stub.launched, [])
        self.assertTrue(
            any("Skipping OpenRGB for this restore" in message for message in stub.messages),
            stub.messages,
        )
        # The restore path cannot reach the elevation helper at all.
        self.assertFalse(hasattr(app, "request_elevated_openrgb_provisioning"))
        self.assertNotIn("runas", inspect.getsource(app.RestoreEngineThread).lower())

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_stale_task_is_reported_as_needing_repair_at_wake_time(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(
                app,
                "openrgb_elevation_status",
                return_value=status(wt.STATUS_PATH_MISMATCH, "Task points to a different path"),
            ):
                with mock.patch.object(app, "run_openrgb_task") as run_task:
                    self.assertFalse(stub.launch_openrgb(self.OPENRGB))

        run_task.assert_not_called()
        self.assertEqual(stub.launched, [])

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_a_failing_task_run_is_skipped_without_raising(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(
                app, "openrgb_elevation_status", return_value=status(wt.STATUS_READY, "Ready")
            ):
                with mock.patch.object(
                    app, "run_openrgb_task", side_effect=wt.WindowsTaskError("boom")
                ):
                    self.assertFalse(stub.launch_openrgb(self.OPENRGB))

        self.assertEqual(stub.launched, [])
        self.assertTrue(
            any("Skipping OpenRGB for this restore" in message for message in stub.messages),
            stub.messages,
        )

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_unreadable_task_status_degrades_to_skipping(self):
        stub = restore_stub()
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(
                app, "openrgb_elevation_status", side_effect=RuntimeError("nope")
            ):
                with mock.patch.object(app, "run_openrgb_task") as run_task:
                    self.assertFalse(stub.launch_openrgb(self.OPENRGB))

        run_task.assert_not_called()
        self.assertEqual(stub.launched, [])


# ---------------------------------------------------------
# 5-6. The whole restore sequence keeps running
# ---------------------------------------------------------
class RestoreFlowStub:
    """Runs the real `RestoreEngineThread.run()` with the side effects neutralised."""

    run = app.RestoreEngineThread.run if app is not None else None
    launch_openrgb = app.RestoreEngineThread.launch_openrgb if app is not None else None
    integration_path_available = (
        app.RestoreEngineThread.integration_path_available if app is not None else None
    )

    def __init__(self, config_manager, is_dark=True):
        self.config_manager = config_manager
        self.running = True
        self.is_dark = is_dark
        self.messages = []
        self.finished = []
        self.sleeps = []
        self.killed = []
        self.launched = []
        self.launch_calls = []
        self.turned_on = []
        self.turned_off = []
        self.openrgb_ready = True
        self.ready_waits = 0
        self.launched_flags = []
        self.ready_waits_when_artemis_launched = []
        self.progress_update = types.SimpleNamespace(emit=self.messages.append)
        self.finished_sequence = types.SimpleNamespace(emit=lambda: self.finished.append(True))

    def wait_for_openrgb_ready(self, launched=False):
        """The real readiness gate talks to a live OpenRGB SDK server.

        These tests exercise the *sequence* around the gate, so the gate is
        replaced by a recorded, configurable stand-in: it never opens a socket.
        `launched` mirrors the wrapper's signature so the sequence's own call
        site is exercised unchanged.
        """
        self.ready_waits += 1
        self.launched_flags.append(launched)
        return self.openrgb_ready

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def kill_process(self, name):
        self.killed.append(name)

    def is_process_running(self, name):
        return False

    def launch_process(self, path, args=None, hidden=True, cwd=None):
        self.launched.append((path, list(args or []), cwd))
        self.launch_calls.append((path, list(args or []), hidden, cwd))
        # Record how many readiness gates had completed by the time each
        # process was started, so the ordering property can be asserted.
        if os.path.basename(path) == cm.INTEGRATIONS["artemis"]["process"]:
            self.ready_waits_when_artemis_launched.append(self.ready_waits)

    def evaluate_solar_state(self, config):
        return self.is_dark

    power_devices = app.RestoreEngineThread.power_devices if app is not None else None

    def safe_turn_on(self, ip):
        self.turned_on.append(ip)

    def safe_turn_off(self, ip):
        self.turned_off.append(ip)


@unittest.skipIf(app is None, SKIP_REASON)
class TestRestoreSequenceContinuesWithoutOpenRgb(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-openrgb-elevation-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)

    def make_executable(self, name):
        path = self.path(name)
        with open(path, "wb") as handle:
            handle.write(b"MZ")
        return path

    def write_config(self, openrgb_enabled):
        config = cm.default_config()
        config["location"].update({"latitude": "11.1111", "longitude": "-22.2222"})
        config["lights"]["devices"] = [yeelight_device("Desk Lamp", "192.168.1.50")]
        config["paths"]["openrgb"] = self.make_executable("OpenRGB.exe")
        config["paths"]["yeelight_connector"] = self.make_executable("Yeelight Chroma Connector.exe")
        config["paths"]["artemis"] = self.make_executable("Artemis.UI.Windows.exe")
        config["integrations"]["openrgb"]["enabled"] = openrgb_enabled
        config["integrations"]["yeelight_connector"]["enabled"] = True
        config["integrations"]["artemis"]["enabled"] = True
        config["automation"]["wait_for_razer_synapse"] = False
        config["automation"]["launch_razer_synapse"] = False
        config_path = self.path(cm.CONFIG_FILENAME)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
        return cm.ConfigManager(config_path)

    def run_sequence(self, openrgb_enabled):
        manager = self.write_config(openrgb_enabled)
        stub = RestoreFlowStub(manager, is_dark=True)
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(
                app,
                "openrgb_elevation_status",
                return_value=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
            ):
                with mock.patch.object(app, "run_openrgb_task") as run_task:
                    stub.run()
        return stub, run_task

    def run_sequence_with_a_matching_task_in_a_user_writable_folder(self):
        """Run the real sequence against a real, unprotected OpenRGB location.

        The configuration points at an executable inside a user-writable folder
        (the test's temporary directory), a *matching* task is reported by the
        (mocked) Task Scheduler query, and the elevation status is computed by
        the real code - including the real ACL inspection. A privileged task
        must never be started for such a target.
        """
        manager = self.write_config(openrgb_enabled=True)
        openrgb_path = os.path.join(self.tmpdir, "OpenRGB.exe")
        matching_task = wt.TaskProbe(
            exists=True,
            command=openrgb_path,
            arguments=wt.OPENRGB_TASK_ARGS_STRING,
            run_level="HighestAvailable",
            working_directory=os.path.dirname(openrgb_path),
            logon_type="InteractiveToken",
            user_id=wt.current_task_principal(),
        )
        stub = RestoreFlowStub(manager, is_dark=True)
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(wt, "query_openrgb_task", return_value=matching_task):
                with mock.patch.object(app, "run_openrgb_task") as run_task:
                    stub.run()
        return stub, run_task

    def test_missing_task_does_not_abort_the_rest_of_the_restore(self):
        stub, run_task = self.run_sequence(openrgb_enabled=True)

        run_task.assert_not_called()
        self.assertEqual([entry for entry in stub.launched if "OpenRGB" in entry[0]], [])
        self.assertTrue(
            any("Skipping OpenRGB for this restore" in message for message in stub.messages),
            stub.messages,
        )
        # The later steps still ran: the night path turned the lights on and the
        # sequence reported completion.
        self.assertIn("192.168.1.50", stub.turned_on)
        self.assertIn(
            "Restoration sequence successfully completed!", stub.messages
        )
        self.assertEqual(stub.finished, [True])

    def test_an_unsafe_openrgb_location_is_skipped_at_wake_time(self):
        stub, run_task = self.run_sequence_with_a_matching_task_in_a_user_writable_folder()

        # The task matches the configuration, but the target may not be handed to
        # a privileged task, so it is never started and OpenRGB is never launched
        # directly either.
        run_task.assert_not_called()
        self.assertEqual([entry for entry in stub.launched if "OpenRGB" in entry[0]], [])
        self.assertTrue(
            any("Skipping OpenRGB for this restore" in message for message in stub.messages),
            stub.messages,
        )
        # The rest of the restoration is unaffected.
        self.assertIn("192.168.1.50", stub.turned_on)
        self.assertIn("Restoration sequence successfully completed!", stub.messages)
        self.assertEqual(stub.finished, [True])

    def test_disabled_openrgb_is_never_launched_or_task_run(self):
        stub, run_task = self.run_sequence(openrgb_enabled=False)

        run_task.assert_not_called()
        self.assertEqual([entry for entry in stub.launched if "OpenRGB" in entry[0]], [])
        self.assertTrue(
            any("OpenRGB integration is disabled" in message for message in stub.messages),
            stub.messages,
        )
        self.assertIn(
            "Restoration sequence successfully completed!", stub.messages
        )


# ---------------------------------------------------------
# 7-10. Task definition: exact path, fixed arguments, quoting
# ---------------------------------------------------------
class TestOpenRgbTaskDefinition(unittest.TestCase):
    def find(self, xml_text, path):
        root = ET.fromstring(xml_text)
        wt._strip_namespaces(root)
        return root.findtext(path)

    def test_action_uses_the_exact_openrgb_path(self):
        xml_text = wt.build_openrgb_task_xml(r"C:\Program Files\OpenRGB\OpenRGB.exe")
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/Command"),
            r"C:\Program Files\OpenRGB\OpenRGB.exe",
        )

    def test_action_sets_the_working_directory_to_the_executable_folder(self):
        executable = r"C:\Program Files\OpenRGB\OpenRGB.exe"
        xml_text = wt.build_openrgb_task_xml(executable)

        self.assertIn("<WorkingDirectory>", xml_text)
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/WorkingDirectory"),
            r"C:\Program Files\OpenRGB",
        )
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/WorkingDirectory"),
            os.path.dirname(executable),
        )

    def test_command_arguments_and_working_directory_stay_separate_fields(self):
        executable = r"C:\Program Files\OpenRGB\OpenRGB.exe"
        xml_text = wt.build_openrgb_task_xml(executable)

        # Never a shell string: three separate action elements.
        self.assertEqual(self.find(xml_text, "Actions/Exec/Command"), executable)
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/Arguments"), "--gui --startminimized --server"
        )
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/WorkingDirectory"), r"C:\Program Files\OpenRGB"
        )

    def test_working_directory_keeps_spaces(self):
        executable = r"C:\Program Files (x86)\My RGB Tools\OpenRGB\OpenRGB.exe"
        xml_text = wt.build_openrgb_task_xml(executable)
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/WorkingDirectory"),
            r"C:\Program Files (x86)\My RGB Tools\OpenRGB",
        )

    def test_working_directory_keeps_non_ascii_characters(self):
        executable = "C:\\Users\\Jörg\\Äpp Säker\\OpenRGB\\OpenRGB.exe"
        xml_text = wt.build_openrgb_task_xml(executable)
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/WorkingDirectory"),
            "C:\\Users\\Jörg\\Äpp Säker\\OpenRGB",
        )

    def test_xml_special_characters_are_still_escaped(self):
        # The escaping helper is imported where it is used (see the import-cost
        # tests below) rather than at module import time; this proves the
        # escaping itself is unchanged, by requiring the document to stay
        # well-formed XML that round-trips to the original path.
        xml_text = wt.build_openrgb_task_xml(r"C:\Tools & More\OpenRGB.exe")
        self.assertIn("&amp;", xml_text)
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/Command"),
            r"C:\Tools & More\OpenRGB.exe",
        )
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/WorkingDirectory"),
            r"C:\Tools & More",
        )

    def test_the_queried_working_directory_matches_the_created_one(self):
        # The task this application writes must survive a Task Scheduler
        # round-trip through the query parser unchanged.
        executable = r"C:\Program Files\OpenRGB\OpenRGB.exe"
        created = wt.build_openrgb_task_xml(executable)
        with mock.patch.object(wt, "_run_schtasks", return_value=(0, created, "")):
            probe_result = wt.query_openrgb_task()
        self.assertEqual(probe_result.working_directory, os.path.dirname(executable))

    def test_action_uses_the_fixed_openrgb_arguments(self):
        xml_text = wt.build_openrgb_task_xml(r"C:\OpenRGB\OpenRGB.exe")
        self.assertEqual(
            self.find(xml_text, "Actions/Exec/Arguments"),
            "--gui --startminimized --server",
        )
        self.assertEqual(wt.OPENRGB_TASK_ARGS_STRING, "--gui --startminimized --server")

    def test_task_runs_with_highest_privileges_and_has_no_triggers(self):
        xml_text = wt.build_openrgb_task_xml(r"C:\OpenRGB\OpenRGB.exe", user_id="PC\\tester")
        root = ET.fromstring(xml_text)
        wt._strip_namespaces(root)

        self.assertEqual(self.find(xml_text, "Principals/Principal/RunLevel"), "HighestAvailable")
        self.assertEqual(
            self.find(xml_text, "Principals/Principal/LogonType"), "InteractiveToken"
        )
        self.assertEqual(self.find(xml_text, "Principals/Principal/UserId"), "PC\\tester")
        self.assertEqual(self.find(xml_text, "Settings/AllowStartOnDemand"), "true")
        # No trigger element may ever carry a schedule or a logon trigger.
        self.assertEqual(list(root.find("Triggers")), [])
        for unwanted in ("LogonTrigger", "BootTrigger", "TimeTrigger", "CalendarTrigger"):
            self.assertNotIn(unwanted, xml_text)

    def test_spaces_in_the_path_survive_validation(self):
        path = os.path.join(tempfile.gettempdir(), "Program Files", "OpenRGB", "OpenRGB.exe")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "wb") as handle:
                handle.write(b"MZ")
            self.assertEqual(wt.validate_openrgb_executable(path), path)
            xml_text = wt.build_openrgb_task_xml(path)
            self.assertEqual(self.find(xml_text, "Actions/Exec/Command"), path)
            self.assertEqual(
                self.find(xml_text, "Actions/Exec/WorkingDirectory"),
                os.path.dirname(path),
            )
        finally:
            shutil.rmtree(os.path.join(tempfile.gettempdir(), "Program Files", "OpenRGB"), ignore_errors=True)

    def test_unicode_path_survives_validation_and_the_task_definition(self):
        path = os.path.join(tempfile.gettempdir(), "OpenRGB-Ünïcödé-Ω", "OpenRGB.exe")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "wb") as handle:
                handle.write(b"MZ")
            self.assertEqual(wt.validate_openrgb_executable(path), path)
            xml_text = wt.build_openrgb_task_xml(path)
            self.assertEqual(self.find(xml_text, "Actions/Exec/Command"), path)
            self.assertEqual(
                self.find(xml_text, "Actions/Exec/WorkingDirectory"),
                os.path.dirname(path),
            )
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_provisioning_never_builds_a_shell_string(self):
        captured = {}

        def fake_run(args, timeout=None):
            captured["args"] = list(args)
            xml_path = args[args.index("/xml") + 1]
            with open(xml_path, "r", encoding="utf-16") as handle:
                captured["xml"] = handle.read()
            return 0, "", ""

        path = os.path.join(tempfile.gettempdir(), "OpenRGB Elevation Probe", "OpenRGB.exe")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "wb") as handle:
                handle.write(b"MZ")
            with mock.patch.object(wt, "is_elevation_target_secure", return_value=secure_target()):
                with mock.patch.object(wt, "_run_schtasks", side_effect=fake_run):
                    wt.provision_openrgb_task(path)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

        args = captured["args"]
        # An argument list, never one concatenated command line: the path stays a
        # single element and is never re-parsed by a shell.
        self.assertIsInstance(args, list)
        for value in args:
            self.assertIsInstance(value, str)
        self.assertEqual(args[:3], ["/create", "/tn", wt.OPENRGB_TASK_NAME])
        self.assertIn("/f", args)
        self.assertIn("/xml", args)
        self.assertEqual(
            self.find(captured["xml"], "Actions/Exec/Command"),
            os.path.abspath(path),
        )
        # The task that is really handed to schtasks always carries the working
        # directory of the executable it launches.
        self.assertEqual(
            self.find(captured["xml"], "Actions/Exec/WorkingDirectory"),
            os.path.dirname(os.path.abspath(path)),
        )

    def test_schtasks_module_never_uses_a_shell(self):
        source = inspect.getsource(wt)
        self.assertNotIn("shell=True", source)

    def test_validate_rejects_command_like_or_non_executable_paths(self):
        for bad in ("", "   ", r"C:\OpenRGB\OpenRGB.bat", r"C:\OpenRGB\OpenRGB.cmd",
                    r"C:\OpenRGB\OpenRGB.exe\r\n--evil", None, 42):
            with self.assertRaises(wt.WindowsTaskError, msg=repr(bad)):
                wt.validate_openrgb_executable(bad)

    def test_validate_rejects_a_path_that_is_not_a_file(self):
        directory = tempfile.mkdtemp(prefix="yeelight-not-a-file-")
        try:
            with self.assertRaises(wt.WindowsTaskError):
                wt.validate_openrgb_executable(directory)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------
# 11. Stale task / path mismatch
# ---------------------------------------------------------
@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class TestElevationStatus(unittest.TestCase):
    OPENRGB = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def status_with(self, task_probe, path=None, enabled=True, security=None):
        # The target security check has its own tests; the task-identity tests
        # below start from a provably protected target so they stay independent
        # of where (or whether) OpenRGB is installed on the test machine.
        if security is None:
            security = secure_target()
        with mock.patch.object(wt, "query_openrgb_task", return_value=task_probe):
            with mock.patch.object(
                wt, "is_elevation_target_secure", return_value=security
            ):
                return wt.openrgb_elevation_status(
                    path or self.OPENRGB, integration_is_enabled=enabled
                )

    def test_matching_task_is_ready(self):
        self.assertTrue(self.status_with(probe(self.OPENRGB)).is_ready)

    def test_missing_task_needs_setup(self):
        result = self.status_with(wt.TaskProbe(exists=False, error="not found"))
        self.assertEqual(result.state, wt.STATUS_NEEDS_SETUP)
        self.assertTrue(result.needs_action)
        self.assertEqual(result.action_label, wt.SET_UP_ACTION_LABEL)

    def test_task_pointing_at_another_path_needs_repair(self):
        result = self.status_with(probe(r"C:\Old Location\OpenRGB.exe"))
        self.assertEqual(result.state, wt.STATUS_PATH_MISMATCH)
        self.assertEqual(result.label, "Task points to a different OpenRGB path")
        self.assertTrue(result.needs_action)
        self.assertEqual(result.action_label, wt.REPAIR_ACTION_LABEL)

    def test_path_comparison_ignores_case_and_separator_style(self):
        result = self.status_with(probe(self.OPENRGB.lower()))
        self.assertTrue(result.is_ready)

    def test_disabled_task_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, enabled=False))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)

    def test_task_without_highest_privileges_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, run_level="LeastPrivilege"))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)

    def test_task_with_unexpected_arguments_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, arguments="--server"))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)

    def test_task_without_a_working_directory_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, working_directory=""))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)
        self.assertTrue(result.needs_action)
        self.assertEqual(result.action_label, wt.REPAIR_ACTION_LABEL)

    def test_task_with_a_wrong_working_directory_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, working_directory=r"C:\Elsewhere"))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)

    def test_task_with_the_executable_folder_is_ready(self):
        result = self.status_with(
            probe(self.OPENRGB, working_directory=os.path.dirname(self.OPENRGB).lower())
        )
        self.assertTrue(result.is_ready)

    def test_task_with_a_different_logon_type_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, logon_type="Password"))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)

    def test_task_without_a_stated_logon_type_is_accepted(self):
        # Task Scheduler omits values that equal its own defaults.
        self.assertTrue(self.status_with(probe(self.OPENRGB, logon_type="")).is_ready)

    def test_task_of_a_different_principal_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, user_id=r"OTHERPC\somebodyelse"))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)

    def test_task_without_a_stated_principal_is_accepted(self):
        self.assertTrue(self.status_with(probe(self.OPENRGB, user_id="")).is_ready)

    def test_task_with_a_trigger_needs_repair(self):
        result = self.status_with(probe(self.OPENRGB, has_triggers=True))
        self.assertEqual(result.state, wt.STATUS_NEEDS_REPAIR)
        self.assertIn("trigger", result.detail.lower())

    def test_correct_task_identity_is_ready(self):
        result = self.status_with(probe(self.OPENRGB))

        self.assertTrue(result.is_ready)
        self.assertEqual(result.state, wt.STATUS_READY)
        self.assertEqual(result.label, "Ready")
        self.assertFalse(result.needs_action)

    def test_an_unsafe_location_is_never_ready(self):
        result = self.status_with(probe(self.OPENRGB), security=insecure_target())

        self.assertFalse(result.is_ready)
        self.assertEqual(result.state, wt.STATUS_UNSAFE_TARGET)
        self.assertEqual(result.label, wt.UNSAFE_TARGET_LABEL)
        self.assertEqual(result.detail, wt.UNSAFE_TARGET_MESSAGE)
        self.assertTrue(result.needs_action)
        self.assertEqual(result.action_label, wt.REPAIR_ACTION_LABEL)

    def test_an_unverifiable_location_is_never_ready(self):
        result = self.status_with(
            probe(self.OPENRGB), security=insecure_target(wt.SECURITY_UNVERIFIED)
        )

        self.assertFalse(result.is_ready)
        self.assertEqual(result.state, wt.STATUS_UNSAFE_TARGET)
        self.assertEqual(result.label, wt.UNVERIFIED_TARGET_LABEL)
        self.assertEqual(result.detail, wt.UNVERIFIED_TARGET_MESSAGE)

    def test_an_unsafe_location_is_reported_even_without_a_task(self):
        result = self.status_with(wt.TaskProbe(exists=False), security=insecure_target())
        self.assertEqual(result.state, wt.STATUS_UNSAFE_TARGET)
        self.assertFalse(result.is_ready)

    def test_the_target_security_is_checked_for_the_saved_path(self):
        with mock.patch.object(wt, "query_openrgb_task", return_value=probe(self.OPENRGB)):
            with mock.patch.object(
                wt, "is_elevation_target_secure", return_value=secure_target()
            ) as check:
                wt.openrgb_elevation_status(self.OPENRGB)
        check.assert_called_once_with(self.OPENRGB)

    def test_disabled_integration_reports_not_used(self):
        result = self.status_with(wt.TaskProbe(exists=False), enabled=False)
        self.assertEqual(result.state, wt.STATUS_DISABLED)
        self.assertFalse(result.needs_action)
        self.assertIsNone(result.action_label)

    def test_query_parses_the_task_scheduler_xml(self):
        with mock.patch.object(
            wt, "_run_schtasks", return_value=(0, task_definition_xml(self.OPENRGB), "")
        ):
            result = wt.query_openrgb_task()
        self.assertTrue(result.exists)
        self.assertEqual(result.command, self.OPENRGB)
        self.assertEqual(result.arguments, wt.OPENRGB_TASK_ARGS_STRING)
        self.assertEqual(result.run_level, "HighestAvailable")
        self.assertTrue(result.enabled)

    def test_query_parses_the_task_identity(self):
        xml_text = task_definition_xml(
            self.OPENRGB, user_id="S-1-5-21-1-2-3-1001", working_directory=r"C:\Program Files\OpenRGB"
        )
        with mock.patch.object(wt, "_run_schtasks", return_value=(0, xml_text, "")):
            result = wt.query_openrgb_task()

        self.assertEqual(result.working_directory, r"C:\Program Files\OpenRGB")
        self.assertEqual(result.logon_type, "InteractiveToken")
        self.assertEqual(result.user_id, "S-1-5-21-1-2-3-1001")
        self.assertFalse(result.has_triggers)

    def test_query_reports_a_trigger(self):
        xml_text = task_definition_xml(
            self.OPENRGB, triggers="<Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>"
        )
        with mock.patch.object(wt, "_run_schtasks", return_value=(0, xml_text, "")):
            result = wt.query_openrgb_task()

        self.assertTrue(result.has_triggers)

    def test_query_tolerates_omitted_default_values(self):
        # Task Scheduler omits fields that equal its own defaults; those must be
        # reported as "not stated" instead of leaking into the status as garbage.
        xml_text = task_definition_xml(
            self.OPENRGB, working_directory="", logon_type="", user_id="", triggers=""
        )
        with mock.patch.object(wt, "_run_schtasks", return_value=(0, xml_text, "")):
            result = wt.query_openrgb_task()

        self.assertTrue(result.exists)
        self.assertEqual(result.working_directory, "")
        self.assertEqual(result.logon_type, "")
        self.assertEqual(result.user_id, "")
        self.assertFalse(result.has_triggers)

    def test_query_never_assumes_defaults_for_the_working_directory(self):
        # A task without a working directory runs wherever Windows decides, which
        # is not the folder that contains OpenRGB.
        status_result = self.status_with(probe(self.OPENRGB, working_directory=""))
        self.assertEqual(status_result.state, wt.STATUS_NEEDS_REPAIR)

    def test_query_treats_a_nonzero_exit_as_a_missing_task(self):
        with mock.patch.object(wt, "_run_schtasks", return_value=(1, "ERROR: not found", "")):
            result = wt.query_openrgb_task()
        self.assertFalse(result.exists)
        self.assertIn("not found", result.error)

    def test_query_never_touches_a_different_task_name(self):
        with mock.patch.object(wt, "_run_schtasks", return_value=(0, "", "")) as run:
            with mock.patch.object(wt.ET, "fromstring", side_effect=ET.ParseError):
                wt.query_openrgb_task()
        self.assertEqual(run.call_args[0][0][:2], ["/query", "/tn"])
        self.assertEqual(run.call_args[0][0][2], wt.OPENRGB_TASK_NAME)
        self.assertEqual(wt.OPENRGB_TASK_NAME, "YeelightPCCompanion-OpenRGB")


# ---------------------------------------------------------
# 11b. Elevated-launch target security (ACLs / effective rights)
# ---------------------------------------------------------
@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class TestElevationTargetSecurity(unittest.TestCase):
    """The privileged task may only ever launch an executable the user cannot replace."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-elevation-target-")
        self.exe = os.path.join(self.tmpdir, "OpenRGB.exe")
        with open(self.exe, "wb") as handle:
            handle.write(b"MZ")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- real Windows ACL inspection -------------------------------------
    def test_a_user_writable_executable_is_reported_unsafe(self):
        require_live_standard_user_token(self)
        # The test's own temporary folder is writable by the current user, which
        # is exactly the situation that must never be given to a privileged task.
        result = wt.is_elevation_target_secure(self.exe)

        self.assertFalse(result.is_secure)
        self.assertFalse(bool(result))
        self.assertEqual(result.code, wt.SECURITY_USER_WRITABLE)
        self.assertEqual(result.message, wt.UNSAFE_TARGET_MESSAGE)
        self.assertEqual(result.label, wt.UNSAFE_TARGET_LABEL)
        self.assertTrue(result.reason)

    def test_a_protected_location_is_accepted(self):
        require_live_standard_user_token(self)
        # C:\Windows\System32 is protected for a normal user account; the check
        # must not reject the kind of location Program Files-style installs use.
        protected = os.path.join(
            os.environ.get("SystemRoot") or r"C:\Windows", "System32", "cmd.exe"
        )
        if not os.path.isfile(protected):  # pragma: no cover - depends on the test machine
            self.skipTest("no system file available for the protected-location check")

        result = wt.is_elevation_target_secure(protected)

        self.assertTrue(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_OK)
        self.assertEqual(result.message, "")

    def test_an_absent_executable_is_reported_as_missing(self):
        result = wt.is_elevation_target_secure(os.path.join(self.tmpdir, "gone.exe"))
        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_MISSING)
        self.assertTrue(result.executable_missing)
        # A missing executable has no user-facing refusal text: nothing can be
        # replaced, and validate_openrgb_executable() refuses it for provisioning.
        self.assertEqual(result.message, "")

    def test_the_checked_targets_cover_the_executable_and_every_parent(self):
        targets = wt._replacement_check_targets(self.exe)
        paths = [path for path, _rights, _what in targets]

        self.assertEqual(paths[0], os.path.abspath(self.exe))
        self.assertEqual(paths[1], os.path.dirname(os.path.abspath(self.exe)))
        for index in range(1, len(paths)):
            self.assertEqual(paths[index], os.path.dirname(paths[index - 1]))
        # The walk stops at the volume root instead of looping there.
        self.assertEqual(os.path.dirname(paths[-1]), paths[-1])
        # The executable itself and its own folder are checked for every way of
        # replacing it; higher folders only for the rights that can reach it.
        self.assertEqual(targets[0][1], wt._REPLACEMENT_RIGHTS)
        self.assertEqual(targets[1][1], wt._FOLDER_REPLACEMENT_RIGHTS)
        self.assertEqual(targets[-1][1], wt._ANCESTOR_REPLACEMENT_RIGHTS)
        self.assertTrue(wt._FOLDER_REPLACEMENT_RIGHTS & wt.FILE_DELETE_CHILD)
        # Ancestors above the containing folder carry only the rights that can
        # reach the executable through them: deleting or renaming a folder in the
        # chain is what an attacker would have to do first, so DELETE and
        # FILE_DELETE_CHILD are part of that mask, while create rights (which
        # cannot replace anything below them on their own) are not.
        self.assertTrue(wt._ANCESTOR_REPLACEMENT_RIGHTS & wt.DELETE)
        self.assertTrue(wt._ANCESTOR_REPLACEMENT_RIGHTS & wt.FILE_DELETE_CHILD)
        self.assertTrue(wt._ANCESTOR_REPLACEMENT_RIGHTS & wt.WRITE_DAC)
        self.assertTrue(wt._ANCESTOR_REPLACEMENT_RIGHTS & wt.WRITE_OWNER)
        self.assertFalse(wt._ANCESTOR_REPLACEMENT_RIGHTS & wt.FILE_WRITE_DATA)
        self.assertFalse(wt._ANCESTOR_REPLACEMENT_RIGHTS & wt.FILE_APPEND_DATA)

    def test_every_dangerous_right_is_considered(self):
        mask = wt._REPLACEMENT_RIGHTS
        for bit, name in (
            (wt.FILE_WRITE_DATA, "write data / add file"),
            (wt.FILE_APPEND_DATA, "append"),
            (wt.FILE_WRITE_ATTRIBUTES, "write attributes"),
            (wt.WRITE_DAC, "write DAC"),
            (wt.WRITE_OWNER, "write owner"),
            (wt.DELETE, "delete"),
        ):
            with self.subTest(right=name):
                self.assertTrue(mask & bit, name)

    # --- fail-closed behaviour (Windows APIs mocked) ---------------------
    def _inspect_with(self, owner="S-1-5-18", access=0, token=object()):
        with mock.patch.object(wt, "_open_standard_user_token", return_value=token):
            with mock.patch.object(wt, "_close_token") as close:
                with mock.patch.object(wt, "_owner_sid_string", return_value=owner):
                    with mock.patch.object(wt, "_effective_access", return_value=access):
                        result = wt.is_elevation_target_secure(self.exe)
        return result, close

    def test_a_user_writable_containing_folder_is_reported_unsafe(self):
        # The executable itself is protected; the folder that contains it is not,
        # so its contents (including any DLL OpenRGB loads from there) could be
        # replaced by the current user.
        executable = os.path.abspath(self.exe)

        def fake_access(path, _mask, _token):
            return 0 if os.path.normcase(path) == os.path.normcase(executable) else _mask

        with mock.patch.object(wt, "_open_standard_user_token", return_value=object()):
            with mock.patch.object(wt, "_close_token"):
                with mock.patch.object(wt, "_owner_sid_string", return_value="S-1-5-18"):
                    with mock.patch.object(
                        wt, "_effective_access", side_effect=fake_access
                    ):
                        result = wt.is_elevation_target_secure(self.exe)

        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_USER_WRITABLE)
        self.assertIn("folder", result.reason)
        self.assertEqual(result.message, wt.UNSAFE_TARGET_MESSAGE)

    def test_a_protected_target_with_a_protected_folder_is_accepted(self):
        result, _close = self._inspect_with()
        self.assertTrue(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_OK)

    def test_a_target_owned_by_the_current_user_is_unsafe(self):
        sid = wt._current_user_sid()
        if not sid:  # pragma: no cover - depends on the test machine
            self.skipTest("Windows did not report a SID for this process")
        result, _close = self._inspect_with(owner=sid)
        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_USER_WRITABLE)
        self.assertIn("owned by the current user", result.reason)

    def test_a_missing_token_fails_closed(self):
        with mock.patch.object(wt, "_open_standard_user_token", return_value=None):
            result = wt.is_elevation_target_secure(self.exe)

        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_UNVERIFIED)
        self.assertEqual(result.message, wt.UNVERIFIED_TARGET_MESSAGE)
        self.assertEqual(result.label, wt.UNVERIFIED_TARGET_LABEL)

    def test_an_unreadable_security_descriptor_fails_closed(self):
        result, _close = self._inspect_with(access=None)
        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_UNVERIFIED)
        self.assertEqual(result.message, wt.UNVERIFIED_TARGET_MESSAGE)

    def test_an_unreadable_owner_fails_closed(self):
        result, _close = self._inspect_with(owner=None)
        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_UNVERIFIED)

    def test_an_unknown_user_identity_fails_closed(self):
        with mock.patch.object(wt, "_open_standard_user_token", return_value=object()):
            with mock.patch.object(wt, "_close_token"):
                with mock.patch.object(wt, "_current_user_sid", return_value=""):
                    result = wt.is_elevation_target_secure(self.exe)

        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_UNVERIFIED)

    def test_a_raising_inspection_never_propagates(self):
        with mock.patch.object(wt, "_open_standard_user_token", side_effect=OSError("boom")):
            result = wt.is_elevation_target_secure(self.exe)

        self.assertFalse(result.is_secure)
        self.assertEqual(result.code, wt.SECURITY_UNVERIFIED)

    def test_the_token_is_always_closed(self):
        _result, close = self._inspect_with()
        close.assert_called_once()

    def test_the_owner_and_the_dacl_are_both_evaluated(self):
        # An object's owner can always rewrite its permissions, so ownership is
        # checked before the DACL even when the DACL looks read-only.
        sid = wt._current_user_sid()
        if not sid:  # pragma: no cover - depends on the test machine
            self.skipTest("Windows did not report a SID for this process")
        with mock.patch.object(wt, "_open_standard_user_token", return_value=object()):
            with mock.patch.object(wt, "_close_token"):
                with mock.patch.object(wt, "_owner_sid_string", return_value=sid):
                    with mock.patch.object(wt, "_effective_access") as access:
                        result = wt.is_elevation_target_secure(self.exe)
        access.assert_not_called()
        self.assertFalse(result.is_secure)


# ---------------------------------------------------------
# 11c. Provisioning is refused for an unsafe target
# ---------------------------------------------------------
@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class TestProvisioningRefusesAnUnsafeTarget(ProvisioningDiagnosticsTestCase):
    def setUp(self):
        # super() redirects the provisioning diagnostics to a temporary folder.
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-unsafe-target-")
        self.exe = os.path.join(self.tmpdir, "OpenRGB.exe")
        with open(self.exe, "wb") as handle:
            handle.write(b"MZ")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_a_user_writable_target_is_never_provisioned(self):
        with mock.patch.object(
            wt, "is_elevation_target_secure", return_value=insecure_target()
        ) as check:
            with mock.patch.object(wt, "_run_schtasks") as run:
                with self.assertRaises(wt.WindowsTaskError) as caught:
                    wt.provision_openrgb_task(self.exe)

        run.assert_not_called()
        check.assert_called_once_with(os.path.abspath(self.exe))
        self.assertEqual(str(caught.exception), wt.UNSAFE_TARGET_MESSAGE)

    def test_an_unverifiable_target_is_never_provisioned(self):
        with mock.patch.object(
            wt,
            "is_elevation_target_secure",
            return_value=insecure_target(wt.SECURITY_UNVERIFIED),
        ):
            with mock.patch.object(wt, "_run_schtasks") as run:
                with self.assertRaises(wt.WindowsTaskError) as caught:
                    wt.provision_openrgb_task(self.exe)

        run.assert_not_called()
        self.assertEqual(str(caught.exception), wt.UNVERIFIED_TARGET_MESSAGE)

    def test_a_real_user_writable_target_is_refused_before_any_prompt(self):
        require_live_standard_user_token(self)
        # No mocks: the real ACL inspection of the test's own temporary folder.
        with mock.patch.object(wt, "is_process_elevated", return_value=False):
            with mock.patch.object(wt, "_shell_execute_ex_runas") as shell:
                ok, message = wt.request_elevated_openrgb_provisioning(self.exe)

        self.assertFalse(ok)
        self.assertEqual(message, wt.UNSAFE_TARGET_MESSAGE)
        self.assertIn("Program Files", message)
        shell.assert_not_called()

    def test_an_elevated_run_refuses_an_unsafe_target_too(self):
        require_live_standard_user_token(self)
        # No mocks for the security side: the real ACL inspection of the test's
        # own temporary folder decides, and the refusal happens before the task
        # would have been created in-process.
        with mock.patch.object(wt, "is_process_elevated", return_value=True):
            with mock.patch.object(wt, "provision_openrgb_task") as provision:
                ok, message = wt.request_elevated_openrgb_provisioning(self.exe)

        self.assertFalse(ok)
        self.assertEqual(message, wt.UNSAFE_TARGET_MESSAGE)
        provision.assert_not_called()

    def test_the_elevated_helper_reports_the_refusal(self):
        messages = []
        with mock.patch.object(
            wt, "is_elevation_target_secure", return_value=insecure_target()
        ):
            with mock.patch.object(wt, "_run_schtasks") as run:
                code = wt.run_provisioning_cli(
                    ["app.exe", wt.PROVISION_FLAG, self.exe], output=messages.append
                )

        # A refusal is the protected-location policy deciding, which is its own
        # failure class - not a generic provisioning failure.
        self.assertEqual(code, wt.PROVISION_EXIT_SECURITY)
        run.assert_not_called()
        self.assertTrue(
            any("can be modified by your normal user account" in text for text in messages),
            messages,
        )

    def test_a_refused_target_leaves_the_configuration_untouched(self):
        # Provisioning lives outside the configuration: a refusal must not write
        # anything anywhere.
        with mock.patch.object(
            wt, "is_elevation_target_secure", return_value=insecure_target()
        ):
            with self.assertRaises(wt.WindowsTaskError):
                wt.provision_openrgb_task(self.exe)

        self.assertTrue(os.path.isfile(self.exe))
        self.assertEqual(os.listdir(self.tmpdir), ["OpenRGB.exe"])


# ---------------------------------------------------------
# 12-13. Configuration-driven task lifecycle
# ---------------------------------------------------------
class TestTaskLifecyclePolicy(unittest.TestCase):
    A = r"C:\Program Files\OpenRGB\OpenRGB.exe"
    B = r"D:\Tools\OpenRGB\OpenRGB.exe"

    def test_changing_the_path_requests_a_refresh(self):
        self.assertEqual(
            wt.openrgb_task_action(True, self.A, True, self.B), wt.ACTION_PROVISION
        )

    def test_unchanged_path_requests_nothing(self):
        self.assertEqual(
            wt.openrgb_task_action(True, self.A, True, self.A), wt.ACTION_NONE
        )

    def test_path_case_change_requests_nothing(self):
        self.assertEqual(
            wt.openrgb_task_action(True, self.A, True, self.A.lower()), wt.ACTION_NONE
        )

    def test_newly_enabled_integration_requests_a_task(self):
        self.assertEqual(
            wt.openrgb_task_action(False, "", True, self.A), wt.ACTION_PROVISION
        )

    def test_disabling_the_integration_requests_removal(self):
        self.assertEqual(
            wt.openrgb_task_action(True, self.A, False, self.A), wt.ACTION_REMOVE
        )
        self.assertEqual(
            wt.openrgb_task_action(True, self.A, False, ""), wt.ACTION_REMOVE
        )

    def test_enabled_without_a_path_requests_nothing(self):
        self.assertEqual(
            wt.openrgb_task_action(True, self.A, True, "  "), wt.ACTION_NONE
        )

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_settings_save_syncs_the_task_for_a_new_path(self):
        stub = SettingsStub()
        stub.config = cm.default_config()
        stub.config["paths"]["openrgb"] = self.B
        stub.config["integrations"]["openrgb"]["enabled"] = True

        with mock.patch.object(
            app, "apply_openrgb_task_action", return_value=(True, "")
        ) as apply_action:
            stub._sync_openrgb_elevation_task((True, self.A))

        apply_action.assert_called_once_with(wt.ACTION_PROVISION, self.B)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_settings_save_removes_the_task_when_disabled(self):
        stub = SettingsStub()
        stub.config = disabled_openrgb_config(self.A)

        with mock.patch.object(
            app, "apply_openrgb_task_action", return_value=(True, "")
        ) as apply_action:
            stub._sync_openrgb_elevation_task((True, self.A))

        self.assertEqual(apply_action.call_args[0][0], wt.ACTION_REMOVE)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_settings_save_does_nothing_when_openrgb_is_unchanged(self):
        config = cm.default_config()
        config["paths"]["openrgb"] = self.A
        config["integrations"]["openrgb"]["enabled"] = True
        stub = SettingsStub()
        stub.config = config

        with mock.patch.object(
            app, "apply_openrgb_task_action", return_value=(True, "")
        ) as apply_action:
            stub._sync_openrgb_elevation_task((True, self.A))

        apply_action.assert_not_called()

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_settings_save_does_not_prompt_when_the_user_was_already_asked(self):
        stub = SettingsStub()
        stub.config = cm.default_config()
        stub.config["paths"]["openrgb"] = self.B
        stub.config["integrations"]["openrgb"]["enabled"] = True
        stub._confirm_openrgb_approval = lambda: False

        with mock.patch.object(
            app, "apply_openrgb_task_action", return_value=(True, "")
        ) as apply_action:
            stub._sync_openrgb_elevation_task((True, self.A))

        apply_action.assert_not_called()

    def test_removal_reports_success_when_there_is_nothing_to_remove(self):
        with mock.patch.object(wt, "query_openrgb_task", return_value=wt.TaskProbe()):
            self.assertFalse(wt.remove_openrgb_task())

    def test_removal_only_ever_targets_the_fixed_task_name(self):
        with mock.patch.object(
            wt, "query_openrgb_task", return_value=probe(self.A)
        ):
            with mock.patch.object(wt, "_run_schtasks", return_value=(0, "", "")) as run:
                self.assertTrue(wt.remove_openrgb_task())

        self.assertEqual(run.call_args[0][0], ["/delete", "/tn", wt.OPENRGB_TASK_NAME, "/f"])


# ---------------------------------------------------------
# 14. Provisioning failure never damages the configuration
# ---------------------------------------------------------
class TestProvisioningFailureIsNotFatal(unittest.TestCase):
    OPENRGB = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-provision-failure-")
        self.config_path = os.path.join(self.tmpdir, cm.CONFIG_FILENAME)
        config = cm.default_config()
        config["paths"]["openrgb"] = self.OPENRGB
        config["integrations"]["openrgb"]["enabled"] = True
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
        self.config = config

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def read_config(self):
        with open(self.config_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_a_raising_provisioner_is_reported_and_changes_nothing(self):
        before = self.read_config()

        def failing(_path):
            raise wt.WindowsTaskError("no approval")

        ok, message = wt.apply_openrgb_task_action(
            wt.ACTION_PROVISION, self.OPENRGB, provisioner=failing
        )
        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertEqual(self.read_config(), before)

    def test_a_declined_approval_is_reported_and_changes_nothing(self):
        before = self.read_config()
        ok, message = wt.apply_openrgb_task_action(
            wt.ACTION_PROVISION,
            self.OPENRGB,
            provisioner=lambda _path: (False, "approval declined"),
        )
        self.assertFalse(ok)
        self.assertEqual(message, "approval declined")
        self.assertEqual(self.read_config(), before)

    def test_a_failed_cleanup_still_leaves_the_integration_disabled(self):
        with mock.patch.object(
            wt, "query_openrgb_task", return_value=probe(self.OPENRGB)
        ):
            with mock.patch.object(
                wt, "_run_schtasks", return_value=(1, "ERROR: Access is denied.", "")
            ):
                ok, message = wt.apply_openrgb_task_action(wt.ACTION_REMOVE, "")

        self.assertFalse(ok)
        self.assertIn("stays disabled", message)
        # The configuration itself was never touched by a failed cleanup.
        self.assertTrue(cm.integration_enabled(self.config, "openrgb"))

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_settings_save_warns_but_does_not_raise_when_provisioning_fails(self):
        stub = SettingsStub()
        stub.config = self.config

        with mock.patch.object(
            app, "apply_openrgb_task_action", return_value=(False, "approval declined")
        ):
            with mock.patch.object(app.QMessageBox, "warning") as warning:
                stub._sync_openrgb_elevation_task((False, ""))
        warning.assert_called_once()


@unittest.skipIf(app is None, SKIP_REASON)
class TestSettingsRepairForAnUnsafeLocation(unittest.TestCase):
    """The repair button must explain an unsafe location instead of asking for UAC."""

    class RepairStub:
        _repair_openrgb_elevation = app.YeelightPCCompanionWindow._repair_openrgb_elevation

        def __init__(self, config, elevation_status):
            self.config = config
            self._status = elevation_status
            self.approval_requests = 0

        def _refresh_openrgb_elevation_status(self):
            return self._status

        def _confirm_openrgb_approval(self):
            self.approval_requests += 1
            return True

    def config(self):
        config = cm.default_config()
        config["paths"]["openrgb"] = r"C:\Users\someone\Downloads\OpenRGB.exe"
        config["integrations"]["openrgb"]["enabled"] = True
        return config

    def test_an_unsafe_location_shows_the_explanation_and_never_provisions(self):
        elevation_status = status(
            wt.STATUS_UNSAFE_TARGET, wt.UNSAFE_TARGET_LABEL, wt.UNSAFE_TARGET_MESSAGE
        )
        stub = self.RepairStub(self.config(), elevation_status)

        with mock.patch.object(app, "apply_openrgb_task_action") as apply_action:
            with mock.patch.object(app.QMessageBox, "warning") as warning:
                stub._repair_openrgb_elevation()

        apply_action.assert_not_called()
        self.assertEqual(stub.approval_requests, 0)
        warning.assert_called_once()
        self.assertIn("Program Files", warning.call_args[0][2])

    def test_a_ready_task_still_asks_for_approval(self):
        stub = self.RepairStub(self.config(), status(wt.STATUS_READY, "Ready"))

        with mock.patch.object(
            app, "apply_openrgb_task_action", return_value=(True, "ready")
        ) as apply_action:
            with mock.patch.object(app.QMessageBox, "information") as information:
                stub._repair_openrgb_elevation()

        self.assertEqual(stub.approval_requests, 1)
        apply_action.assert_called_once()
        information.assert_called_once()


@unittest.skipIf(wizard is None, WIZARD_SKIP_REASON)
class TestWizardElevationStep(unittest.TestCase):
    """The wizard provisions after the configuration has already been written."""

    OPENRGB = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    class WizardStub:
        _set_up_openrgb_elevation = (
            wizard.FirstRunWizard._set_up_openrgb_elevation if wizard is not None else None
        )

    def config(self):
        config = cm.default_config()
        config["paths"]["openrgb"] = self.OPENRGB
        config["integrations"]["openrgb"]["enabled"] = True
        return config

    def test_no_step_when_openrgb_is_disabled_or_unconfigured(self):
        with mock.patch.object(wizard, "apply_openrgb_task_action") as apply_action:
            for config in (cm.default_config(), disabled_openrgb_config(self.OPENRGB)):
                self.assertIsNone(self.WizardStub()._set_up_openrgb_elevation(config))
        apply_action.assert_not_called()

    def test_an_already_ready_task_does_not_ask_again(self):
        with mock.patch.object(
            wizard, "openrgb_elevation_status", return_value=status(wt.STATUS_READY, "Ready")
        ):
            with mock.patch.object(wizard.QMessageBox, "question") as question:
                self.assertIsNone(self.WizardStub()._set_up_openrgb_elevation(self.config()))
        question.assert_not_called()

    def test_declining_approval_returns_a_warning_and_saves_nothing(self):
        with mock.patch.object(
            wizard,
            "openrgb_elevation_status",
            return_value=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
        ):
            with mock.patch.object(
                wizard.QMessageBox,
                "question",
                return_value=wizard.QMessageBox.StandardButton.No,
            ):
                with mock.patch.object(wizard, "apply_openrgb_task_action") as apply_action:
                    warning = self.WizardStub()._set_up_openrgb_elevation(self.config())

        self.assertIn("could not be enabled", warning)
        self.assertIn("repair this later from the Integrations page", warning)
        apply_action.assert_not_called()

    def test_failed_provisioning_returns_the_same_warning(self):
        with mock.patch.object(
            wizard,
            "openrgb_elevation_status",
            return_value=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
        ):
            with mock.patch.object(
                wizard.QMessageBox,
                "question",
                return_value=wizard.QMessageBox.StandardButton.Yes,
            ):
                with mock.patch.object(
                    wizard, "apply_openrgb_task_action", return_value=(False, "access denied")
                ):
                    warning = self.WizardStub()._set_up_openrgb_elevation(self.config())

        self.assertIn("could not be enabled", warning)

    def test_an_unsafe_location_is_explained_without_asking_for_approval(self):
        with mock.patch.object(
            wizard,
            "openrgb_elevation_status",
            return_value=status(
                wt.STATUS_UNSAFE_TARGET, wt.UNSAFE_TARGET_LABEL, wt.UNSAFE_TARGET_MESSAGE
            ),
        ):
            with mock.patch.object(wizard.QMessageBox, "question") as question:
                with mock.patch.object(wizard, "apply_openrgb_task_action") as apply_action:
                    warning = self.WizardStub()._set_up_openrgb_elevation(self.config())

        question.assert_not_called()
        apply_action.assert_not_called()
        self.assertIn("Program Files", warning)
        self.assertIn("repair this later from the Integrations page", warning)

    def test_a_refusal_from_provisioning_is_shown_to_the_user(self):
        with mock.patch.object(
            wizard,
            "openrgb_elevation_status",
            return_value=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
        ):
            with mock.patch.object(
                wizard.QMessageBox,
                "question",
                return_value=wizard.QMessageBox.StandardButton.Yes,
            ):
                with mock.patch.object(
                    wizard,
                    "apply_openrgb_task_action",
                    return_value=(False, wt.UNSAFE_TARGET_MESSAGE),
                ):
                    warning = self.WizardStub()._set_up_openrgb_elevation(self.config())

        self.assertIn("could not be enabled", warning)
        self.assertIn("Program Files", warning)

    def test_successful_provisioning_reports_nothing(self):
        with mock.patch.object(
            wizard,
            "openrgb_elevation_status",
            return_value=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
        ):
            with mock.patch.object(
                wizard.QMessageBox,
                "question",
                return_value=wizard.QMessageBox.StandardButton.Yes,
            ):
                with mock.patch.object(
                    wizard, "apply_openrgb_task_action", return_value=(True, "ready")
                ) as apply_action:
                    warning = self.WizardStub()._set_up_openrgb_elevation(self.config())

        self.assertIsNone(warning)
        self.assertEqual(apply_action.call_args[0][0], wt.ACTION_PROVISION)


# ---------------------------------------------------------
# 20. Narrow, hardcoded provisioning command line
# ---------------------------------------------------------
class TestTaskPrincipalAndInvocation(unittest.TestCase):
    """The principal written into the task and the elevated re-invocation."""

    @unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
    def test_windows_reports_a_sid_for_the_current_user(self):
        sid = wt._current_user_sid()
        if not sid:
            self.skipTest("Windows did not report a SID for this process")
        self.assertTrue(sid.startswith("S-1-"), sid)

    def test_the_principal_falls_back_to_the_user_name(self):
        with mock.patch.object(wt, "_current_user_sid", return_value=""):
            with mock.patch.object(wt, "_current_user_name", return_value="DOMAIN\\user"):
                self.assertEqual(wt.current_task_principal(), "DOMAIN\\user")

    def test_the_self_invocation_uses_the_running_script(self):
        argv = wt._self_invocation([wt.REMOVE_FLAG])
        self.assertEqual(argv[-1], wt.REMOVE_FLAG)
        if not getattr(sys, "frozen", False):
            self.assertTrue(os.path.isfile(argv[1]), argv)
            self.assertTrue(argv[1].endswith(".py"), argv[1])

    def test_the_self_invocation_falls_back_to_the_main_module(self):
        fake_main = types.SimpleNamespace(__file__=__file__)
        with mock.patch.object(wt.sys, "argv", ["-c"]):
            with mock.patch.dict(sys.modules, {"__main__": fake_main}):
                argv = wt._self_invocation([wt.REMOVE_FLAG])
        self.assertEqual(argv[1], os.path.abspath(__file__))

    def test_the_self_invocation_for_a_packaged_build_uses_the_executable(self):
        with mock.patch.object(wt.sys, "frozen", True, create=True):
            argv = wt._self_invocation([wt.REMOVE_FLAG])
        self.assertEqual(argv, [sys.executable, wt.REMOVE_FLAG])

    def test_no_script_can_be_found_is_reported_not_raised_as_an_attribute_error(self):
        with mock.patch.object(wt.sys, "argv", []):
            with mock.patch.dict(sys.modules, {"__main__": types.SimpleNamespace()}):
                with self.assertRaises(wt.WindowsTaskError):
                    wt._self_invocation([wt.REMOVE_FLAG])


class TestProvisioningCommandLine(ProvisioningDiagnosticsTestCase):
    def test_plain_start_is_not_a_provisioning_invocation(self):
        self.assertIsNone(wt.parse_provisioning_argv(["app.exe"]))
        self.assertIsNone(wt.parse_provisioning_argv(["app.exe", "--tray"]))
        self.assertIsNone(
            wt.parse_provisioning_argv(["app.exe", "--no-automation", "--no-autorestore"])
        )

    def test_provision_requires_exactly_one_path(self):
        action, path = wt.parse_provisioning_argv(
            ["app.exe", "--provision-openrgb-task", r"C:\OpenRGB\OpenRGB.exe"]
        )
        self.assertEqual(action, wt.ACTION_PROVISION)
        self.assertEqual(path, r"C:\OpenRGB\OpenRGB.exe")

    def test_remove_takes_no_arguments(self):
        self.assertEqual(
            wt.parse_provisioning_argv(["app.exe", "--remove-openrgb-task"]),
            (wt.ACTION_REMOVE, ""),
        )

    def test_malformed_provisioning_lines_are_rejected(self):
        bad_lines = [
            ["app.exe", "--provision-openrgb-task"],
            ["app.exe", "--provision-openrgb-task", "a.exe", "b.exe"],
            ["app.exe", "--remove-openrgb-task", "extra"],
            ["app.exe", "--task-name", "other", "--provision-openrgb-task", "a.exe"],
            ["app.exe", "--provision-openrgb-task", "a.exe", "--arguments", "--anything"],
        ]
        for line in bad_lines:
            with self.assertRaises(wt.WindowsTaskError, msg=repr(line)):
                wt.parse_provisioning_argv(line)

    def test_cli_mode_runs_the_requested_action_and_never_raises(self):
        messages = []
        with mock.patch.object(wt, "provision_openrgb_task") as provision:
            with mock.patch.object(
                wt, "validate_openrgb_executable", return_value=r"C:\OpenRGB\OpenRGB.exe"
            ):
                with mock.patch.object(
                    wt, "openrgb_elevation_status", return_value=status(wt.STATUS_READY, "Ready")
                ):
                    code = wt.run_provisioning_cli(
                        ["app.exe", "--provision-openrgb-task", r"C:\OpenRGB\OpenRGB.exe"],
                        output=messages.append,
                    )
        self.assertEqual(code, 0)
        provision.assert_called_once_with(r"C:\OpenRGB\OpenRGB.exe")

    def test_cli_mode_returns_none_for_a_normal_start(self):
        self.assertIsNone(wt.run_provisioning_cli(["app.exe", "--tray"], output=lambda _m: None))

    def test_cli_mode_reports_a_bad_command_line_without_starting_the_app(self):
        messages = []
        code = wt.run_provisioning_cli(
            ["app.exe", "--remove-openrgb-task", "extra"], output=messages.append
        )
        self.assertEqual(code, 2)
        self.assertTrue(messages)

    def test_provisioning_mode_does_not_accept_a_non_executable_path(self):
        messages = []
        code = wt.run_provisioning_cli(
            ["app.exe", "--provision-openrgb-task", r"C:\evil.cmd"], output=messages.append
        )
        self.assertEqual(code, 1)


# ---------------------------------------------------------
# 19. Elevated provisioning request (ShellExecuteExW handshake)
# ---------------------------------------------------------
class TestElevatedProvisioningRequest(unittest.TestCase):
    OPENRGB = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def test_already_elevated_provisions_in_process_without_a_prompt(self):
        # This is a control-flow test, not a filesystem/ACL integration test.
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.OPENRGB
        ), mock.patch.object(
            wt, "is_elevation_target_secure", return_value=secure_target()
        ), mock.patch.object(
            wt, "is_process_elevated", return_value=True
        ), mock.patch.object(
            wt, "provision_openrgb_task"
        ) as provision, mock.patch.object(
            wt, "_shell_execute_ex_runas"
        ) as shell:
            ok, _message = wt.request_elevated_openrgb_provisioning(self.OPENRGB)

        self.assertTrue(ok)
        provision.assert_called_once()
        shell.assert_not_called()

    def test_a_raising_in_process_provisioning_never_propagates(self):
        # Keep the test focused on exception containment after validation has
        # succeeded; the configured example path need not exist on the runner.
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.OPENRGB
        ), mock.patch.object(
            wt, "is_elevation_target_secure", return_value=secure_target()
        ), mock.patch.object(
            wt, "is_process_elevated", return_value=True
        ), mock.patch.object(
            wt, "provision_openrgb_task", side_effect=RuntimeError("boom")
        ):
            ok, message = wt.request_elevated_openrgb_provisioning(self.OPENRGB)
        self.assertFalse(ok)
        self.assertIn("could not be created", message)

    def test_a_non_executable_path_is_rejected_before_any_prompt(self):
        with mock.patch.object(wt, "_shell_execute_ex_runas") as shell:
            ok, message = wt.request_elevated_openrgb_provisioning(r"C:\nope\evil.bat")
        self.assertFalse(ok)
        self.assertIn(".exe", message)
        shell.assert_not_called()


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class TestElevatedHandshake(unittest.TestCase):
    """The parent must know how the elevated helper ended, not guess.

    Every test here drives ``run_elevated_provisioning()`` with the launcher,
    the waiter, the task inspection and the result reader injected, so the real
    UAC prompt and the real Task Scheduler are never involved.
    """

    OPENRGB = r"C:\Program Files\OpenRGB\OpenRGB.exe"
    EXE = "yeelight.exe"
    ARGV = ["yeelight.exe", wt.PROVISION_FLAG, r"C:\Program Files\OpenRGB\OpenRGB.exe"]

    def setUp(self):
        self.results = {}
        patches = [
            mock.patch.object(wt, "_self_invocation", return_value=list(self.ARGV)),
            mock.patch.object(wt, "clear_provision_result", return_value=True),
            mock.patch.object(wt, "log_provisioning"),
            mock.patch.object(wt, "read_provision_result", side_effect=self._read_result),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _read_result(self, not_before=None):
        if not self.results:
            return None
        payload = dict(self.results)
        if not_before is not None:
            payload.setdefault("written_at", not_before + 1.0)
            if payload["written_at"] < not_before:
                return None
        return payload

    def _run(self, launcher, waiter, status=None, timeout=30.0, **kwargs):
        with mock.patch.object(
            wt, "openrgb_elevation_status", return_value=status
        ) as query:
            outcome = wt.run_elevated_provisioning(
                wt.ACTION_PROVISION,
                self.OPENRGB,
                timeout_seconds=timeout,
                launcher=launcher,
                waiter=waiter,
                **kwargs
            )
        return outcome, query

    # --- launch ---------------------------------------------------------
    def test_a_started_helper_is_launched_with_the_narrow_cli_and_its_handle_waited_on(self):
        captured = {}
        handle = object()

        def launcher(executable, arguments):
            captured["executable"] = executable
            captured["arguments"] = list(arguments)
            return 42, handle

        def waiter(process, _timeout):
            captured["waited"] = process
            captured["timeout"] = _timeout
            return wt.PROVISION_EXIT_OK

        outcome, _query = self._run(
            launcher, waiter, status=status(wt.STATUS_READY, "Ready")
        )

        self.assertTrue(outcome.ok)
        self.assertEqual(captured["executable"], self.EXE)
        # Only the path varies; the mode is one of the two hardcoded flags.
        self.assertEqual(captured["arguments"][0], wt.PROVISION_FLAG)
        self.assertEqual(captured["arguments"][1], self.OPENRGB)
        self.assertIs(captured["waited"], handle)
        self.assertEqual(captured["timeout"], 30.0)

    def test_a_declined_uac_is_reported_as_a_decline_not_a_failure(self):
        def launcher(_executable, _arguments):
            return wt.SE_ERR_ACCESSDENIED, None

        outcome, query = self._run(
            launcher, mock.Mock(return_value=7), status=status(wt.STATUS_NEEDS_SETUP, "Needs setup")
        )

        self.assertFalse(outcome.ok)
        self.assertIn("declined", outcome.message.lower())
        self.assertIsNone(outcome.exit_code)
        # Nothing was launched, so nothing may be waited on or inspected.
        query.assert_not_called()

    def test_a_launch_failure_is_reported_immediately(self):
        waited = mock.Mock()

        def launcher(_executable, _arguments):
            return 2, None

        outcome, query = self._run(
            launcher, waited, status=status(wt.STATUS_NEEDS_SETUP, "Needs setup")
        )

        self.assertFalse(outcome.ok)
        self.assertIn("could not be requested", outcome.message)
        self.assertIn("Windows error 2", outcome.message)
        waited.assert_not_called()
        query.assert_not_called()

    def test_launch_completes_without_a_usable_handle_is_a_failure(self):
        def launcher(_executable, _arguments):
            return 42, None  # "started" but no handle to wait on

        outcome, _query = self._run(
            launcher, mock.Mock(), status=status(wt.STATUS_NEEDS_SETUP, "Needs setup")
        )
        self.assertFalse(outcome.ok)
        self.assertIn("could not be requested", outcome.message)

    def test_a_raising_launcher_is_reported_not_raised(self):
        def launcher(_executable, _arguments):
            raise OSError("no shell")

        outcome, _query = self._run(
            launcher, mock.Mock(), status=status(wt.STATUS_NEEDS_SETUP, "Needs setup")
        )
        self.assertFalse(outcome.ok)
        self.assertIn("could not be requested", outcome.message)
        self.assertIn("OSError", outcome.message)

    # --- exit code ------------------------------------------------------
    def test_the_parent_reads_the_helper_exit_code(self):
        for code in (wt.PROVISION_EXIT_TASK_CREATION, wt.PROVISION_EXIT_FAILED):
            with self.subTest(code=code):
                outcome, _query = self._run(
                    lambda _e, _a: (42, object()),
                    lambda _p, _t: code,
                    status=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.exit_code, code)

    def test_a_helper_success_with_a_ready_task_is_a_success(self):
        outcome, query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_OK,
            status=status(wt.STATUS_READY, "Ready"),
        )

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message, "OpenRGB seamless elevated launch is ready.")
        query.assert_called_once_with(self.OPENRGB)

    def test_the_handle_is_always_closed(self):
        handle = mock.Mock()
        closers = []
        original = wt._close_process_handle

        def spy(candidate):
            closers.append(candidate)
            return original(candidate)

        with mock.patch.object(wt, "_close_process_handle", side_effect=spy):
            for waiter in (
                lambda _p, _t: wt.PROVISION_EXIT_OK,
                lambda _p, _t: None,
                mock.Mock(side_effect=RuntimeError("boom")),
            ):
                closers.clear()
                with self.subTest(waiter=waiter):
                    self._run(
                        lambda _e, _a: (42, handle),
                        waiter,
                        status=status(wt.STATUS_READY, "Ready"),
                    )
                    self.assertEqual(closers, [handle])

    # --- timeout --------------------------------------------------------
    def test_a_helper_that_never_finishes_is_reported_as_a_timeout(self):
        outcome, query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: None,  # WaitForSingleObject timed out
            status=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
            timeout=30.0,
        )

        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.exit_code)
        self.assertIn("did not finish", outcome.message)
        self.assertIn("30 seconds", outcome.message)
        # "Needs setup" is exactly the vague answer that must not be the report.
        self.assertNotIn("Needs setup", outcome.message)
        query.assert_called_once()

    def test_a_timeout_still_succeeds_when_the_task_turned_out_ready(self):
        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: None,
            status=status(wt.STATUS_READY, "Ready"),
        )
        self.assertTrue(outcome.ok)

    # --- the narrow result channel --------------------------------------
    def test_the_helper_diagnostic_is_shown_verbatim(self):
        detail = "Windows Task Scheduler rejected the task definition: bad XML."
        self.results = {"success": False, "message": detail}

        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_TASK_CREATION,
            status=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
        )

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.message, detail)

    def test_a_stale_result_is_ignored(self):
        # The helper wrote nothing; an older result must not become this run's
        # answer. The fallback then comes from the exit code.
        self.results = {"success": True, "message": "stale", "written_at": 0.0}

        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_SECURITY,
            status=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
            clock=lambda: 1000.0,
        )

        self.assertFalse(outcome.ok)
        self.assertNotEqual(outcome.message, "stale")
        self.assertIn("protected-location", outcome.message)

    def test_a_missing_result_file_still_gives_a_useful_exit_code_failure(self):
        self.results = {}

        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_TASK_CREATION,
            status=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
        )

        self.assertFalse(outcome.ok)
        self.assertIn("Task Scheduler rejected the task definition", outcome.message)
        self.assertIn("could not be configured", outcome.message)

    def test_the_result_file_is_cleared_before_the_helper_starts(self):
        order = []
        original = wt.clear_provision_result
        with mock.patch.object(
            wt, "clear_provision_result", side_effect=lambda: (order.append("clear"), True)[1]
        ):
            self._run(
                lambda _e, _a: (order.append("launch"), (42, object()))[1],
                lambda _p, _t: wt.PROVISION_EXIT_OK,
                status=status(wt.STATUS_READY, "Ready"),
            )
        self.assertEqual(order[0], "clear")
        self.assertIn("launch", order)
        self.assertIsNotNone(original)

    def test_an_unreadable_result_falls_back_to_the_exit_code(self):
        self.results = {"success": True, "message": "stale", "written_at": 1.0}

        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_OK,
            status=status(wt.STATUS_READY, "Ready"),
            clock=lambda: 1000.0,
        )
        # A stale "success" may not be trusted, but the task state still decides.
        self.assertTrue(outcome.ok)

    # --- child success but no usable task -------------------------------
    def test_helper_success_without_a_task_is_a_post_create_failure(self):
        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_OK,
            status=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
        )

        self.assertFalse(outcome.ok)
        self.assertIn("completed, but the task is not ready", outcome.message)
        self.assertIn("Needs setup", outcome.message)
        self.assertEqual(outcome.exit_code, 0)

    def test_helper_success_with_a_needs_repair_task_reports_that_exact_state(self):
        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_OK,
            status=status(wt.STATUS_NEEDS_REPAIR, "Needs repair"),
        )

        self.assertFalse(outcome.ok)
        self.assertIn("Needs repair", outcome.message)
        self.assertIn("completed, but the task is not ready", outcome.message)

    def test_an_unreadable_task_state_is_reported_as_such(self):
        outcome, _query = self._run(
            lambda _e, _a: (42, object()),
            lambda _p, _t: wt.PROVISION_EXIT_OK,
            status=None,
        )
        self.assertFalse(outcome.ok)
        self.assertIn("not ready", outcome.message)


class TestProvisionExitCodeReporting(ProvisioningDiagnosticsTestCase):
    OPENRGB = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def test_provisioning_refuses_a_user_writable_target_with_the_security_code(self):
        # A refusal happens inside provisioning, before any approval is
        # requested: no elevation attempt, no schtasks call, and its own code.
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.OPENRGB
        ):
            with mock.patch.object(
                wt, "is_elevation_target_secure", return_value=insecure_target()
            ):
                with mock.patch.object(wt, "_run_schtasks") as schtasks:
                    with mock.patch.object(wt, "_shell_execute_ex_runas") as shell:
                        code = wt.run_provisioning_cli(
                            ["app.exe", wt.PROVISION_FLAG, self.OPENRGB],
                            output=lambda _m: None,
                        )
        self.assertEqual(code, wt.PROVISION_EXIT_SECURITY)
        schtasks.assert_not_called()
        shell.assert_not_called()

    def test_provisioning_refuses_an_unverifiable_target_with_the_security_code(self):
        # "Could not be verified" is the other half of the same policy: it must
        # fail closed with the same class rather than ever creating the task.
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.OPENRGB
        ):
            with mock.patch.object(wt, "_open_standard_user_token", return_value=None):
                with mock.patch.object(wt, "_run_schtasks") as schtasks:
                    code = wt.run_provisioning_cli(
                        ["app.exe", wt.PROVISION_FLAG, self.OPENRGB],
                        output=lambda _m: None,
                    )
        self.assertEqual(code, wt.PROVISION_EXIT_SECURITY)
        schtasks.assert_not_called()

    def test_a_rejected_task_definition_uses_the_task_creation_code(self):
        with mock.patch.object(wt, "validate_openrgb_executable", return_value=self.OPENRGB):
            with mock.patch.object(wt, "is_elevation_target_secure", return_value=secure_target()):
                with mock.patch.object(
                    wt,
                    "provision_openrgb_task",
                    side_effect=wt.WindowsTaskError("schtasks said no"),
                ):
                    code = wt.run_provisioning_cli(
                        ["app.exe", wt.PROVISION_FLAG, self.OPENRGB], output=lambda _m: None
                    )
        self.assertEqual(code, wt.PROVISION_EXIT_TASK_CREATION)

    def test_a_created_but_unready_task_uses_the_not_ready_code(self):
        with mock.patch.object(wt, "validate_openrgb_executable", return_value=self.OPENRGB):
            with mock.patch.object(wt, "is_elevation_target_secure", return_value=secure_target()):
                with mock.patch.object(wt, "provision_openrgb_task"):
                    with mock.patch.object(
                        wt,
                        "openrgb_elevation_status",
                        return_value=status(wt.STATUS_NEEDS_REPAIR, "Needs repair"),
                    ):
                        code = wt.run_provisioning_cli(
                            ["app.exe", wt.PROVISION_FLAG, self.OPENRGB],
                            output=lambda _m: None,
                        )
        self.assertEqual(code, wt.PROVISION_EXIT_NOT_READY)

    def test_a_malformed_command_line_uses_the_bad_command_line_code(self):
        code = wt.run_provisioning_cli(
            ["app.exe", wt.PROVISION_FLAG], output=lambda _m: None
        )
        self.assertEqual(code, wt.PROVISION_EXIT_BAD_COMMAND_LINE)

    def test_a_failed_removal_uses_the_task_creation_code(self):
        with mock.patch.object(
            wt, "remove_openrgb_task", side_effect=wt.WindowsTaskError("denied")
        ):
            code = wt.run_provisioning_cli(
                ["app.exe", wt.REMOVE_FLAG], output=lambda _m: None
            )
        self.assertEqual(code, wt.PROVISION_EXIT_TASK_CREATION)

    def test_a_successful_removal_is_a_success(self):
        with mock.patch.object(wt, "remove_openrgb_task", return_value=True):
            code = wt.run_provisioning_cli(
                ["app.exe", wt.REMOVE_FLAG], output=lambda _m: None
            )
        self.assertEqual(code, wt.PROVISION_EXIT_OK)

    def test_the_exit_codes_are_distinct(self):
        codes = [
            wt.PROVISION_EXIT_OK,
            wt.PROVISION_EXIT_FAILED,
            wt.PROVISION_EXIT_BAD_COMMAND_LINE,
            wt.PROVISION_EXIT_SECURITY,
            wt.PROVISION_EXIT_TASK_CREATION,
            wt.PROVISION_EXIT_NOT_READY,
        ]
        self.assertEqual(len(set(codes)), len(codes))
        # Every code that means "something failed" must have honest wording of
        # its own, so a missing result file never turns into a vague message.
        for code in codes[1:]:
            with self.subTest(code=code):
                self.assertTrue(wt.PROVISION_EXIT_MESSAGES[code].strip())


class TestProvisioningResultChannel(unittest.TestCase):
    """The child -> parent result file: fixed name, diagnostics only, transient."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-provision-result-")
        # Redirected per test so nothing is written beside the real application
        # data, and the inspection of the real path helper stays visible.
        patch = mock.patch.object(wt, "provisioning_data_dir", return_value=self.tmpdir)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_the_path_is_fixed_inside_the_application_data_directory(self):
        self.assertEqual(
            wt.provision_result_path(),
            os.path.join(self.tmpdir, wt.PROVISION_RESULT_FILENAME),
        )

    def test_a_result_round_trips_with_only_the_narrow_fields(self):
        self.assertTrue(wt.write_provision_result(False, "no good", exit_code=4))
        result = wt.read_provision_result()

        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "no good")
        self.assertEqual(result["exit_code"], 4)
        payload = json.load(open(wt.provision_result_path(), encoding="utf-8"))
        # Nothing beyond the narrow contract may be persisted.
        self.assertEqual(set(payload), {"success", "message", "exit_code"})

    def test_a_stale_result_is_rejected_by_its_timestamp(self):
        wt.write_provision_result(True, "old attempt")
        self.assertIsNotNone(wt.read_provision_result(not_before=0))
        self.assertIsNone(wt.read_provision_result(not_before=time.time() + 60))

    def test_clearing_removes_the_file_and_is_idempotent(self):
        wt.write_provision_result(True, "done")
        self.assertTrue(os.path.isfile(wt.provision_result_path()))

        self.assertTrue(wt.clear_provision_result())
        self.assertFalse(os.path.exists(wt.provision_result_path()))
        self.assertTrue(wt.clear_provision_result())  # nothing left to remove

    def test_a_missing_file_reads_as_no_result(self):
        self.assertIsNone(wt.read_provision_result())

    def test_malformed_json_is_never_reported_as_an_outcome(self):
        with open(wt.provision_result_path(), "w", encoding="utf-8") as stream:
            stream.write("{not json")
        self.assertIsNone(wt.read_provision_result())

    def test_json_without_the_contract_field_is_ignored(self):
        with open(wt.provision_result_path(), "w", encoding="utf-8") as stream:
            json.dump({"message": "no success key"}, stream)
        self.assertIsNone(wt.read_provision_result())

    def test_writing_never_raises_when_the_location_is_unusable(self):
        with mock.patch.object(wt, "provisioning_data_dir", return_value=""):
            self.assertFalse(wt.write_provision_result(True, "x"))
            self.assertIsNone(wt.read_provision_result())
            self.assertTrue(wt.clear_provision_result())

    def test_writing_never_raises_on_an_io_error(self):
        with mock.patch("builtins.open", side_effect=OSError("denied")):
            self.assertFalse(wt.write_provision_result(True, "x"))

    def test_no_data_directory_means_no_path_at_all(self):
        with mock.patch.object(wt, "provisioning_data_dir", return_value=""):
            self.assertEqual(wt.provision_result_path(), "")
            self.assertEqual(wt.provisioning_log_path(), "")


class TestProvisioningDiagnosticsLocation(unittest.TestCase):
    """Where the diagnostics live, and what they must never contain."""

    def test_the_paths_come_from_the_environment_never_a_user_name(self):
        for function in (wt.provisioning_data_dir, wt.log_provisioning):
            with self.subTest(function=function.__name__):
                source = inspect.getsource(function)
                self.assertNotIn("Marko", source)
                self.assertNotIn("C:\\Users\\", source)
        self.assertIn("LOCALAPPDATA", inspect.getsource(wt.provisioning_data_dir))

    def test_both_files_share_the_application_data_directory(self):
        directory = wt.provisioning_data_dir()
        self.assertTrue(directory)
        for path in (wt.provision_result_path(), wt.provisioning_log_path()):
            self.assertEqual(os.path.dirname(path), directory)

    def test_the_result_file_name_is_fixed(self):
        self.assertTrue(wt.provision_result_path().endswith(wt.PROVISION_RESULT_FILENAME))
        self.assertTrue(wt.provisioning_log_path().endswith(wt.PROVISIONING_LOG_FILENAME))


class TestShellExecuteExLauncher(unittest.TestCase):
    """The launcher wrapper: documented struct, correct verb, handle returned.

    The real API is mocked here, so no UAC prompt is ever shown and no elevated
    process is started by the unit suite.
    """

    EXE = r"C:\Program Files\Yeelight\yeelight.exe"
    ARGS = [wt.PROVISION_FLAG, r"C:\Program Files\OpenRGB\OpenRGB.exe"]

    def _start_fake_shell(self, started=True, last_error=0, handle=0):
        self.info = {}
        fake = mock.Mock()
        fake.ShellExecuteExW.side_effect = self._record
        self.started = started
        self.handle = handle

        patches = [
            mock.patch.object(wt.ctypes, "WinDLL", return_value=fake),
            mock.patch.object(wt.ctypes, "get_last_error", return_value=last_error),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _record(self, structure):
        self.info["structure"] = structure._obj
        if self.started and self.handle:
            structure._obj.hProcess = self.handle
        return self.started

    def test_the_callback_style_is_requested_so_a_process_handle_comes_back(self):
        # SEE_MASK_NOCLOSEPROCESS is the whole point: without it the parent gets
        # no handle, no exit code, and is back to guessing.
        self._start_fake_shell()
        wt._shell_execute_ex_runas(self.EXE, self.ARGS)

        structure = self.info["structure"]
        self.assertTrue(structure.fMask & wt.SEE_MASK_NOCLOSEPROCESS)
        self.assertEqual(structure.lpVerb, "runas")
        self.assertEqual(structure.lpFile, self.EXE)
        self.assertEqual(structure.nShow, wt.SW_SHOWNORMAL)
        self.assertEqual(structure.cbSize, ctypes.sizeof(wt._SHELLEXECUTEINFOW))

    def test_the_arguments_travel_as_one_command_line_with_the_path_quoted(self):
        self._start_fake_shell()
        wt._shell_execute_ex_runas(self.EXE, self.ARGS)

        parameters = self.info["structure"].lpParameters
        self.assertIn(wt.PROVISION_FLAG, parameters)
        # The path contains spaces, so it must arrive quoted - never split.
        self.assertIn('"C:\\Program Files\\OpenRGB\\OpenRGB.exe"', parameters)

    def test_the_directory_is_the_executables_own_folder(self):
        self._start_fake_shell()
        wt._shell_execute_ex_runas(self.EXE, self.ARGS)
        self.assertEqual(self.info["structure"].lpDirectory, os.path.dirname(self.EXE))

    def test_a_started_helper_yields_a_handle_the_caller_owns(self):
        handle = 0x1234
        self._start_fake_shell(handle=handle)

        result, returned = wt._shell_execute_ex_runas(self.EXE, self.ARGS)

        self.assertEqual(result, 42)
        self.assertEqual(returned, handle)
        self.assertIsNotNone(self.info["structure"])

    def test_a_started_helper_without_a_handle_is_still_reported_as_started(self):
        self._start_fake_shell()
        result, handle = wt._shell_execute_ex_runas(self.EXE, self.ARGS)
        self.assertEqual(result, 42)
        self.assertIsNone(handle)

    def test_a_declined_uac_maps_to_the_declined_code(self):
        self._start_fake_shell(started=False, last_error=wt.ERROR_CANCELLED)
        result, handle = wt._shell_execute_ex_runas(self.EXE, self.ARGS)
        self.assertEqual(result, wt.SE_ERR_ACCESSDENIED)
        self.assertIsNone(handle)

    def test_another_launch_error_is_reported_as_a_failure_code(self):
        self._start_fake_shell(started=False, last_error=2)
        result, handle = wt._shell_execute_ex_runas(self.EXE, self.ARGS)
        self.assertLessEqual(result, 32)
        self.assertEqual(result, 2)
        self.assertIsNone(handle)

    def test_a_raising_shell_is_reported_not_propagated(self):
        # No helper is started here, so this test does not need the fake shell.
        with mock.patch("ctypes.WinDLL", side_effect=OSError("no shell32")):
            result, handle = wt._shell_execute_ex_runas(self.EXE, self.ARGS)
        self.assertEqual(result, -1)
        self.assertIsNone(handle)

    def test_the_structure_matches_the_documented_layout(self):
        # cbSize is validated by Windows, so the field list must not drift.
        names = [name for name, _type in wt._SHELLEXECUTEINFOW._fields_]
        for required in (
            "cbSize",
            "fMask",
            "lpVerb",
            "lpFile",
            "lpParameters",
            "lpDirectory",
            "nShow",
            "hProcess",
        ):
            with self.subTest(field=required):
                self.assertIn(required, names)
        self.assertEqual(names[-1], "hProcess")


class _FakeKernel:
    """A kernel32 stand-in whose API attributes accept argtypes assignment.

    ``_wait_for_process`` declares ``argtypes`` on every function it calls, so
    the stand-in exposes small writable objects instead of plain functions.
    """

    class _Entry:
        def __init__(self, function):
            self.function = function
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.function(*args)

    def __init__(self, wait_results, exit_code=0, get_ok=True):
        self.calls = {"closed": 0, "waited": [], "terminated": 0}
        self._wait_results = list(wait_results)
        self._exit_code = exit_code
        self._get_ok = get_ok
        self.WaitForSingleObject = self._Entry(self._wait_for_single_object)
        self.GetExitCodeProcess = self._Entry(self._get_exit_code)
        self.TerminateProcess = self._Entry(self._terminate)
        self.CloseHandle = self._Entry(self._close)

    def _wait_for_single_object(self, _handle, milliseconds):
        self.calls["waited"].append(milliseconds)
        if len(self._wait_results) > 1:
            return self._wait_results.pop(0)
        return self._wait_results[0]

    def _get_exit_code(self, _handle, out):
        if not self._get_ok:
            return False
        out._obj.value = self._exit_code
        return True

    def _terminate(self, _handle, _code):
        self.calls["terminated"] += 1
        return True

    def _close(self, _handle):
        self.calls["closed"] += 1
        return True


class TestProcessHandleLifecycle(unittest.TestCase):
    """Waiting for the helper and always closing its handle."""

    def _start_kernel(self, wait_results, exit_code=0, get_ok=True):
        kernel = _FakeKernel(wait_results, exit_code=exit_code, get_ok=get_ok)
        patch = mock.patch.object(wt.ctypes, "WinDLL", return_value=kernel)
        patch.start()
        self.addCleanup(patch.stop)
        return kernel

    def test_a_finished_helper_yields_its_exit_code(self):
        kernel = self._start_kernel([wt.WAIT_OBJECT_0], exit_code=4)
        self.assertEqual(wt._wait_for_process(0x1, 30.0), 4)
        # The timeout is passed through in milliseconds.
        self.assertEqual(kernel.calls["waited"], [30000])

    def test_a_timeout_yields_no_exit_code(self):
        kernel = self._start_kernel([wt.WAIT_TIMEOUT])
        self.assertIsNone(wt._wait_for_process(0x1, 30.0))
        # The first wait uses the caller's timeout; the grace waits follow it.
        self.assertEqual(kernel.calls["waited"][0], 30000)
        self.assertTrue(all(value > 0 for value in kernel.calls["waited"]))

    def test_a_helper_that_finishes_during_the_grace_period_is_not_terminated(self):
        kernel = self._start_kernel([wt.WAIT_TIMEOUT, wt.WAIT_OBJECT_0])
        self.assertIsNone(wt._wait_for_process(0x1, 0.01))
        self.assertEqual(kernel.calls["terminated"], 0)

    def test_a_genuinely_stuck_helper_is_terminated_before_giving_up(self):
        kernel = self._start_kernel([wt.WAIT_TIMEOUT])
        wt._wait_for_process(0x1, 0.01)
        self.assertEqual(kernel.calls["terminated"], 1)

    def test_a_failed_exit_code_read_yields_no_code(self):
        self._start_kernel([wt.WAIT_OBJECT_0], get_ok=False)
        self.assertIsNone(wt._wait_for_process(0x1, 30.0))

    def test_an_unexpected_wait_result_yields_no_code(self):
        self._start_kernel([wt.WAIT_FAILED])
        self.assertIsNone(wt._wait_for_process(0x1, 30.0))

    def test_a_missing_handle_is_not_waited_on(self):
        with mock.patch.object(wt.ctypes, "WinDLL") as windll:
            self.assertIsNone(wt._wait_for_process(None, 30.0))
        windll.assert_not_called()

    def test_closing_a_handle_is_idempotent_and_safe(self):
        kernel = self._start_kernel([wt.WAIT_OBJECT_0])
        wt._close_process_handle(None)  # no handle: nothing to close
        self.assertEqual(kernel.calls["closed"], 0)
        wt._close_process_handle(0x1)
        self.assertEqual(kernel.calls["closed"], 1)

    def test_closing_never_raises_when_kernel32_is_unavailable(self):
        with mock.patch.object(wt.ctypes, "WinDLL", side_effect=OSError("nope")):
            wt._close_process_handle(0x1)  # must not raise


class TestProvisionOutcome(unittest.TestCase):
    def test_it_unpacks_like_the_old_tuple(self):
        ok, message = wt.ProvisionOutcome(True, "ready", 0)
        self.assertTrue(ok)
        self.assertEqual(message, "ready")

    def test_it_keeps_the_exit_code_visible(self):
        outcome = wt.ProvisionOutcome(False, "nope", 4)
        self.assertEqual(outcome.exit_code, 4)
        self.assertFalse(outcome.ok)

    def test_a_successful_outcome_reports_success(self):
        outcome = wt.ProvisionOutcome(True, "ready", 0)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.exit_code, 0)


class TestProvisioningLog(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-provision-log-")
        patch = mock.patch.object(wt, "provisioning_data_dir", return_value=self.tmpdir)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_the_log_lands_in_the_application_data_directory(self):
        wt.log_provisioning("Helper exited with code 4.")
        path = wt.provisioning_log_path()

        self.assertEqual(path, os.path.join(self.tmpdir, wt.PROVISIONING_LOG_FILENAME))
        self.assertIn("Helper exited with code 4.", open(path, encoding="utf-8").read())

    def test_logging_never_raises_when_the_location_is_unusable(self):
        with mock.patch.object(wt, "provisioning_data_dir", return_value=""):
            wt.log_provisioning("nothing to log into")  # must not raise

    def test_logging_never_raises_on_an_io_error(self):
        with mock.patch("builtins.open", side_effect=OSError("denied")):
            wt.log_provisioning("nothing to log into")

    def test_the_log_stays_bounded(self):
        path = wt.provisioning_log_path()
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("x" * (wt.PROVISION_LOG_MAX_BYTES + 1))

        wt.log_provisioning("after the cap")

        text = open(path, encoding="utf-8").read()
        self.assertLessEqual(len(text), wt.PROVISION_LOG_MAX_BYTES + 200)
        self.assertIn("after the cap", text)

    def test_no_secret_is_ever_logged_by_the_provisioning_paths(self):
        # The reasons that may reach the log are task state and file names only.
        for forbidden in ("latitude", "longitude", "ip_address", "192.168"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, inspect.getsource(wt.log_provisioning))


# ---------------------------------------------------------
# 19b. The elevated-helper branch of the protected-location check
# ---------------------------------------------------------
class TestLinkedTokenBranch(unittest.TestCase):
    """The branch that only runs inside the elevated helper.

    This is the code path that used to fail unnoticed: the helper duplicated the
    UAC linked token demanding ``SecurityImpersonation``, which Windows refuses
    with ``ERROR_BAD_IMPERSONATION_LEVEL`` (1346), so the whole
    protected-location check answered "could not be verified" inside the helper
    while it worked fine unelevated. Provisioning then refused every target, the
    helper exited with a failure and the parent could only report "not ready".

    The live fix was verified by running the helper elevated for real; these
    tests pin the level fallback so a regression cannot creep back in.
    """

    class FakeAdvapi:
        """Stands in for the advapi32 namespace: only DuplicateToken is used."""

        def __init__(self, accepted_levels=()):
            self.accepted = set(accepted_levels)
            self.calls = []

        def DuplicateToken(self, _source, level, out):
            self.calls.append(level)
            if level not in self.accepted:
                return False
            ctypes.cast(out, ctypes.POINTER(ctypes.wintypes.HANDLE)).contents.value = 0xBEEF
            return True

    def test_the_linked_token_is_accepted_at_identification_level(self):
        # Exactly what Windows does for the UAC linked token: impersonation is
        # refused, identification succeeds. The fallback must produce a handle.
        advapi32 = self.FakeAdvapi(accepted_levels=(wt.SECURITY_IDENTIFICATION,))

        token = wt._duplicate_token_at_usable_level(advapi32, 0x999)

        self.assertEqual(token.value, 0xBEEF)
        self.assertEqual(
            advapi32.calls, [wt.SECURITY_IMPERSONATION, wt.SECURITY_IDENTIFICATION]
        )

    def test_the_higher_level_is_preferred_when_the_token_supports_it(self):
        # The unelevated own token carries the impersonation level, so nothing
        # changes for that branch.
        advapi32 = self.FakeAdvapi(accepted_levels=(wt.SECURITY_IMPERSONATION,))

        token = wt._duplicate_token_at_usable_level(advapi32, 0x999)

        self.assertEqual(token.value, 0xBEEF)
        self.assertEqual(advapi32.calls, [wt.SECURITY_IMPERSONATION])

    def test_no_accepted_level_yields_no_token(self):
        advapi32 = self.FakeAdvapi(accepted_levels=())
        self.assertIsNone(wt._duplicate_token_at_usable_level(advapi32, 0x999))

    def test_a_successful_call_without_a_handle_is_not_a_token(self):
        class Handleless:
            def DuplicateToken(self, _source, _level, _out):
                return True  # claims success but writes nothing

        self.assertIsNone(wt._duplicate_token_at_usable_level(Handleless(), 0x999))

    def test_the_linked_token_handle_is_closed_on_every_path(self):
        # Both the primary token and the linked token are closed: the linked
        # handle is a resource this process newly owns and it is only used as a
        # source for the duplicate.
        source = inspect.getsource(wt._open_standard_user_token)
        self.assertIn("kernel32.CloseHandle(linked)", source)
        self.assertIn("kernel32.CloseHandle(primary)", source)
        self.assertIn("if linked is not None:", source)

    def test_an_elevated_process_without_a_linked_token_fails_closed(self):
        # UAC disabled: there is no limited token, so no protected-location
        # claim can be made and provisioning must refuse rather than trust the
        # elevated identity.
        source = inspect.getsource(wt._open_standard_user_token)
        self.assertIn("TOKEN_LINKED_TOKEN_CLASS", source)
        self.assertIn("return None", source)
        # The elevation state decides the branch, not a group-membership guess.
        self.assertIn("_read_process_elevation()", source)

    def test_the_live_unelevated_branch_still_produces_a_token(self):
        # Real Windows call, no elevation, no writes. Some CI/service accounts
        # have no linked limited token at all, which is an environment property
        # rather than an application failure.
        token = wt._open_standard_user_token()
        if not token:
            self.skipTest("this Windows account exposes no usable standard-user token")
        wt._close_token(token)

    def test_the_live_check_still_separates_protected_from_writable(self):
        require_live_standard_user_token(self)
        tmpdir = tempfile.mkdtemp(prefix="yeelight-linked-token-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        writable = os.path.join(tmpdir, "OpenRGB.exe")
        with open(writable, "wb") as handle:
            handle.write(b"MZ")

        protected = os.path.join(
            os.environ.get("SystemRoot") or r"C:\Windows", "System32", "cmd.exe"
        )
        self.assertFalse(wt.is_elevation_target_secure(writable).is_secure)
        if os.path.isfile(protected):
            self.assertTrue(wt.is_elevation_target_secure(protected).is_secure)


# ---------------------------------------------------------
# 14a. The environment handed to installed external programs
# ---------------------------------------------------------
def spawn_stub(record, error=None, on_spawn=None):
    """A `subprocess.Popen` stand-in that records how it was called."""

    def builder(command, **kwargs):
        record.append((command, kwargs))
        if on_spawn is not None:
            on_spawn(command, kwargs)
        if error is not None:
            raise error
        return types.SimpleNamespace(pid=4242)

    return builder


class TestExternalProcessEnvironment(unittest.TestCase):
    """The child environment handed to installed third-party programs."""

    BUNDLE = r"C:\Games\MyApp\_internal"
    BUNDLE_MARKER = "_MEIPASS"
    QT_BIN = r"C:\Games\MyApp\_internal\PyQt6\Qt6\bin"
    WIN32 = r"C:\Games\MyApp\_internal\pywin32_system32"
    SIMILAR = r"C:\Games\MyApp\_internal_backup"
    SIBLING = r"C:\Games\MyApp\internal"
    SYSTEM32 = r"C:\Windows\System32"

    def environment_with(self, *entries):
        return {"PATH": os.pathsep.join(entries), "KEEP_ME": "yes"}

    def bundle(self):
        return mock.patch.object(app.sys, self.BUNDLE_MARKER, self.BUNDLE, create=True)

    def test_bundle_entries_are_removed_from_the_child_path(self):
        base = self.environment_with(self.BUNDLE, self.QT_BIN, self.SYSTEM32, self.WIN32)

        with self.bundle():
            environment = app.external_process_environment(base)

        self.assertEqual(environment["PATH"].split(os.pathsep), [self.SYSTEM32])
        self.assertEqual(environment["KEEP_ME"], "yes")

    def test_unrelated_entries_are_preserved(self):
        unrelated = [
            self.SYSTEM32,
            r"C:\Program Files\Razer",
            r"C:\Program Files (x86)\Artemis",
            self.SIMILAR,  # only looks similar: not inside the bundle
            self.SIBLING,  # a plain sibling directory
        ]
        base = self.environment_with(self.BUNDLE, *unrelated)

        with self.bundle():
            environment = app.external_process_environment(base)

        self.assertEqual(environment["PATH"].split(os.pathsep), unrelated)

    def test_source_runs_do_not_alter_the_environment(self):
        base = self.environment_with(self.BUNDLE, self.SYSTEM32)

        with mock.patch.object(app.sys, self.BUNDLE_MARKER, None, create=True):
            environment = app.external_process_environment(base)

        self.assertEqual(environment, base)

    def test_an_absent_path_is_not_invented(self):
        with self.bundle():
            environment = app.external_process_environment({"KEEP_ME": "yes"})

        self.assertNotIn("PATH", environment)

    def test_path_components_are_compared_as_paths_not_text(self):
        # A substring test would wrongly treat `_internal_backup` as being
        # inside the bundle; a case-sensitive test would wrongly keep a
        # differently-cased spelling of the bundle directory.
        cases = [
            (self.BUNDLE, True),
            (self.BUNDLE.lower(), True),
            (self.BUNDLE.upper(), True),
            (self.QT_BIN, True),
            (self.BUNDLE + "\\", True),
            (os.path.join(self.BUNDLE, "nested", "deeper"), True),
            (self.SIMILAR, False),  # only looks similar
            (self.SIBLING, False),  # a plain sibling directory
            (self.SYSTEM32, False),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(app._is_inside(path, self.BUNDLE), expected)


@unittest.skipIf(app is None, SKIP_REASON)
class TestExternalProcessLaunchSanitation(unittest.TestCase):
    """`launch_process` is the single gate for installed external programs."""

    BUNDLE = TestExternalProcessEnvironment.BUNDLE
    BUNDLE_MARKER = TestExternalProcessEnvironment.BUNDLE_MARKER
    PREVIOUS_DLL_DIR = r"C:\Games\MyApp\_internal"

    def setUp(self):
        # The real launch gate needs an existing executable on disk, so each
        # test gets a real temporary file to point it at.
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-external-launch-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.external = os.path.join(self.tmpdir, "Installed App.exe")
        with open(self.external, "wb") as handle:
            handle.write(b"MZ")

        self.calls = []
        self.record = []
        self.patch(app, "_set_dll_directory", self.record_set)
        self.patch(app, "_get_dll_directory", lambda: self.PREVIOUS_DLL_DIR)
        self.patch(app.subprocess, "Popen", spawn_stub(self.record))

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record_set(self, directory):
        self.calls.append(("set", directory))
        return True

    def bundle(self):
        return mock.patch.object(app.sys, self.BUNDLE_MARKER, self.BUNDLE, create=True)

    def launch(self, path=None, **kwargs):
        stub = types.SimpleNamespace(
            running=True,
            progress_update=types.SimpleNamespace(emit=lambda message: None),
        )
        app.RestoreEngineThread.launch_process(stub, path or self.external, **kwargs)
        return stub

    def assert_no_bundle_entries(self, environment):
        entries = environment["PATH"].split(os.pathsep)
        self.assertEqual(
            [entry for entry in entries if app._is_inside(entry, self.BUNDLE)], [], entries
        )

    def test_the_dll_directory_is_cleared_for_the_spawn_and_restored(self):
        self.launch()

        self.assertEqual(self.calls, [("set", None), ("set", self.PREVIOUS_DLL_DIR)])
        self.assertEqual(len(self.record), 1)

    def test_the_child_is_started_while_the_dll_directory_is_cleared(self):
        # Snapshot both the DLL-directory history and the child's PATH at the
        # exact moment the child is created.
        snapshot = {}

        def on_spawn(_command, kwargs):
            snapshot["calls"] = list(self.calls)
            snapshot["PATH"] = kwargs["env"]["PATH"]

        self.patch(app.subprocess, "Popen", spawn_stub(self.record, on_spawn=on_spawn))

        with self.bundle():
            self.launch()

        # Exactly one change had happened when the child was created: the clear.
        self.assertEqual(snapshot["calls"], [("set", None)])
        # ...and the child's PATH was already sanitized.
        entries = snapshot["PATH"].split(os.pathsep)
        self.assertEqual(
            [entry for entry in entries if app._is_inside(entry, self.BUNDLE)], [], entries
        )
        # The previous value is put back once the spawn is done.
        self.assertEqual(self.calls, [("set", None), ("set", self.PREVIOUS_DLL_DIR)])

    def test_a_failing_spawn_still_restores_the_dll_directory(self):
        self.patch(app.subprocess, "Popen", spawn_stub(self.record, error=OSError("boom")))

        with self.assertRaises(OSError):
            self.launch()

        self.assertEqual(self.calls, [("set", None), ("set", self.PREVIOUS_DLL_DIR)])

    def test_cancelled_restores_never_spawn_anything(self):
        stub = types.SimpleNamespace(
            running=False,
            progress_update=types.SimpleNamespace(emit=lambda message: None),
        )
        app.RestoreEngineThread.launch_process(stub, self.external)

        self.assertEqual(self.record, [])
        self.assertEqual(self.calls, [])

    def test_the_previous_value_is_read_back_not_assumed(self):
        self.launch()

        self.assertIn(("set", self.PREVIOUS_DLL_DIR), self.calls)
        # Nothing in the launch path assumes the bundle directory.
        self.assertNotIn("_MEIPASS", inspect.getsource(app.external_process_environment_scope))

    def test_the_clear_and_spawn_section_is_lock_protected(self):
        events = []

        class TracingLock:
            def acquire(self):
                events.append("acquire")

            def release(self):
                events.append("release")

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc_info):
                self.release()
                return False

        self.patch(app, "_LAUNCH_LOCK", TracingLock())
        fake_subprocess = types.SimpleNamespace(
            Popen=lambda command, **kwargs: (
                events.append("spawn") or types.SimpleNamespace(pid=1)
            ),
            STARTUPINFO=subprocess.STARTUPINFO,
            STARTF_USESHOWWINDOW=subprocess.STARTF_USESHOWWINDOW,
        )
        self.patch(app, "subprocess", fake_subprocess)

        self.launch()

        self.assertEqual(events[0], "acquire")
        self.assertIn("spawn", events)
        self.assertEqual(events[-1], "release")

    def test_the_environment_is_filtered_without_mutating_the_application_environment(self):
        before = dict(app.os.environ)

        with self.bundle():
            self.launch()

        _command, kwargs = self.record[0]
        self.assert_no_bundle_entries(kwargs["env"])
        self.assertEqual(dict(app.os.environ), before)
        self.assertEqual(app.os.environ.get("PATH"), before.get("PATH"))

    def test_the_connector_keeps_its_own_working_directory_and_is_sanitized(self):
        # Use a real temporary executable so the launch gate is tested without
        # assuming Yeelight Chroma Connector is installed on the test machine.
        connector_dir = os.path.join(self.tmpdir, "Yeelight Chroma Connector")
        os.makedirs(connector_dir)
        connector = os.path.join(connector_dir, "Yeelight Chroma Connector.exe")
        with open(connector, "wb") as handle:
            handle.write(b"MZ")

        with self.bundle():
            self.launch(connector, hidden=False, cwd=os.path.dirname(connector))

        _command, kwargs = self.record[0]
        self.assertEqual(kwargs["cwd"], os.path.dirname(connector))
        self.assertNotEqual(kwargs["startupinfo"].wShowWindow, 0)  # started, not hidden
        self.assert_no_bundle_entries(kwargs["env"])
        self.assertEqual(self.calls, [("set", None), ("set", self.PREVIOUS_DLL_DIR)])

    def test_the_direct_openrgb_launch_keeps_its_cwd_and_is_sanitized(self):
        # `launch_openrgb` reuses the same gate, so the elevated direct launch
        # gets the sanitized environment and keeps its own working directory.
        class OpenRgbStub:
            launch_process = app.RestoreEngineThread.launch_process

            def __init__(self):
                self.running = True
                self.progress_update = types.SimpleNamespace(emit=lambda message: None)

        with self.bundle():
            with mock.patch.object(app, "is_process_elevated", return_value=True):
                self.assertTrue(
                    app.RestoreEngineThread.launch_openrgb(OpenRgbStub(), self.external)
                )

        _command, kwargs = self.record[-1]
        self.assertEqual(kwargs["cwd"], os.path.dirname(self.external))
        self.assertEqual(kwargs["creationflags"], 0)
        self.assert_no_bundle_entries(kwargs["env"])

        _command, kwargs = self.record[-1]
        self.assertEqual(kwargs["cwd"], os.path.dirname(self.external))
        self.assertEqual(kwargs["creationflags"], 0)
        self.assert_no_bundle_entries(kwargs["env"])

    def test_only_the_external_launch_gate_passes_the_sanitized_environment(self):
        # Application-internal subprocesses (`taskkill`, the elevation helper)
        # never route through `launch_process`, so they keep the plain
        # environment; the single `env=` argument proves the gate is narrow.
        source = inspect.getsource(app)
        self.assertEqual(source.count("env=external_process_environment()"), 1)
        kill_process = inspect.getsource(app.RestoreEngineThread.kill_process)
        self.assertNotIn("launch_process", kill_process)
        self.assertNotIn("external_process_environment", kill_process)


# ---------------------------------------------------------
# 14a. Every enabled device is targeted, and one failure never stops the rest
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestMultiDeviceRestoreTargets(unittest.TestCase):
    """The wake/day paths iterate the enabled device list, not two fixed slots."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="yeelight-multi-device-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)

    def manager(self, devices):
        config = cm.default_config()
        config["location"].update({"latitude": "11.1111", "longitude": "-22.2222"})
        config["lights"]["devices"] = devices
        # Razer/Artemis/OpenRGB/connector stay disabled or unconfigured: this
        # test is only about which Yeelight devices are addressed.
        config["automation"]["wait_for_razer_synapse"] = False
        config["automation"]["launch_razer_synapse"] = False
        config_path = self.path(cm.CONFIG_FILENAME)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
        return cm.ConfigManager(config_path)

    def four_devices(self):
        return [
            yeelight_device("Desk Lamp", "192.168.1.50"),
            yeelight_device("Monitor Lightstrip", "192.168.1.51"),
            yeelight_device("Living Room", "192.168.1.52", enabled=False),
            yeelight_device("Study", "192.168.1.53"),
        ]

    def test_nighttime_turns_on_every_enabled_device(self):
        stub = RestoreFlowStub(self.manager(self.four_devices()), is_dark=True)
        stub.run()
        self.assertEqual(
            ["192.168.1.50", "192.168.1.51", "192.168.1.53"], stub.turned_on
        )
        self.assertEqual([], stub.turned_off)

    def test_daytime_turns_off_every_enabled_device(self):
        stub = RestoreFlowStub(self.manager(self.four_devices()), is_dark=False)
        stub.run()
        self.assertEqual(
            ["192.168.1.50", "192.168.1.51", "192.168.1.53"], stub.turned_off
        )
        self.assertEqual([], stub.turned_on)
        self.assertIn("Restoration sequence successfully completed!", stub.messages)

    def test_a_disabled_device_is_never_targeted(self):
        devices = [
            yeelight_device("Desk Lamp", "192.168.1.50", enabled=False),
            yeelight_device("Study", "192.168.1.53"),
        ]
        stub = RestoreFlowStub(self.manager(devices), is_dark=True)
        stub.run()
        self.assertEqual(["192.168.1.53"], stub.turned_on)

    def test_zero_devices_does_not_fail_the_restore(self):
        for is_dark in (True, False):
            with self.subTest(is_dark=is_dark):
                stub = RestoreFlowStub(self.manager([]), is_dark=is_dark)
                stub.run()
                self.assertEqual([], stub.turned_on)
                self.assertEqual([], stub.turned_off)
                self.assertIn("Restoration sequence successfully completed!", stub.messages)

    def test_one_failing_device_does_not_stop_the_others(self):
        devices = [
            yeelight_device("Desk Lamp", "192.168.1.50"),
            yeelight_device("Monitor Lightstrip", "192.168.1.51"),
            yeelight_device("Study", "192.168.1.53"),
        ]
        stub = RestoreFlowStub(self.manager(devices), is_dark=True)

        # The middle device is unreachable: the others must still be switched.
        def failing(ip):
            if ip == "192.168.1.51":
                raise OSError("host unreachable")
            stub.turned_on.append(ip)

        stub.safe_turn_on = failing
        with self.assertLogs(level="WARNING") as captured:
            stub.run()

        self.assertEqual(["192.168.1.50", "192.168.1.53"], stub.turned_on)
        self.assertTrue(
            any("192.168.1.51" in line for line in captured.output), captured.output
        )
        self.assertIn("Restoration sequence successfully completed!", stub.messages)

    def test_one_failing_device_does_not_stop_the_daytime_shutdown(self):
        devices = [
            yeelight_device("Desk Lamp", "192.168.1.50"),
            yeelight_device("Monitor Lightstrip", "192.168.1.51"),
        ]
        stub = RestoreFlowStub(self.manager(devices), is_dark=False)

        def failing(ip):
            if ip == "192.168.1.50":
                raise OSError("host unreachable")
            stub.turned_off.append(ip)

        stub.safe_turn_off = failing
        with self.assertLogs(level="WARNING"):
            stub.run()

        self.assertEqual(["192.168.1.51"], stub.turned_off)

    def test_the_restore_never_runs_discovery(self):
        """The restore path reads the configuration; it never scans the network."""
        stub = RestoreFlowStub(self.manager(self.four_devices()), is_dark=True)

        def explode(*args, **kwargs):
            raise AssertionError("the restore sequence started a network discovery")

        with mock.patch.object(app_yd, "discover_devices", side_effect=explode):
            stub.run()

        self.assertEqual(3, len(stub.turned_on))


# ---------------------------------------------------------
# 14b. Every child process gets the right working directory
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestChildProcessWorkingDirectories(TestRestoreSequenceContinuesWithoutOpenRgb):
    """Each launched integration starts in the directory it needs.

    The Yeelight Chroma Connector resolves its runtime files relative to its
    current directory. Launched with *our* inherited directory it loaded them
    from this application's own packaged folder (`dist\\...\\_internal`), which
    kept the build output locked. It therefore starts in its own executable's
    folder, while Razer and Artemis keep the inherited one unchanged.
    """

    CONNECTOR = "Yeelight Chroma Connector.exe"
    SYNAPSE = "Razer Synapse 3.exe"
    ARTEMIS = "Artemis.UI.Windows.exe"

    def restore_config(self, wait_for_synapse=False, launch_synapse=False):
        """A full four-integration configuration, everything enabled."""
        config = cm.default_config()
        config["location"].update({"latitude": "11.1111", "longitude": "-22.2222"})
        config["lights"]["devices"] = [yeelight_device("Desk Lamp", "192.168.1.50")]
        for key, executable in (
            ("openrgb", "OpenRGB.exe"),
            ("yeelight_connector", self.CONNECTOR),
            ("razer_synapse", self.SYNAPSE),
            ("artemis", self.ARTEMIS),
        ):
            config["paths"][key] = self.make_executable(executable)
            config["integrations"][key]["enabled"] = True
        config["automation"]["wait_for_razer_synapse"] = wait_for_synapse
        config["automation"]["launch_razer_synapse"] = launch_synapse
        config_path = self.path(cm.CONFIG_FILENAME)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
        return cm.ConfigManager(config_path)

    def run_full_sequence(self):
        """Run the real restore sequence with every integration enabled.

        OpenRGB is launched directly (the app is reported as elevated, which is
        the only restore path that reaches `launch_process` for OpenRGB).
        """
        stub = RestoreFlowStub(self.restore_config(), is_dark=True)
        with mock.patch.object(app, "is_process_elevated", return_value=True):
            stub.run()
        return stub

    def launch_of(self, stub, executable):
        """The recorded launch(es) of one executable, ignoring the others."""
        calls = [
            entry
            for entry in stub.launch_calls
            if os.path.basename(entry[0]) == executable
        ]
        self.assertTrue(calls, stub.launch_calls)
        return calls

    def test_the_restore_path_starts_the_connector_in_its_own_folder(self):
        stub = self.run_full_sequence()
        (path, args, hidden, cwd), = self.launch_of(stub, self.CONNECTOR)

        self.assertEqual(cwd, os.path.dirname(path))
        # That folder is the application's installation folder here (not the
        # inherited working directory the test process runs in).
        self.assertEqual(cwd, self.tmpdir)
        self.assertNotEqual(cwd, os.getcwd())
        # Nothing else about the connector launch changed: still no arguments,
        # still started without a hidden window.
        self.assertEqual(args, [])
        self.assertFalse(hidden)

    def test_openrgb_still_uses_its_own_executable_folder(self):
        stub = self.run_full_sequence()
        path, args, _hidden, cwd = self.launch_of(stub, "OpenRGB.exe")[0]

        self.assertEqual(cwd, os.path.dirname(path))
        self.assertEqual(args, list(wt.OPENRGB_TASK_ARGS))

    def test_razer_keeps_the_inherited_working_directory(self):
        stub = RestoreFlowStub(
            self.restore_config(wait_for_synapse=False, launch_synapse=True),
            is_dark=True,
        )
        with mock.patch.object(app, "is_process_elevated", return_value=False):
            with mock.patch.object(
                app,
                "openrgb_elevation_status",
                return_value=status(wt.STATUS_NEEDS_SETUP, "Needs setup"),
            ):
                stub.run()

        path, args, hidden, cwd = self.launch_of(stub, self.SYNAPSE)[0]

        # Out of scope for the connector fix: launched exactly as before.
        self.assertEqual(path, self.path(self.SYNAPSE))
        self.assertIsNone(cwd)
        self.assertEqual(args, [])
        self.assertFalse(hidden)

    def test_artemis_keeps_the_inherited_working_directory(self):
        stub = self.run_full_sequence()
        calls = self.launch_of(stub, self.ARTEMIS)

        # The stub never reports a newly started process, so the sequence
        # retries once - that retry behaviour must stay untouched too.
        self.assertEqual(len(calls), 2, calls)
        for _path, args, hidden, cwd in calls:
            self.assertIsNone(cwd)
            self.assertEqual(args, ["--minimized"])
            self.assertFalse(hidden)

    def test_the_connector_is_launched_exactly_once(self):
        stub = self.run_full_sequence()
        self.assertEqual(len(self.launch_of(stub, self.CONNECTOR)), 1)

    def test_no_sequencing_or_timing_value_changed(self):
        # The connector fix must not touch the restore sequence: same order,
        # same waits outside the OpenRGB gate, still ending with the completion
        # message. OpenRGB is elevated-launched here, its readiness gate is
        # satisfied, Artemis starts and is then retried once.
        stub = self.run_full_sequence()

        # Sleeps outside the OpenRGB readiness gate, in order: the 5 s system
        # stabilization wait, the 3 s music-mode release wait, the 1 s daytime
        # connector-stop wait (this run is *dark*, so that one is absent) and the
        # two 3 s Artemis retry waits. The old "wait 8 s for OpenRGB to bind its
        # server" sleep is gone - it is now a readiness gate, see
        # tests/test_openrgb_readiness.py.
        self.assertEqual(stub.sleeps, [5, 3, 3, 3])
        self.assertEqual(stub.ready_waits, 1)
        self.assertEqual(stub.finished, [True])
        self.assertEqual(
            [os.path.basename(entry[0]) for entry in stub.launch_calls],
            ["OpenRGB.exe", self.CONNECTOR, self.ARTEMIS, self.ARTEMIS],
        )
        self.assertIn("Restoration sequence successfully completed!", stub.messages)
        self.assertIn(
            "OpenRGB launched. Waiting for OpenRGB to finish detecting controllers...",
            stub.messages,
        )
        # Step 0 still terminates the stale controller processes first.
        self.assertIn(self.CONNECTOR, stub.killed)
        self.assertIn(self.ARTEMIS, stub.killed)
        self.assertIn("OpenRGB.exe", stub.killed)

    def test_the_connector_launch_site_pins_its_own_folder(self):
        # There is currently a single connector launch site. If another is ever
        # added it must pass its own directory as well - an inherited working
        # directory is exactly the bug that is being fixed here.
        source = inspect.getsource(app.RestoreEngineThread)
        self.assertEqual(
            len(re.findall(r"self\.launch_process\(\s*connector_path", source)), 1, source
        )

        # Nothing is passed between the connector path and the pinned directory,
        # so the working directory cannot be lost behind an argument.
        compact = re.sub(r"\s+", "", source)
        self.assertIn(
            "self.launch_process(connector_path,hidden=False,cwd=os.path.dirname(connector_path),)",
            compact,
        )


# ---------------------------------------------------------
# 15. The automatic restore path contains no interactive elevation call
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestNoInteractiveElevationInAutomation(unittest.TestCase):
    def test_restore_thread_has_no_elevation_request(self):
        source = inspect.getsource(app.RestoreEngineThread)
        self.assertNotIn("runas", source.lower())
        self.assertNotIn("ShellExecute", source)

    def test_the_old_shell_execute_launcher_is_gone(self):
        self.assertFalse(hasattr(app.RestoreEngineThread, "launch_process_elevated"))
        self.assertTrue(hasattr(app.RestoreEngineThread, "launch_openrgb"))

    def test_runas_exists_only_in_the_explicit_provisioning_helper(self):
        for name, member in vars(wt).items():
            if not inspect.isfunction(member) or member.__module__ != wt.__name__:
                continue
            source = inspect.getsource(member)
            if name == "_shell_execute_ex_runas":
                self.assertIn('"runas"', source)
            else:
                # Includes the parent-side sequence that drives the helper: it
                # must reach the elevation request through that one helper only.
                self.assertNotIn('"runas"', source, f'{name} uses "runas"')
        self.assertNotIn("runas", inspect.getsource(app.SolarEngineThread).lower())

    def test_the_process_handle_launcher_is_the_only_elevation_launcher(self):
        # The handle-less ShellExecuteW() launcher is gone: without a process
        # handle the parent cannot tell a failed helper from a missing one.
        self.assertFalse(hasattr(wt, "_shell_execute_runas"))
        self.assertTrue(hasattr(wt, "_shell_execute_ex_runas"))
        self.assertTrue(hasattr(wt, "_wait_for_process"))
        self.assertTrue(hasattr(wt, "_close_process_handle"))
        for name in ("_shell_execute_ex_runas", "_wait_for_process"):
            with self.subTest(name=name):
                self.assertIn("NOCLOSEPROCESS" if "ex_runas" in name else "WaitForSingleObject",
                              inspect.getsource(getattr(wt, name)))

    def test_the_provisioning_path_does_not_poll_for_the_task(self):
        # Blind polling was the primary completion mechanism; it must not come
        # back, because it cannot distinguish "still working" from "failed".
        for name in (
            "run_elevated_provisioning",
            "request_elevated_openrgb_provisioning",
        ):
            with self.subTest(name=name):
                source = inspect.getsource(getattr(wt, name))
                self.assertNotIn("time.sleep", source)
                self.assertNotIn("POLL", source)

    def test_the_suspend_path_has_no_elevation_request(self):
        source = inspect.getsource(app.YeelightPCCompanionWindow._execute_suspend_actions)
        self.assertNotIn("runas", source.lower())
        self.assertNotIn("ShellExecute", source)


class TestStartupImportCost(unittest.TestCase):
    """`windows_tasks` must stay cheap to import: it loads on every launch.

    `xml.sax.saxutils` exists only to provide `escape`, but importing it also
    pulls in `urllib.request`, and with it `ssl`, `http.client` and `email`.
    That chain measured roughly 90 ms of cold-start cost for the whole
    application, so `escape` is fetched inside `build_openrgb_task_xml()` - the
    one function that needs it, on a path that never runs at startup.
    """

    @staticmethod
    def import_windows_tasks_in_a_fresh_interpreter():
        script = (
            "import sys, windows_tasks; "
            "print(int('xml.sax.saxutils' in sys.modules), "
            "int('urllib.request' in sys.modules))"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
        )

    def test_importing_windows_tasks_does_not_load_the_sax_helpers(self):
        completed = self.import_windows_tasks_in_a_fresh_interpreter()
        self.assertEqual(0, completed.returncode, completed.stderr)
        loaded_sax, loaded_urllib = completed.stdout.split()
        self.assertEqual("0", loaded_sax, "xml.sax.saxutils must not be imported eagerly")
        self.assertEqual("0", loaded_urllib, "urllib.request must not be imported eagerly")

    def test_no_module_level_function_needs_the_escaping_helper(self):
        # The helper is only reachable from inside the task-XML builder, so the
        # lazy import cannot have hidden a module-level name that other code
        # relies on.
        self.assertFalse(hasattr(wt, "escape"))


if __name__ == "__main__":
    unittest.main()