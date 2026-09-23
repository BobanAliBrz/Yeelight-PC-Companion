"""Regression tests for the OpenRGB Windows-service conflict feature.

These cover detection, identity, the pure conflict policy, the narrow elevated
``--disable-openrgb-service`` CLI, the elevation boundary, the Integrations UI
presentation, the restore diagnostic and the suspend-timing contract.

NO test may:

* manipulate the real OpenRGB service,
* create or delete a real Windows service,
* elevate,
* create or delete a real scheduled task,
* trigger real sleep,
* kill the real OpenRGB process.

The Windows Service Control Manager layer is mocked/injected at the
``openrgb_service`` helper boundary. Elevation is mocked. The real SCM is never
opened by this suite.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openrgb_service as svc  # noqa: E402
import windows_tasks as wt  # noqa: E402

try:
    import yeelight_pc_companion as app  # noqa: E402
except Exception as exc:  # pragma: no cover - environment dependent
    app = None
    APP_IMPORT_ERROR = exc
else:
    APP_IMPORT_ERROR = None

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception as exc:  # pragma: no cover - environment dependent
    QApplication = None
    QMessageBox = None
    QT_IMPORT_ERROR = exc
else:
    QT_IMPORT_ERROR = None

SKIP_REASON = f"application stack not importable here: {APP_IMPORT_ERROR or QT_IMPORT_ERROR}"


def qt_app():
    return QApplication.instance() or QApplication([])


class ScmFake:
    """A minimal in-memory stand-in for the SCM helpers.

    Tests never open the real Service Control Manager. The fake records handle
    closes so leak assertions are possible.
    """

    def __init__(
        self,
        exists=True,
        state=svc.SERVICE_STOPPED,
        start_type=svc.SERVICE_DEMAND_START,
        binary_path=r"C:\Program Files\OpenRGB\OpenRGB.exe",
        open_scm_error=None,
        open_service_error=0,
        status_error=None,
        config_error=None,
    ):
        self.exists = exists
        self.state = state
        self.start_type = start_type
        self.binary_path = binary_path
        self.open_scm_error = open_scm_error
        self.open_service_error = open_service_error
        self.status_error = status_error
        self.config_error = config_error
        self.closed_handles = []
        self.change_config_calls = []
        self.control_calls = []
        self.scm_handle = 100
        self.service_handle = 200
        self._next_handle = 100

    def open_scm(self, desired_access=svc.SC_MANAGER_CONNECT):
        if self.open_scm_error:
            raise svc.OpenRgbServiceError(self.open_scm_error)
        self._next_handle += 1
        self.scm_handle = self._next_handle
        return types.SimpleNamespace(), self.scm_handle

    def open_service(self, advapi32, scm_handle, desired_access):
        if not self.exists:
            return 0, svc.ERROR_SERVICE_DOES_NOT_EXIST
        if self.open_service_error:
            return 0, self.open_service_error
        self._next_handle += 1
        self.service_handle = self._next_handle
        self.last_desired_access = desired_access
        return self.service_handle, 0

    def query_status(self, advapi32, service_handle):
        if self.status_error:
            raise svc.OpenRgbServiceError(self.status_error)
        return types.SimpleNamespace(dwCurrentState=self.state)

    def query_config(self, advapi32, service_handle):
        if self.config_error:
            raise svc.OpenRgbServiceError(self.config_error)
        # QueryServiceConfigW needs SERVICE_QUERY_CONFIG on the handle. Enforce
        # it so a mutation open that forgets that right fails the way real
        # Windows would (Access Denied) instead of silently succeeding.
        if not (self.last_desired_access & svc.SERVICE_QUERY_CONFIG):
            raise svc.OpenRgbServiceError(
                "QueryServiceConfigW requires SERVICE_QUERY_CONFIG "
                f"(granted mask={self.last_desired_access:#x})."
            )
        # Mirror the real helper: return the *name*, not the raw DWORD.
        if isinstance(self.start_type, str):
            name = self.start_type
        else:
            name = svc.START_TYPE_NAMES.get(int(self.start_type), svc.START_TYPE_UNKNOWN)
        return name, self.binary_path

    def change_startup_disabled(self, advapi32, service_handle):
        self.change_config_calls.append(service_handle)
        self.start_type = svc.SERVICE_DISABLED

    def request_stop(self, advapi32, service_handle):
        self.control_calls.append(service_handle)
        return True

    def wait_stopped(self, advapi32, service_handle, timeout_seconds):
        if self.state == svc.SERVICE_STOPPED:
            return True
        self.state = svc.SERVICE_STOPPED
        return True

    def close_handle(self, handle):
        if not handle:
            return
        self.closed_handles.append(handle)


def install_scm_fake(testcase, fake):
    """Route every SCM helper in openrgb_service through `fake`."""
    patches = [
        mock.patch.object(svc, "_open_scm", fake.open_scm),
        mock.patch.object(svc, "_open_service", fake.open_service),
        mock.patch.object(svc, "_query_service_status", fake.query_status),
        mock.patch.object(svc, "_query_service_binary_path", fake.query_config),
        mock.patch.object(svc, "_change_startup_disabled", fake.change_startup_disabled),
        mock.patch.object(svc, "_request_service_stop", fake.request_stop),
        mock.patch.object(svc, "_wait_for_service_stopped", fake.wait_stopped),
        mock.patch.object(svc, "_close_service_handle", fake.close_handle),
    ]
    for patch in patches:
        patch.start()
        testcase.addCleanup(patch.stop)
    return fake


# ---------------------------------------------------------
# 1. Read-only probe
# ---------------------------------------------------------
class TestReadOnlyProbe(unittest.TestCase):
    def test_absent_service_is_reported_as_absent_without_error(self):
        fake = install_scm_fake(self, ScmFake(exists=False))
        probe = svc.probe_openrgb_service()
        self.assertFalse(probe.exists)
        self.assertTrue(probe.is_absent)
        self.assertEqual(probe.error, "")
        self.assertEqual(probe.state, svc.STATE_UNKNOWN)

    def test_stopped_service_reports_stopped_state(self):
        install_scm_fake(self, ScmFake(state=svc.SERVICE_STOPPED))
        probe = svc.probe_openrgb_service()
        self.assertTrue(probe.exists)
        self.assertEqual(probe.state, "stopped")

    def test_running_service_reports_running_state(self):
        install_scm_fake(self, ScmFake(state=svc.SERVICE_RUNNING))
        probe = svc.probe_openrgb_service()
        self.assertEqual(probe.state, "running")

    def test_automatic_start_type(self):
        install_scm_fake(self, ScmFake(start_type=svc.SERVICE_AUTO_START))
        probe = svc.probe_openrgb_service()
        self.assertEqual(probe.start_type, "automatic")

    def test_manual_start_type(self):
        install_scm_fake(self, ScmFake(start_type=svc.SERVICE_DEMAND_START))
        probe = svc.probe_openrgb_service()
        self.assertEqual(probe.start_type, "manual")

    def test_disabled_start_type(self):
        install_scm_fake(self, ScmFake(start_type=svc.SERVICE_DISABLED))
        probe = svc.probe_openrgb_service()
        self.assertEqual(probe.start_type, "disabled")

    def test_boot_and_system_start_types_are_classified(self):
        install_scm_fake(self, ScmFake(start_type=svc.SERVICE_BOOT_START))
        self.assertEqual(svc.probe_openrgb_service().start_type, "boot")
        install_scm_fake(self, ScmFake(start_type=svc.SERVICE_SYSTEM_START))
        self.assertEqual(svc.probe_openrgb_service().start_type, "system")

    def test_query_error_is_not_absent(self):
        install_scm_fake(self, ScmFake(open_scm_error="SCM unavailable"))
        probe = svc.probe_openrgb_service()
        self.assertFalse(probe.exists)
        self.assertFalse(probe.is_absent)
        self.assertTrue(probe.query_failed)
        self.assertIn("SCM unavailable", probe.error)

    def test_access_error_is_not_absent(self):
        install_scm_fake(self, ScmFake(open_service_error=svc.ERROR_ACCESS_DENIED))
        probe = svc.probe_openrgb_service()
        self.assertFalse(probe.is_absent)
        self.assertTrue(probe.query_failed)

    def test_status_query_failure_keeps_the_error(self):
        install_scm_fake(self, ScmFake(status_error="status unreadable"))
        probe = svc.probe_openrgb_service()
        self.assertTrue(probe.exists)
        self.assertTrue(probe.query_failed)
        self.assertIn("status unreadable", probe.error)

    def test_handles_are_always_closed(self):
        fake = install_scm_fake(self, ScmFake())
        svc.probe_openrgb_service()
        self.assertIn(fake.service_handle, fake.closed_handles)
        self.assertIn(fake.scm_handle, fake.closed_handles)

    def test_handles_are_closed_on_query_failure(self):
        fake = install_scm_fake(self, ScmFake(status_error="boom", config_error="boom"))
        svc.probe_openrgb_service()
        self.assertIn(fake.service_handle, fake.closed_handles)
        self.assertIn(fake.scm_handle, fake.closed_handles)

    def test_handles_are_closed_when_scm_open_fails(self):
        fake = install_scm_fake(self, ScmFake(open_scm_error="no scm"))
        svc.probe_openrgb_service()
        # No handles were opened; nothing must be left dangling.
        self.assertEqual(fake.closed_handles, [])

    def test_probe_never_raises(self):
        install_scm_fake(
            self,
            ScmFake(
                open_scm_error=None,
                open_service_error=1,
                status_error="x",
                config_error="y",
            ),
        )
        probe = svc.probe_openrgb_service()
        self.assertIsInstance(probe, svc.OpenRgbServiceProbe)


# ---------------------------------------------------------
# 2. Binary identity
# ---------------------------------------------------------
class TestBinaryIdentity(unittest.TestCase):
    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def test_quoted_matching_executable(self):
        self.assertTrue(
            svc.service_binary_matches(f'"{self.EXPECTED}"', self.EXPECTED)
        )

    def test_unquoted_matching_executable(self):
        self.assertTrue(svc.service_binary_matches(self.EXPECTED, self.EXPECTED))

    def test_case_insensitive_matching(self):
        self.assertTrue(
            svc.service_binary_matches(
                r"c:\program files\openrgb\openrgb.EXE", self.EXPECTED
            )
        )

    def test_path_with_spaces(self):
        self.assertTrue(
            svc.service_binary_matches(
                r'"C:\Program Files\OpenRGB\OpenRGB.exe"', self.EXPECTED
            )
        )
        self.assertTrue(
            svc.service_binary_matches(
                r"C:\Program Files\OpenRGB\OpenRGB.exe", self.EXPECTED
            )
        )

    def test_image_path_with_normal_arguments(self):
        for image in (
            r'"C:\Program Files\OpenRGB\OpenRGB.exe" --server',
            r'"C:\Program Files\OpenRGB\OpenRGB.exe" --gui --startminimized --server',
            r"C:\Program Files\OpenRGB\OpenRGB.exe --server --gui",
            r'"C:\Program Files\OpenRGB\OpenRGB.exe" --server --gui --startminimized',
        ):
            with self.subTest(image=image):
                self.assertTrue(svc.service_binary_matches(image, self.EXPECTED))

    def test_mismatch(self):
        self.assertFalse(
            svc.service_binary_matches(
                r"C:\Other\App\OpenRGB.exe", self.EXPECTED
            )
        )
        self.assertFalse(
            svc.service_binary_matches(
                r"C:\Program Files\Other\OpenRGB.exe", self.EXPECTED
            )
        )

    def test_malformed_binary_path_is_unverifiable(self):
        self.assertIsNone(svc.service_binary_matches("", self.EXPECTED))
        self.assertIsNone(svc.service_binary_matches('"unterminated', self.EXPECTED))
        self.assertIsNone(svc.service_binary_matches("   ", self.EXPECTED))

    def test_empty_expected_path_is_unverifiable(self):
        self.assertIsNone(
            svc.service_binary_matches(self.EXPECTED, "")
        )

    def test_native_object_manager_prefix_is_handled(self):
        self.assertTrue(
            svc.service_binary_matches(
                r"\??\C:\Program Files\OpenRGB\OpenRGB.exe", self.EXPECTED
            )
        )

    def test_extract_service_binary_path_shapes(self):
        cases = {
            self.EXPECTED: self.EXPECTED,
            f'"{self.EXPECTED}"': self.EXPECTED,
            f'"{self.EXPECTED}" --server': self.EXPECTED,
            f"{self.EXPECTED} --server": self.EXPECTED,
            r"\??\C:\Program Files\OpenRGB\OpenRGB.exe": self.EXPECTED,
        }
        for image, expected in cases.items():
            with self.subTest(image=image):
                self.assertEqual(
                    svc.extract_service_binary_path(image).lower(),
                    expected.lower(),
                )

    def test_normalize_expands_env_and_casefolds(self):
        normalized = svc.normalize_executable_path(self.EXPECTED.upper())
        self.assertEqual(
            normalized, svc.normalize_executable_path(self.EXPECTED)
        )

    def test_mismatch_never_reaches_mutation_code(self):
        fake = install_scm_fake(
            self,
            ScmFake(
                state=svc.SERVICE_RUNNING,
                start_type=svc.SERVICE_AUTO_START,
                binary_path=r"C:\Elsewhere\OpenRGB.exe",
            ),
        )
        result = svc.disable_openrgb_service(self.EXPECTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(fake.change_config_calls, [])
        self.assertEqual(fake.control_calls, [])

    def test_unverifiable_identity_never_reaches_mutation_code(self):
        fake = install_scm_fake(self, ScmFake(binary_path=""))
        result = svc.disable_openrgb_service(self.EXPECTED)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(fake.change_config_calls, [])
        self.assertEqual(fake.control_calls, [])


# ---------------------------------------------------------
# 3. Conflict policy
# ---------------------------------------------------------
class TestConflictPolicy(unittest.TestCase):
    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def evaluate(self, **overrides):
        defaults = {
            "exists": True,
            "state": "stopped",
            "start_type": "manual",
            "binary_path": self.EXPECTED,
            "error": "",
        }
        defaults.update(overrides)
        probe = svc.OpenRgbServiceProbe(**defaults)
        return svc.evaluate_openrgb_service_status(probe, True, self.EXPECTED)

    def test_running_is_conflict_regardless_of_manual(self):
        status = self.evaluate(state="running", start_type="manual")
        self.assertEqual(status.state, svc.SERVICE_STATE_CONFLICT)
        self.assertTrue(status.is_conflict)
        self.assertTrue(status.can_auto_fix)

    def test_running_is_conflict_even_when_disabled_start_type(self):
        # A running service is a current conflict even if its start type looks
        # harmless - something started it.
        status = self.evaluate(state="running", start_type="disabled")
        self.assertTrue(status.is_conflict)

    def test_automatic_is_conflict_even_when_stopped(self):
        status = self.evaluate(state="stopped", start_type="automatic")
        self.assertEqual(status.state, svc.SERVICE_STATE_CONFLICT)
        self.assertTrue(status.is_conflict)
        self.assertTrue(status.can_auto_fix)

    def test_boot_and_system_are_automatic_conflicts(self):
        for start in ("boot", "system"):
            with self.subTest(start=start):
                status = self.evaluate(state="stopped", start_type=start)
                self.assertTrue(status.is_conflict)

    def test_stopped_manual_is_not_an_active_conflict(self):
        status = self.evaluate(state="stopped", start_type="manual")
        self.assertEqual(status.state, svc.SERVICE_STATE_INSTALLED_IDLE)
        self.assertFalse(status.is_conflict)
        self.assertFalse(status.can_auto_fix)
        self.assertIn("Manual", status.detail)

    def test_stopped_disabled_is_no_conflict(self):
        status = self.evaluate(state="stopped", start_type="disabled")
        self.assertEqual(status.state, svc.SERVICE_STATE_NO_CONFLICT)
        self.assertFalse(status.is_conflict)

    def test_absent_service_is_no_conflict(self):
        probe = svc.OpenRgbServiceProbe(exists=False, error="")
        status = svc.evaluate_openrgb_service_status(probe, True, self.EXPECTED)
        self.assertEqual(status.state, svc.SERVICE_STATE_NO_CONFLICT)

    def test_unknown_is_not_silently_safe(self):
        probe = svc.OpenRgbServiceProbe(exists=False, error="access denied")
        status = svc.evaluate_openrgb_service_status(probe, True, self.EXPECTED)
        self.assertEqual(status.state, svc.SERVICE_STATE_UNKNOWN)
        self.assertFalse(status.can_auto_fix)
        self.assertIn("Review", status.detail)

    def test_unknown_start_type_is_not_safe(self):
        status = self.evaluate(
            state="stopped",
            start_type="unknown",
            binary_path=self.EXPECTED,
        )
        self.assertEqual(status.state, svc.SERVICE_STATE_UNKNOWN)
        self.assertFalse(status.can_auto_fix)

    def test_binary_mismatch_is_review_and_not_auto_fixable(self):
        status = self.evaluate(
            state="running",
            start_type="automatic",
            binary_path=r"C:\Other\App.exe",
        )
        self.assertEqual(status.state, svc.SERVICE_STATE_BINARY_MISMATCH)
        self.assertTrue(status.is_conflict)
        self.assertFalse(status.can_auto_fix)

    def test_integration_disabled_is_not_used(self):
        probe = svc.OpenRgbServiceProbe(
            exists=True,
            state="running",
            start_type="automatic",
            binary_path=self.EXPECTED,
        )
        status = svc.evaluate_openrgb_service_status(probe, False, self.EXPECTED)
        self.assertEqual(status.state, svc.SERVICE_STATE_NOT_USED)
        self.assertFalse(status.is_conflict)

    def test_start_pending_counts_as_not_stopped(self):
        status = self.evaluate(state="start_pending", start_type="manual")
        self.assertTrue(status.is_conflict)

    def test_partial_status_failure_with_disabled_config_is_unknown_not_safe(self):
        # exists=True, QueryServiceStatus failed, QueryServiceConfig succeeded
        # with Disabled + a matching binary. Fail closed: never "No conflict".
        status = self.evaluate(
            exists=True,
            state="unknown",
            start_type="disabled",
            binary_path=self.EXPECTED,
            error="status unreadable",
        )
        self.assertEqual(status.state, svc.SERVICE_STATE_UNKNOWN)
        self.assertNotEqual(status.state, svc.SERVICE_STATE_NO_CONFLICT)
        self.assertFalse(status.can_auto_fix)
        self.assertIn("status unreadable", status.detail)

    def test_partial_status_failure_with_automatic_config_is_unknown_no_fix(self):
        status = self.evaluate(
            exists=True,
            state="unknown",
            start_type="automatic",
            binary_path=self.EXPECTED,
            error="status unreadable",
        )
        self.assertEqual(status.state, svc.SERVICE_STATE_UNKNOWN)
        self.assertFalse(status.can_auto_fix)
        self.assertNotEqual(status.state, svc.SERVICE_STATE_NO_CONFLICT)

    def test_partial_config_failure_is_unknown_or_review_no_fix(self):
        # exists=True, status read succeeded (running), config query failed so
        # identity/start type are unverifiable.
        status = self.evaluate(
            exists=True,
            state="running",
            start_type="unknown",
            binary_path="",
            error="config unreadable",
        )
        self.assertIn(
            status.state,
            (svc.SERVICE_STATE_UNKNOWN, svc.SERVICE_STATE_BINARY_MISMATCH),
        )
        self.assertFalse(status.can_auto_fix)
        self.assertNotEqual(status.state, svc.SERVICE_STATE_NO_CONFLICT)

    def test_error_present_never_produces_no_conflict(self):
        cases = [
            dict(exists=False, state="unknown", start_type="unknown", binary_path="", error="x"),
            dict(exists=True, state="unknown", start_type="disabled", binary_path=self.EXPECTED, error="x"),
            dict(exists=True, state="stopped", start_type="manual", binary_path=self.EXPECTED, error="x"),
            dict(exists=True, state="stopped", start_type="disabled", binary_path=self.EXPECTED, error="x"),
            dict(exists=True, state="running", start_type="automatic", binary_path=self.EXPECTED, error="x"),
            dict(exists=True, state="stopped", start_type="unknown", binary_path="", error="x"),
        ]
        for overrides in cases:
            with self.subTest(**overrides):
                status = self.evaluate(**overrides)
                self.assertNotEqual(status.state, svc.SERVICE_STATE_NO_CONFLICT)
                self.assertFalse(status.can_auto_fix)

    def test_policy_is_pure_and_takes_no_service_name(self):
        # The evaluator must not accept a service name parameter.
        parameters = inspect.signature(svc.evaluate_openrgb_service_status).parameters
        self.assertNotIn("service_name", parameters)
        self.assertNotIn("name", parameters)


# ---------------------------------------------------------
# 4. Privileged CLI
# ---------------------------------------------------------
class TestDisableServiceCommandLine(unittest.TestCase):
    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def test_exact_fixed_command_accepted(self):
        action, path = wt.parse_provisioning_argv(
            ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED]
        )
        self.assertEqual(action, wt.ACTION_DISABLE_OPENRGB_SERVICE)
        self.assertEqual(path, self.EXPECTED)

    def test_missing_path_rejected(self):
        with self.assertRaises(wt.WindowsTaskError):
            wt.parse_provisioning_argv(["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG])

    def test_extra_arguments_rejected(self):
        with self.assertRaises(wt.WindowsTaskError):
            wt.parse_provisioning_argv(
                ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED, "extra"]
            )

    def test_service_name_cannot_be_supplied(self):
        for flag in (
            "--service-name",
            "--service",
            "--svc",
            "--name",
            "--run-command",
            "--command",
            "--arguments",
            "--task-name",
        ):
            with self.subTest(flag=flag):
                with self.assertRaises(wt.WindowsTaskError):
                    wt.parse_provisioning_argv(
                        ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, flag, "OpenRGB"]
                    )
                with self.assertRaises(wt.WindowsTaskError):
                    wt.parse_provisioning_argv(
                        ["app.exe", flag, "OpenRGB", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED]
                    )

    def test_flag_must_be_first(self):
        with self.assertRaises(wt.WindowsTaskError):
            wt.parse_provisioning_argv(
                ["app.exe", "--tray", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED]
            )

    def test_no_generic_command_interface_exists(self):
        # Banned tokens must not appear as *accepted CLI flags*. Mentions in
        # documentation that say they are rejected are fine.
        source = inspect.getsource(wt.parse_provisioning_argv)
        for token in ("--service-name", "--run-command", "--arguments", "--command", "--tn"):
            self.assertNotIn(f'"{token}"', source)
        # The accepted flag set is exactly three fixed modes.
        self.assertIn(wt.PROVISION_FLAG, source)
        self.assertIn(wt.REMOVE_FLAG, source)
        self.assertIn(wt.DISABLE_OPENRGB_SERVICE_FLAG, source)

    def test_absent_service_safely_succeeds(self):
        messages = []
        with mock.patch.object(
            svc, "probe_openrgb_service", return_value=svc.OpenRgbServiceProbe()
        ):
            with mock.patch.object(
                wt, "validate_openrgb_executable", return_value=self.EXPECTED
            ):
                code = wt.run_provisioning_cli(
                    ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED],
                    output=messages.append,
                )
        self.assertEqual(code, 0)
        self.assertTrue(any("No OpenRGB" in m or "stopped and disabled" in m or "nothing" in m.lower() for m in messages))

    def test_matching_service_can_be_stopped_and_disabled(self):
        fake = ScmFake(
            state=svc.SERVICE_RUNNING,
            start_type=svc.SERVICE_AUTO_START,
            binary_path=self.EXPECTED,
        )
        # Force the stop wait to flip the state like a real stop would.
        def wait_stopped(advapi32, service_handle, timeout_seconds):
            fake.state = svc.SERVICE_STOPPED
            fake.start_type = svc.SERVICE_DISABLED
            return True

        fake.wait_stopped = wait_stopped
        install_scm_fake(self, fake)
        messages = []
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.EXPECTED
        ):
            code = wt.run_provisioning_cli(
                ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED],
                output=messages.append,
            )
        self.assertEqual(code, 0)
        self.assertTrue(fake.change_config_calls)
        self.assertTrue(fake.control_calls)

    def test_mismatch_refuses_mutation(self):
        fake = install_scm_fake(
            self,
            ScmFake(binary_path=r"C:\Other\OpenRGB.exe", state=svc.SERVICE_RUNNING),
        )
        messages = []
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.EXPECTED
        ):
            code = wt.run_provisioning_cli(
                ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED],
                output=messages.append,
            )
        self.assertEqual(code, 3)
        self.assertEqual(fake.change_config_calls, [])

    def test_stop_failure_is_not_reported_as_success(self):
        fake = ScmFake(
            state=svc.SERVICE_RUNNING,
            start_type=svc.SERVICE_AUTO_START,
            binary_path=self.EXPECTED,
        )

        def fail_stop(advapi32, service_handle):
            raise svc.OpenRgbServiceError("The OpenRGB service could not be stopped.")

        def no_wait(advapi32, service_handle, timeout_seconds):
            return False

        fake.request_stop = fail_stop
        fake.wait_stopped = no_wait
        # Startup still gets disabled.
        def change(advapi32, service_handle):
            fake.change_config_calls.append(service_handle)
            fake.start_type = svc.SERVICE_DISABLED

        fake.change_startup_disabled = change
        install_scm_fake(self, fake)
        messages = []
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.EXPECTED
        ):
            code = wt.run_provisioning_cli(
                ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED],
                output=messages.append,
            )
        self.assertNotEqual(code, 0)
        self.assertTrue(any("did not stop" in m or "could not be stopped" in m for m in messages))

    def test_disabled_but_still_running_is_not_success(self):
        fake = ScmFake(
            state=svc.SERVICE_RUNNING,
            start_type=svc.SERVICE_AUTO_START,
            binary_path=self.EXPECTED,
        )

        def change(advapi32, service_handle):
            fake.change_config_calls.append(service_handle)
            fake.start_type = svc.SERVICE_DISABLED

        def request_stop(advapi32, service_handle):
            fake.control_calls.append(service_handle)
            # Accepted, but the service never actually stops.
            return True

        def wait_stopped(advapi32, service_handle, timeout_seconds):
            return False  # timeout

        fake.change_startup_disabled = change
        fake.request_stop = request_stop
        fake.wait_stopped = wait_stopped
        install_scm_fake(self, fake)
        messages = []
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.EXPECTED
        ):
            code = wt.run_provisioning_cli(
                ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED],
                output=messages.append,
            )
        self.assertNotEqual(code, 0)
        self.assertTrue(any("Disabled" in m and "did not stop" in m for m in messages))

    def test_helper_requeries_and_verifies_state(self):
        # Success must depend on the post-mutation re-query, not only on the
        # change/stop calls returning.
        fake = ScmFake(
            state=svc.SERVICE_STOPPED,
            start_type=svc.SERVICE_DEMAND_START,
            binary_path=self.EXPECTED,
        )
        query_order = []
        original_query = fake.query_config

        def tracking_query(advapi32, service_handle):
            query_order.append(("config", fake.start_type))
            return original_query(advapi32, service_handle)

        fake.query_config = tracking_query
        install_scm_fake(self, fake)
        with mock.patch.object(
            wt, "validate_openrgb_executable", return_value=self.EXPECTED
        ):
            code = wt.run_provisioning_cli(
                ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, self.EXPECTED],
                output=lambda _m: None,
            )
        self.assertEqual(code, 0)
        # At least one config query after the mutation (the re-query).
        self.assertGreaterEqual(len(query_order), 2)

    def test_validate_rejects_non_exe(self):
        messages = []
        code = wt.run_provisioning_cli(
            ["app.exe", wt.DISABLE_OPENRGB_SERVICE_FLAG, r"C:\evil.cmd"],
            output=messages.append,
        )
        self.assertEqual(code, 1)

    def test_ordinary_start_is_unaffected(self):
        self.assertIsNone(wt.parse_provisioning_argv(["app.exe"]))
        self.assertIsNone(wt.parse_provisioning_argv(["app.exe", "--tray"]))
        self.assertIsNone(
            wt.run_provisioning_cli(["app.exe", "--tray"], output=lambda _m: None)
        )


class TestDisableMutationInternals(unittest.TestCase):
    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def test_disable_result_success_requires_disabled_and_stopped(self):
        fake = ScmFake(
            state=svc.SERVICE_RUNNING,
            start_type=svc.SERVICE_AUTO_START,
            binary_path=self.EXPECTED,
        )

        def change(advapi32, service_handle):
            fake.change_config_calls.append(service_handle)
            fake.start_type = svc.SERVICE_DISABLED

        def request_stop(advapi32, service_handle):
            fake.control_calls.append(service_handle)
            return True

        def wait_stopped(advapi32, service_handle, timeout_seconds):
            fake.state = svc.SERVICE_STOPPED
            return True

        fake.change_startup_disabled = change
        fake.request_stop = request_stop
        fake.wait_stopped = wait_stopped
        install_scm_fake(self, fake)
        result = svc.disable_openrgb_service(self.EXPECTED)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)

    def test_handles_are_closed_after_mutation(self):
        fake = ScmFake(
            state=svc.SERVICE_STOPPED,
            start_type=svc.SERVICE_DEMAND_START,
            binary_path=self.EXPECTED,
        )

        def change(advapi32, service_handle):
            fake.change_config_calls.append(service_handle)
            fake.start_type = svc.SERVICE_DISABLED

        fake.change_startup_disabled = change
        install_scm_fake(self, fake)
        svc.disable_openrgb_service(self.EXPECTED)
        self.assertIn(fake.service_handle, fake.closed_handles)
        self.assertIn(fake.scm_handle, fake.closed_handles)

    def test_identity_is_checked_before_any_scm_mutation_handle_work(self):
        # Even the service open for mutation must not happen on mismatch.
        fake = ScmFake(binary_path=r"C:\Other\app.exe")
        opened = []
        original_open = fake.open_service

        def tracking_open(advapi32, scm_handle, desired_access):
            opened.append(desired_access)
            return original_open(advapi32, scm_handle, desired_access)

        fake.open_service = tracking_open
        install_scm_fake(self, fake)
        result = svc.disable_openrgb_service(self.EXPECTED)
        self.assertFalse(result.ok)
        # The read-only probe may open with QUERY rights; the mutation rights
        # (STOP/CHANGE_CONFIG) must never have been requested.
        for access in opened:
            self.assertEqual(access & (svc.SERVICE_STOP | svc.SERVICE_CHANGE_CONFIG), 0)

    def test_mutation_handle_requests_exactly_the_rights_it_needs(self):
        """The mutation handle must include SERVICE_QUERY_CONFIG.

        Final verification calls QueryServiceConfigW on the *same* handle to
        re-read the startup type. Without SERVICE_QUERY_CONFIG a real Windows
        repair can stop+disable the service and then fail verification with
        Access Denied. The mask is otherwise exactly the four rights this
        handle uses - never SERVICE_ALL_ACCESS or unrelated service rights.
        """
        expected_mask = (
            svc.SERVICE_QUERY_CONFIG
            | svc.SERVICE_QUERY_STATUS
            | svc.SERVICE_STOP
            | svc.SERVICE_CHANGE_CONFIG
        )
        fake = ScmFake(
            state=svc.SERVICE_STOPPED,
            start_type=svc.SERVICE_DEMAND_START,
            binary_path=self.EXPECTED,
        )

        def change(advapi32, service_handle):
            fake.change_config_calls.append(service_handle)
            fake.start_type = "disabled"

        fake.change_startup_disabled = change
        opened = []
        original_open = fake.open_service

        def tracking_open(advapi32, scm_handle, desired_access):
            opened.append(desired_access)
            return original_open(advapi32, scm_handle, desired_access)

        fake.open_service = tracking_open
        install_scm_fake(self, fake)

        result = svc.disable_openrgb_service(self.EXPECTED)
        self.assertTrue(result.ok)

        mutation_opens = [
            access for access in opened if access & svc.SERVICE_CHANGE_CONFIG
        ]
        self.assertEqual(
            len(mutation_opens),
            1,
            f"expected exactly one mutation open, got {opened!r}",
        )
        self.assertEqual(
            mutation_opens[0],
            expected_mask,
            "mutation mask must be QUERY_CONFIG|QUERY_STATUS|STOP|CHANGE_CONFIG "
            f"(got {mutation_opens[0]:#x}, want {expected_mask:#x})",
        )
        # All four required rights are present.
        for right in (
            svc.SERVICE_QUERY_CONFIG,
            svc.SERVICE_QUERY_STATUS,
            svc.SERVICE_STOP,
            svc.SERVICE_CHANGE_CONFIG,
        ):
            self.assertEqual(
                mutation_opens[0] & right,
                right,
                f"missing required right {right:#x}",
            )
        # And nothing broader than those four (this also rejects ALL_ACCESS).
        self.assertEqual(mutation_opens[0] & ~expected_mask, 0)

    def test_final_verification_uses_query_config_on_the_mutation_handle(self):
        """ScmFake enforces SERVICE_QUERY_CONFIG; a full repair must succeed."""
        fake = ScmFake(
            state=svc.SERVICE_STOPPED,
            start_type=svc.SERVICE_DEMAND_START,
            binary_path=self.EXPECTED,
        )

        def change(advapi32, service_handle):
            fake.change_config_calls.append(service_handle)
            fake.start_type = "disabled"

        fake.change_startup_disabled = change
        install_scm_fake(self, fake)
        result = svc.disable_openrgb_service(self.EXPECTED)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)

    def test_wait_saying_stopped_cannot_override_a_running_final_query(self):
        """The FINAL re-query is authoritative.

        A wait helper that reports stopped must not turn a contradictory final
        RUNNING (or START_PENDING) result into success.
        """
        for final_state in (svc.SERVICE_RUNNING, svc.SERVICE_START_PENDING):
            with self.subTest(final_state=final_state):
                fake = ScmFake(
                    state=svc.SERVICE_RUNNING,
                    start_type=svc.SERVICE_AUTO_START,
                    binary_path=self.EXPECTED,
                )

                def change(advapi32, service_handle, fake=fake):
                    fake.change_config_calls.append(service_handle)
                    fake.start_type = "disabled"

                def request_stop(advapi32, service_handle, fake=fake):
                    fake.control_calls.append(service_handle)
                    return True

                def wait_stopped(advapi32, service_handle, timeout_seconds, fake=fake, fs=final_state):
                    # Wait claims success...
                    return True
                    # ...but the service is NOT actually stopped. Leave state as-is.

                def query_status(advapi32, service_handle, fake=fake, fs=final_state):
                    # Final re-query contradicts the wait.
                    return types.SimpleNamespace(dwCurrentState=fs)

                def query_config(advapi32, service_handle, fake=fake):
                    return "disabled", fake.binary_path

                fake.change_startup_disabled = change
                fake.request_stop = request_stop
                fake.wait_stopped = wait_stopped
                fake.query_status = query_status
                fake.query_config = query_config
                install_scm_fake(self, fake)

                result = svc.disable_openrgb_service(self.EXPECTED)
                self.assertFalse(result.ok, "wait=True must not override a non-stopped final state")
                self.assertNotEqual(result.exit_code, 0)
                self.assertNotIn("stopped and disabled", result.message)


# ---------------------------------------------------------
# 5. Elevation boundary
# ---------------------------------------------------------
class TestElevationBoundary(unittest.TestCase):
    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def test_only_explicit_ui_action_reaches_service_mutation(self):
        if app is None:
            self.skipTest(SKIP_REASON)
        source = inspect.getsource(app)
        # The elevated request must appear only in the explicit repair method.
        self.assertIn("request_elevated_disable_openrgb_service", source)
        # Automatic paths must not mention it.
        for name in (
            "_execute_suspend_actions",
            "trigger_resume",
            "on_resume_completed",
            "trigger_suspend",
            "check_system_statuses",
            "on_solar_update",
        ):
            method = getattr(app.YeelightPCCompanionWindow, name, None) or getattr(
                app.RestoreEngineThread, name, None
            )
            if method is None:
                continue
            body = inspect.getsource(method)
            self.assertNotIn("request_elevated_disable_openrgb_service", body, name)
            self.assertNotIn("disable_openrgb_service", body, name)

    def test_restore_engine_never_elevates_for_the_service(self):
        if app is None:
            self.skipTest(SKIP_REASON)
        source = inspect.getsource(app.RestoreEngineThread)
        self.assertNotIn("request_elevated_disable_openrgb_service", source)
        self.assertNotIn("ShellExecute", source)
        self.assertNotIn('"runas"', source.lower())

    def test_suspend_path_has_no_service_mutation(self):
        if app is None:
            self.skipTest(SKIP_REASON)
        source = inspect.getsource(app.YeelightPCCompanionWindow._execute_suspend_actions)
        self.assertNotIn("disable_openrgb_service", source)
        self.assertNotIn("ChangeServiceConfig", source)
        self.assertNotIn("ControlService", source)
        self.assertNotIn("openrgb_service", source)
        self.assertNotIn("runas", source.lower())
        self.assertNotIn("ShellExecute", source)

    def test_suspend_budget_tests_contract_is_untouched(self):
        if app is None:
            self.skipTest(SKIP_REASON)
        self.assertEqual(app.SUSPEND_SEQUENCE_TARGET_SECONDS, 1.5)
        self.assertLess(app.suspend_budgeted_seconds(), app.SUSPEND_SEQUENCE_TARGET_SECONDS)
        self.assertEqual(wt.END_TASK_STOP_BUDGET_SECONDS, 0.4)

    def test_status_polling_does_not_elevate_or_mutate(self):
        if app is None:
            self.skipTest(SKIP_REASON)
        source = inspect.getsource(app.YeelightPCCompanionWindow.check_system_statuses)
        self.assertNotIn("disable_openrgb_service", source)
        self.assertNotIn("request_elevated", source)

    def test_probe_is_read_only_minimum_access(self):
        source = inspect.getsource(svc.probe_openrgb_service)
        self.assertIn("SERVICE_QUERY_CONFIG", source)
        self.assertIn("SERVICE_QUERY_STATUS", source)
        self.assertNotIn("SERVICE_CHANGE_CONFIG", source)
        self.assertNotIn("SERVICE_STOP", source)

    def test_no_arbitrary_service_name_parameter_on_mutation(self):
        parameters = inspect.signature(svc.disable_openrgb_service).parameters
        self.assertNotIn("service_name", parameters)
        self.assertNotIn("name", parameters)
        self.assertIn("expected_openrgb_path", parameters)


# ---------------------------------------------------------
# 6. UI presentation
# ---------------------------------------------------------
# Full-window UI coverage lives in tests/test_ui.py (that harness already
# constructs YeelightPCCompanionWindow reliably). This class pins the
# presentation contract at the source level so a regression in the Integrations
# card wiring is caught even when Qt is unavailable.
class TestServiceConflictUiSurface(unittest.TestCase):
    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_refresh_and_repair_surface_is_wired(self):
        source = inspect.getsource(app.YeelightPCCompanionWindow)
        self.assertIn("lbl_openrgb_service", source)
        self.assertIn("btn_openrgb_service", source)
        self.assertIn("repair_openrgb_service_conflict", source)
        self.assertIn("DISABLE_SERVICE_ACTION_LABEL", source)
        repair = inspect.getsource(app.YeelightPCCompanionWindow._repair_openrgb_service_conflict)
        self.assertIn("trigger_resume", repair)
        self.assertNotIn("RestoreEngineThread(", repair)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_confirm_dialog_explains_stop_and_disable(self):
        source = inspect.getsource(app.YeelightPCCompanionWindow._confirm_openrgb_service_disable)
        self.assertIn("stop", source.lower())
        self.assertIn("Disabled", source)
        self.assertIn("Continue?", source)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_fix_button_enabled_only_when_can_auto_fix(self):
        source = inspect.getsource(app.YeelightPCCompanionWindow._refresh_openrgb_service_status)
        self.assertIn("can_auto_fix", source)
        self.assertIn("setEnabled", source)

    @unittest.skipIf(app is None, SKIP_REASON)
    def test_repair_is_refused_during_sleep_transition(self):
        source = inspect.getsource(app.YeelightPCCompanionWindow.repair_openrgb_service_conflict)
        self.assertIn("_sleep_transition_active", source)
        repair = inspect.getsource(app.YeelightPCCompanionWindow._repair_openrgb_service_conflict)
        self.assertIn("request_elevated_disable_openrgb_service", repair)

    def test_card_declares_a_separate_service_status_area(self):
        source = inspect.getsource(
            __import__("ui_components").IntegrationCard.__init__
        )
        self.assertIn("with_service_status", source)
        self.assertIn("Windows service", source)
        self.assertIn("lbl_service_status", source)


# ---------------------------------------------------------
# 7. Restore behaviour
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestRestoreServiceDiagnostics(unittest.TestCase):
    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def make_stub(self, running=True):
        class RestoreStub:
            run = app.RestoreEngineThread.run
            _report_openrgb_service_owned_instance = (
                app.RestoreEngineThread._report_openrgb_service_owned_instance
            )
            wait_for_openrgb_ready = app.RestoreEngineThread.wait_for_openrgb_ready
            launch_openrgb = app.RestoreEngineThread.launch_openrgb
            kill_process = app.RestoreEngineThread.kill_process
            is_process_running = app.RestoreEngineThread.is_process_running
            integration_path_available = (
                app.RestoreEngineThread.integration_path_available
            )

            def __init__(self):
                self.running = True
                self.messages = []
                self.launched = []
                self.progress_update = types.SimpleNamespace(emit=self.messages.append)
                self.finished_sequence = types.SimpleNamespace(emit=lambda: None)
                self._process_running = running
                self._killed = []

            def sleep(self, _seconds):
                return None

            def is_process_running(self, name):
                return self._process_running

            def kill_process(self, name):
                self._killed.append(name)
                return None

            def integration_path_available(self, key, path):
                return True

            def launch_process(self, path, args=None, hidden=True, cwd=None):
                self.launched.append(path)

            def launch_openrgb(self, path):
                self.launched.append(path)
                return True

            def wait_for_openrgb_ready(self, launched=False):
                self.messages.append(f"gate:{launched}")
                return True

            def evaluate_solar_state(self, config):
                return False

            def power_devices(self, device_ips, turn_on=False):
                return None

        return RestoreStub()

    def test_service_owned_running_openrgb_is_not_followed_by_a_second_launch(self):
        stub = self.make_stub(running=True)
        import config_manager as cm

        manager = mock.Mock()
        config = cm.default_config()
        config["integrations"]["openrgb"]["enabled"] = True
        config["paths"]["openrgb"] = self.EXPECTED
        manager.load.return_value = config
        stub.config_manager = manager

        def enabled(_config, key, default=False):
            return key == "openrgb"

        with mock.patch.object(svc, "probe_openrgb_service") as probe:
            probe.return_value = svc.OpenRgbServiceProbe(
                exists=True,
                state="running",
                start_type="automatic",
                binary_path=self.EXPECTED,
            )
            with mock.patch.object(app, "integration_enabled", side_effect=enabled):
                with mock.patch.object(app, "enabled_device_ips", return_value=[]):
                    stub.run()

        # Never a second launch while one is already running.
        self.assertEqual([p for p in stub.launched if "OpenRGB" in p], [])
        self.assertTrue(any("gate:False" in m for m in stub.messages))

    def test_warning_identifies_the_service_conflict(self):
        stub = self.make_stub(running=True)
        with mock.patch.object(svc, "probe_openrgb_service") as probe:
            probe.return_value = svc.OpenRgbServiceProbe(
                exists=True,
                state="running",
                start_type="automatic",
                binary_path=self.EXPECTED,
            )
            stub._report_openrgb_service_owned_instance()
        self.assertTrue(
            any("OpenRGB Windows service is active" in m for m in stub.messages)
        )
        self.assertTrue(
            any("Integrations" in m for m in stub.messages)
        )

    def test_no_service_means_no_conflict_warning(self):
        stub = self.make_stub(running=True)
        with mock.patch.object(svc, "probe_openrgb_service") as probe:
            probe.return_value = svc.OpenRgbServiceProbe(exists=False, error="")
            stub._report_openrgb_service_owned_instance()
        self.assertFalse(
            any("OpenRGB Windows service is active" in m for m in stub.messages)
        )

    def test_stopped_manual_service_does_not_warn(self):
        stub = self.make_stub(running=True)
        with mock.patch.object(svc, "probe_openrgb_service") as probe:
            probe.return_value = svc.OpenRgbServiceProbe(
                exists=True,
                state="stopped",
                start_type="manual",
                binary_path=self.EXPECTED,
            )
            stub._report_openrgb_service_owned_instance()
        self.assertFalse(
            any("OpenRGB Windows service is active" in m for m in stub.messages)
        )

    def test_report_failure_never_breaks_the_restore(self):
        stub = self.make_stub(running=True)
        with mock.patch.object(svc, "probe_openrgb_service", side_effect=RuntimeError("x")):
            stub._report_openrgb_service_owned_instance()  # must not raise


# ---------------------------------------------------------
# 8. Suspend contract
# ---------------------------------------------------------
@unittest.skipIf(app is None, SKIP_REASON)
class TestSuspendContract(unittest.TestCase):
    def test_budget_stays_strictly_below_target(self):
        self.assertLess(app.suspend_budgeted_seconds(), app.SUSPEND_SEQUENCE_TARGET_SECONDS)
        self.assertLessEqual(wt.END_TASK_STOP_BUDGET_SECONDS, 0.5)
        self.assertLess(
            wt.END_TASK_STOP_BUDGET_SECONDS, app.SUSPEND_SEQUENCE_TARGET_SECONDS / 3
        )

    def test_service_stop_budget_is_not_the_suspend_budget(self):
        # The service repair path may wait longer; it must not share the 0.4 s
        # constant and must not enlarge it.
        self.assertNotEqual(
            svc.OPENRGB_SERVICE_DISABLE_TIMEOUT_SECONDS,
            wt.END_TASK_STOP_BUDGET_SECONDS,
        )
        self.assertGreater(
            svc.OPENRGB_SERVICE_DISABLE_TIMEOUT_SECONDS,
            wt.END_TASK_STOP_BUDGET_SECONDS,
        )
        self.assertLessEqual(wt.END_TASK_STOP_BUDGET_SECONDS, 0.4)

    def test_suspend_source_contains_no_service_control(self):
        source = inspect.getsource(app.YeelightPCCompanionWindow._execute_suspend_actions)
        for token in (
            "ChangeServiceConfig",
            "ControlService",
            "OpenService",
            "probe_openrgb_service",
            "disable_openrgb_service",
            "request_elevated_disable",
        ):
            self.assertNotIn(token, source)

    def test_no_new_uac_in_suspend_or_solar(self):
        for name in (
            "_execute_suspend_actions",
            "on_solar_update",
            "trigger_suspend",
        ):
            method = getattr(app.YeelightPCCompanionWindow, name)
            source = inspect.getsource(method)
            self.assertNotIn("runas", source.lower(), name)
            self.assertNotIn("ShellExecute", source, name)


# ---------------------------------------------------------
# 9. Security surface
# ---------------------------------------------------------
class TestSecuritySurface(unittest.TestCase):
    def test_service_name_is_a_fixed_constant(self):
        self.assertEqual(svc.OPENRGB_SERVICE_NAME, "OpenRGB")

    def test_disable_helper_uses_only_the_fixed_service_name(self):
        source = inspect.getsource(svc._open_service)
        self.assertIn("OPENRGB_SERVICE_NAME", source)
        # No caller-supplied service name anywhere in the mutation path.
        for fn in (
            svc.disable_openrgb_service,
            svc._open_service,
            svc.probe_openrgb_service,
        ):
            parameters = inspect.signature(fn).parameters
            self.assertNotIn("service_name", parameters)

    def test_expected_path_is_only_an_identity_input(self):
        source = inspect.getsource(svc.disable_openrgb_service)
        self.assertIn("service_binary_matches", source)
        # The expected path must never be executed or handed to a shell.
        self.assertNotIn("subprocess", source)
        self.assertNotIn("Popen", source)
        self.assertNotIn("ShellExecute", source)
        self.assertNotIn("CreateProcess", source)

    def test_no_credentials_are_read_or_stored(self):
        source = inspect.getsource(svc)
        lowered = source.lower()
        # No credential handling. (The SCM ChangeServiceConfigW signature has
        # an unused lpPassword slot required by the ABI; that is not a secret.)
        self.assertNotIn("credential", lowered)
        self.assertNotIn("getpassword", lowered)
        self.assertNotIn("api_key", lowered)
        self.assertNotIn("secret", lowered)

    def test_probe_opens_with_minimum_access(self):
        source = inspect.getsource(svc.probe_openrgb_service)
        self.assertIn("SC_MANAGER_CONNECT", inspect.getsource(svc._open_scm))
        self.assertIn("SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS", source)


if __name__ == "__main__":
    unittest.main()
