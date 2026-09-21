"""Tests for the per-user start-at-logon model and the narrow task-removal CLI.

Stage 7 replaced the retired *elevated scheduled-task* startup with a per-user
``HKCU\\...\\Run`` entry, and moved OpenRGB task removal onto the application's
existing narrow CLI (used by the uninstaller).

These tests never touch the real registry, Task Scheduler or UAC. Registry access
goes through an in-memory fake, and ``schtasks`` is injected as a recorder.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import config_manager as cm  # noqa: E402
import windows_tasks as wt  # noqa: E402


def _strip_docstrings(source):
    """Return *source* with every module/class/function docstring removed.

    Used by the source-shape assertions so that prose documenting a rule can
    neither satisfy nor break them.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:]
    return ast.unparse(tree)


class FakeWinReg:
    """Minimal in-memory stand-in for the ``winreg`` module.

    Only the handful of calls :mod:`config_manager` makes are implemented, plus
    enough fidelity that a bug in the startup helpers shows up: values are stored
    per key, and a missing value raises ``FileNotFoundError`` exactly like the
    real module does.
    """

    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self):
        self.keys = {}

    # -- helpers used by the tests ------------------------------------

    def seed(self, subkey, name, value):
        self.keys.setdefault(subkey, {})[name] = value

    def values(self, subkey):
        return dict(self.keys.get(subkey, {}))

    # -- winreg API ----------------------------------------------------

    def OpenKey(self, root, subkey, reserved=0, access=0):  # noqa: N802
        if subkey not in self.keys:
            raise FileNotFoundError(f"no such key: {subkey}")
        return _FakeKey(self, subkey)

    def CreateKeyEx(self, root, subkey, reserved=0, access=0):  # noqa: N802
        self.keys.setdefault(subkey, {})
        return _FakeKey(self, subkey)

    def QueryValueEx(self, key, name):  # noqa: N802
        store = self.keys.get(key.subkey, {})
        if name not in store:
            raise FileNotFoundError(f"no such value: {name}")
        return store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, type_, value):  # noqa: N802
        self.keys.setdefault(key.subkey, {})[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        store = self.keys.get(key.subkey, {})
        if name not in store:
            raise FileNotFoundError(f"no such value: {name}")
        del store[name]


class _FakeKey:
    def __init__(self, module, subkey):
        self.module = module
        self.subkey = subkey

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StartupRegistrationTestCase(unittest.TestCase):
    """Base class that swaps the registry for a fake."""

    def setUp(self):
        self.registry = FakeWinReg()
        self._original_winreg = cm._winreg
        cm._winreg = lambda: self.registry

    def tearDown(self):
        cm._winreg = self._original_winreg


class TestStartupRegistration(StartupRegistrationTestCase):
    def test_nothing_is_registered_by_default(self):
        self.assertFalse(cm.is_startup_enabled())
        self.assertIsNone(cm.startup_entry())

    def test_enable_writes_a_single_run_value(self):
        command = cm.enable_startup(r"C:\Tools\YeelightPCCompanion.exe")

        self.assertTrue(cm.is_startup_enabled())
        self.assertEqual(
            self.registry.values(cm.STARTUP_RUN_KEY),
            {cm.STARTUP_VALUE_NAME: command},
        )
        # No spaces: Windows needs no quotes, so none must be added.
        self.assertEqual(command, r"C:\Tools\YeelightPCCompanion.exe --tray")

    def test_the_entry_lives_in_the_per_user_run_key(self):
        cm.enable_startup(r"C:\Tools\app.exe")
        self.assertIn("Software\\Microsoft\\Windows\\CurrentVersion\\Run", cm.STARTUP_RUN_KEY)
        self.assertNotIn("HKLM", cm.STARTUP_RUN_KEY)

    def test_paths_with_spaces_are_quoted(self):
        command = cm.enable_startup(r"C:\Program Files\Yeelight PC Companion\YeelightPCCompanion.exe")
        self.assertTrue(command.startswith('"'))
        self.assertIn("YeelightPCCompanion.exe", command)
        self.assertTrue(command.endswith("--tray"))
        # Exactly one quoted element: the executable, not the arguments.
        self.assertEqual(command.count('"'), 2)

    def test_enable_starts_in_the_tray(self):
        command = cm.enable_startup(r"C:\Tools\app.exe")
        self.assertIn("--tray", command)

    def test_enable_is_idempotent(self):
        first = cm.enable_startup(r"C:\Tools\app.exe")
        second = cm.enable_startup(r"C:\Tools\app.exe")
        self.assertEqual(first, second)
        self.assertEqual(len(self.registry.values(cm.STARTUP_RUN_KEY)), 1)

    def test_enable_updates_a_moved_installation(self):
        cm.enable_startup(r"C:\Old\app.exe")
        cm.enable_startup(r"C:\New\app.exe")
        self.assertIn("New", cm.startup_entry())
        self.assertNotIn("Old", cm.startup_entry())

    def test_disable_removes_the_entry(self):
        cm.enable_startup(r"C:\Tools\app.exe")
        self.assertTrue(cm.disable_startup())
        self.assertFalse(cm.is_startup_enabled())

    def test_disable_is_not_an_error_when_absent(self):
        self.assertFalse(cm.disable_startup())

    def test_disable_leaves_other_programs_alone(self):
        """Only this application's own value may ever be removed."""
        self.registry.seed(cm.STARTUP_RUN_KEY, "SomeOtherApp", r'"C:\Other\other.exe"')
        self.registry.seed(cm.STARTUP_RUN_KEY, "OneDrive", r'"C:\OneDrive.exe"')
        cm.enable_startup(r"C:\Tools\app.exe")

        cm.disable_startup()

        remaining = self.registry.values(cm.STARTUP_RUN_KEY)
        self.assertNotIn(cm.STARTUP_VALUE_NAME, remaining)
        self.assertEqual(
            remaining,
            {"SomeOtherApp": r'"C:\Other\other.exe"', "OneDrive": r'"C:\OneDrive.exe"'},
        )

    def test_enable_leaves_other_programs_alone(self):
        self.registry.seed(cm.STARTUP_RUN_KEY, "OneDrive", r'"C:\OneDrive.exe"')
        cm.enable_startup(r"C:\Tools\app.exe")
        self.assertIn("OneDrive", self.registry.values(cm.STARTUP_RUN_KEY))

    def test_enable_creates_the_key_when_it_does_not_exist(self):
        self.assertNotIn(cm.STARTUP_RUN_KEY, self.registry.keys)
        cm.enable_startup(r"C:\Tools\app.exe")
        self.assertIn(cm.STARTUP_RUN_KEY, self.registry.keys)

    def test_enable_reports_when_the_registry_is_unavailable(self):
        cm._winreg = lambda: None
        with self.assertRaises(RuntimeError):
            cm.enable_startup(r"C:\Tools\app.exe")

    def test_disable_is_a_no_op_when_the_registry_is_unavailable(self):
        cm._winreg = lambda: None
        self.assertFalse(cm.disable_startup())
        self.assertFalse(cm.is_startup_enabled())

    def test_default_target_is_the_running_interpreter_when_frozen(self):
        had_frozen = hasattr(sys, "frozen")
        original = getattr(sys, "frozen", None)
        sys.frozen = True
        try:
            command = cm.enable_startup()
            self.assertIn(os.path.basename(sys.executable), command)
        finally:
            if had_frozen:
                sys.frozen = original
            else:
                del sys.frozen


class TestLegacyStartupTaskMigration(unittest.TestCase):
    """The retired elevated logon task must be removable, and only it."""

    def _recorder(self, existing, fail_delete=()):
        calls = []

        def run(args):
            calls.append(list(args))
            if args[0] == "/query":
                return 0 if args[2] in existing else 1
            if args[0] == "/delete":
                if args[2] in fail_delete:
                    return 1
                return 0
            return 1

        return run, calls

    def test_only_the_known_legacy_tasks_are_removed(self):
        run, calls = self._recorder({"YeelightPCCompanion", "LuminaLightOrchestrator"})
        removed = cm.remove_legacy_startup_tasks(run_schtasks=run)

        self.assertEqual(set(removed), set(cm.STARTUP_LEGACY_TASK_NAMES))
        queried = {call[2] for call in calls if call[0] == "/query"}
        self.assertEqual(queried, set(cm.STARTUP_LEGACY_TASK_NAMES))
        # The OpenRGB elevation task is a different task and must never appear.
        self.assertNotIn("YeelightPCCompanion-OpenRGB", queried)

    def test_absent_tasks_are_not_an_error(self):
        run, calls = self._recorder(set())
        self.assertEqual(cm.remove_legacy_startup_tasks(run_schtasks=run), [])
        self.assertFalse([call for call in calls if call[0] == "/delete"])

    def test_a_failed_removal_is_reported_not_raised(self):
        run, _ = self._recorder({"YeelightPCCompanion"}, fail_delete={"YeelightPCCompanion"})
        self.assertEqual(cm.remove_legacy_startup_tasks(run_schtasks=run), [])

    def test_the_legacy_names_are_fixed_constants(self):
        self.assertEqual(
            cm.STARTUP_LEGACY_TASK_NAMES,
            ("YeelightPCCompanion", "LuminaLightOrchestrator"),
        )

    def test_the_owned_value_name_is_fixed(self):
        self.assertEqual(cm.STARTUP_VALUE_NAME, "YeelightPCCompanion")


class TestOpenRgbTaskRemovalCliIsNarrow(unittest.TestCase):
    """The uninstaller reuses this CLI, so its surface must stay minimal."""

    def test_the_remove_flag_takes_no_arguments(self):
        action, path = wt.parse_provisioning_argv(["app.exe", "--remove-openrgb-task"])
        self.assertEqual(action, wt.ACTION_REMOVE)
        self.assertEqual(path, "")

    def test_extra_arguments_are_rejected(self):
        for extra in ("YeelightPCCompanion-OpenRGB", "/f", "SomeOtherTask"):
            with self.subTest(extra=extra):
                with self.assertRaises(wt.WindowsTaskError):
                    wt.parse_provisioning_argv(["app.exe", "--remove-openrgb-task", extra])

    def test_there_is_no_generic_task_name_interface(self):
        """No flag may let a caller choose an arbitrary task or command."""
        for flag in ("--task-name", "--run-command", "--arguments", "--command", "--tn"):
            with self.subTest(flag=flag):
                # An unknown flag is either ignored as an ordinary start or
                # rejected outright - it never becomes an action.
                try:
                    result = wt.parse_provisioning_argv(["app.exe", flag, "YeelightPCCompanion-OpenRGB"])
                except wt.WindowsTaskError:
                    continue
                self.assertIsNone(result, f"{flag} must not be accepted as a request")

    def test_the_task_name_is_a_fixed_constant(self):
        self.assertEqual(wt.OPENRGB_TASK_NAME, "YeelightPCCompanion-OpenRGB")

    def test_an_ordinary_start_is_not_a_provisioning_request(self):
        self.assertIsNone(wt.parse_provisioning_argv(["app.exe"]))
        self.assertIsNone(wt.parse_provisioning_argv(["app.exe", "--tray"]))

    def test_removal_only_ever_targets_the_fixed_task(self):
        """`remove_openrgb_task` must not accept a task name at all."""
        import inspect

        parameters = inspect.signature(wt.remove_openrgb_task).parameters
        self.assertEqual(list(parameters), [])
        source = inspect.getsource(wt.remove_openrgb_task)
        # `/delete` is issued against the constant, never a caller-supplied string.
        self.assertIn("OPENRGB_TASK_NAME", source)


class TestTaskAwareOpenRgbStopContract(unittest.TestCase):
    """The sleep-path stop must stay non-raising and task-scoped."""

    def test_outcome_constants_are_distinct(self):
        outcomes = {
            wt.END_TASK_STOPPED,
            wt.END_TASK_NOT_RUNNING,
            wt.END_TASK_ABSENT,
            wt.END_TASK_FAILED,
        }
        self.assertEqual(len(outcomes), 4)

    def test_end_task_only_targets_the_elevation_task(self):
        import inspect

        source = inspect.getsource(wt.end_openrgb_task)
        self.assertIn("OPENRGB_TASK_NAME", source)
        self.assertIn('"/end"', source)
        # It must never fall back to an interactive elevation prompt.
        self.assertNotIn("runas", source)

    def test_end_task_has_no_task_name_parameter(self):
        import inspect

        parameters = inspect.signature(wt.end_openrgb_task).parameters
        self.assertNotIn("task_name", parameters)
        self.assertNotIn("name", parameters)
        # ... and no caller-chosen timing either: the deadline is the module's.
        self.assertEqual(list(parameters), [])

    def test_end_task_is_bounded(self):
        self.assertLessEqual(wt.END_TASK_STOP_BUDGET_SECONDS, 0.5)
        self.assertGreater(wt.END_TASK_STOP_BUDGET_SECONDS, 0)

    def test_end_task_never_queries_the_task_first(self):
        """A `/query` on the suspend path is exactly the bug that was fixed.

        Querying cost up to `SCHTASKS_TIMEOUT_SECONDS` (30 s) before `/end` even
        started, on a path whose whole budget is one 1.5 s sequence. The check is
        made against the *executable* statements (docstrings stripped), so prose
        describing the rule cannot satisfy or break it.
        """
        import ast
        import inspect

        source = inspect.getsource(wt.end_openrgb_task)
        self.assertNotIn("query_openrgb_task", source)
        code = _strip_docstrings(source)
        tree = ast.parse(code)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run_schtasks"
        ]
        self.assertEqual(len(calls), 1, "the stop must invoke schtasks exactly once")
        argument = calls[0].args[0]
        self.assertIsInstance(argument, ast.List)
        self.assertEqual(
            [element.value for element in argument.elts if isinstance(element, ast.Constant)],
            ["/end", "/tn"],
        )
        # No `/query` action can be built anywhere in the executable statements.
        self.assertNotIn('"/query"', code)

    def test_end_task_contains_no_second_or_third_deadline(self):
        """One global deadline: no per-call timeout constant may reappear."""
        import inspect

        source = inspect.getsource(wt.end_openrgb_task)
        self.assertNotIn("END_TASK_TIMEOUT_SECONDS", source)
        self.assertNotIn("SCHTASKS_TIMEOUT_SECONDS", source)
        self.assertNotIn("poll_timeout", source)
        # The only timeout handed to schtasks is the remaining global budget.
        self.assertIn("remaining()", source)

    def test_a_fixed_suspend_budget_is_smaller_than_a_worst_case_query(self):
        self.assertLess(wt.END_TASK_STOP_BUDGET_SECONDS, wt.SCHTASKS_TIMEOUT_SECONDS)
        self.assertLess(wt.END_TASK_STOP_BUDGET_SECONDS, 1.0)

    def test_end_task_never_raises_when_schtasks_is_missing(self):
        original = wt._run_schtasks
        original_pids = wt._openrgb_process_ids

        wt._openrgb_process_ids = lambda: [1234]
        wt._run_schtasks = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no schtasks"))
        try:
            self.assertEqual(wt.end_openrgb_task(), wt.END_TASK_FAILED)
        finally:
            wt._run_schtasks = original
            wt._openrgb_process_ids = original_pids

    def test_end_task_reports_absent_when_there_is_no_task(self):
        """An absent task is classified from the `/end` result, without a query."""
        original = wt._run_schtasks
        wt._run_schtasks = lambda *a, **k: (
            1,
            "",
            "ERROR: The system cannot find the file specified.",
        )
        try:
            self.assertEqual(wt.end_openrgb_task(), wt.END_TASK_ABSENT)
        finally:
            wt._run_schtasks = original

    def test_end_task_reports_not_running_from_the_reported_wording(self):
        original = wt._run_schtasks
        wt._run_schtasks = lambda *a, **k: (
            1,
            "ERROR: The task is currently not running.",
            "",
        )
        try:
            self.assertEqual(wt.end_openrgb_task(), wt.END_TASK_NOT_RUNNING)
        finally:
            wt._run_schtasks = original


class _FakeClock:
    """A deterministic monotonic clock advanced only by the fake sleep."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


class TestTaskAwareStopGlobalDeadline(unittest.TestCase):
    """The *whole* task-aware stop shares ONE 0.4 s monotonic deadline.

    Every case here is driven by a fake clock and a fake `schtasks`, so the
    timing contract is asserted deterministically and the machine never sleeps.
    `os.name` is patched to ``"nt"`` so the Windows code path is exercised on any
    host; `_openrgb_process_ids` is replaced, so no real process is inspected.
    """

    def setUp(self):
        self.clock = _FakeClock()
        self.schtasks_calls = []
        self.pids_script = []
        self._patchers = [
            mock.patch.object(wt.os, "name", "nt"),
            mock.patch.object(wt, "_end_task_monotonic", self.clock.monotonic),
            mock.patch.object(wt, "_end_task_sleep", self.clock.sleep),
            mock.patch.object(wt, "_openrgb_process_ids", self._next_pids),
            mock.patch.object(wt, "_run_schtasks", self._schtasks),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- fakes ---------------------------------------------------------

    def _next_pids(self):
        if self.pids_script:
            value = self.pids_script.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return []

    def _schtasks(self, args, timeout=None):
        self.schtasks_calls.append((list(args), timeout))
        if self.schtasks_result is None:  # pragma: no cover - a test bug
            raise AssertionError("no scripted schtasks result")
        if isinstance(self.schtasks_result, Exception):
            raise self.schtasks_result
        if callable(self.schtasks_result):
            return self.schtasks_result(args, timeout)
        return self.schtasks_result

    # -- assertions ----------------------------------------------------

    def _assert_within_budget(self):
        self.assertLessEqual(
            self.clock.now - self.clock_start,
            wt.END_TASK_STOP_BUDGET_SECONDS,
            "the task-aware stop consumed more than its global deadline",
        )

    def run_with(self, schtasks_result, pids_script):
        self.schtasks_result = schtasks_result
        self.pids_script = list(pids_script)
        self.clock_start = self.clock.now
        outcome = wt.end_openrgb_task()
        self._assert_within_budget()
        return outcome

    # -- cases ---------------------------------------------------------

    def test_missing_task_is_globally_bounded(self):
        outcome = self.run_with(
            (1, "", "ERROR: The system cannot find the file specified."), []
        )
        self.assertEqual(outcome, wt.END_TASK_ABSENT)
        self.assertEqual([call[0] for call in self.schtasks_calls], [["/end", "/tn", wt.OPENRGB_TASK_NAME]])

    def test_a_hung_schtasks_is_globally_bounded(self):
        """The old code could spend 30 s here; the budget is now 0.4 s."""
        import subprocess

        def hang(args, timeout):
            # A hung `schtasks.exe` is exactly a subprocess timeout at the
            # timeout the module handed it - it never gets more than the budget.
            raise subprocess.TimeoutExpired(cmd="schtasks.exe", timeout=timeout or 0.0)

        outcome = self.run_with(hang, [])
        self.assertEqual(outcome, wt.END_TASK_FAILED)
        # The whole thing still fit inside the one deadline.
        self.assertLessEqual(
            self.clock.now - self.clock_start, wt.END_TASK_STOP_BUDGET_SECONDS
        )
        # And schtasks was never given more time than that deadline.
        timeout = self.schtasks_calls[0][1]
        self.assertIsNotNone(timeout)
        self.assertLessEqual(timeout, wt.END_TASK_STOP_BUDGET_SECONDS)

    def test_accepted_end_whose_process_never_disappears_is_globally_bounded(self):
        """A stop Task Scheduler accepts but that does not take effect cannot hang."""
        outcome = self.run_with((0, "SUCCESS: terminated", ""), [[4242]] * 200)
        self.assertEqual(outcome, wt.END_TASK_FAILED)
        self.assertLessEqual(
            self.clock.now - self.clock_start, wt.END_TASK_STOP_BUDGET_SECONDS
        )
        # At least one verification happened, and no unbounded polling did.
        self.assertGreaterEqual(self.clock.now - self.clock_start, 0.0)

    def test_normal_success_works(self):
        """A stop that has already taken effect: no wait beyond the first check."""
        outcome = self.run_with((0, "SUCCESS: terminated", ""), [[4242], []])
        self.assertEqual(outcome, wt.END_TASK_STOPPED)
        self.assertLessEqual(
            self.clock.now - self.clock_start, wt.END_TASK_VERIFY_INTERVAL_SECONDS
        )
        self.assertAlmostEqual(self.schtasks_calls[0][1], wt.END_TASK_STOP_BUDGET_SECONDS)

    def test_success_after_one_verification_wait_still_succeeds(self):
        outcome = self.run_with((0, "SUCCESS: terminated", ""), [[4242], [4242], []])
        self.assertEqual(outcome, wt.END_TASK_STOPPED)
        self.assertGreater(self.clock.now - self.clock_start, 0.0)
        self.assertLessEqual(
            self.clock.now - self.clock_start, wt.END_TASK_STOP_BUDGET_SECONDS
        )

    def test_an_unreadable_process_list_is_not_trusted_as_stopped(self):
        """`None` means "could not read", never "nothing is running"."""
        outcome = self.run_with((0, "SUCCESS: terminated", ""), [None] * 200)
        self.assertEqual(outcome, wt.END_TASK_FAILED)

    def test_an_unexpected_failure_wording_is_reported_as_failed(self):
        outcome = self.run_with((1, "", "ERROR: Access is denied."), [])
        self.assertEqual(outcome, wt.END_TASK_FAILED)

    def test_a_success_code_never_bypasses_the_verification(self):
        """Exit code 0 is not enough: the process must actually be gone."""
        seen = []

        def _pids():
            seen.append(True)
            return [4242]

        with mock.patch.object(wt, "_openrgb_process_ids", _pids):
            self.schtasks_result = (0, "SUCCESS", "")
            self.pids_script = []
            self.clock_start = self.clock.now
            outcome = wt.end_openrgb_task()

        self.assertEqual(outcome, wt.END_TASK_FAILED)
        self.assertTrue(seen, "the exit code was trusted without verification")


if __name__ == "__main__":
    unittest.main()
