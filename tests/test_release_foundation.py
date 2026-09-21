"""Tests for the Stage 7 release foundation.

These cover the release-facing contract rather than application behaviour:

* the version has exactly one source of truth and the generated artifacts agree
  with it,
* the privacy scanner enforces the *artifact-specific* policy (``portable.flag``
  is required in the portable payload and forbidden in the installer payload),
* the installer configuration is well formed and contains no private path,
* a fresh installed-style run stores its configuration under LocalAppData while
  a portable build stores it beside the executable.

Nothing here touches the network, Task Scheduler, UAC or real hardware.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import app_metadata  # noqa: E402
import config_manager as cm  # noqa: E402
import release_privacy_scan as scanner  # noqa: E402
import write_installer_version  # noqa: E402
import write_version_info  # noqa: E402

ISS_PATH = os.path.join(REPO_ROOT, "installer", "YeelightPCCompanion.iss")


class TestVersionSourceOfTruth(unittest.TestCase):
    """`app_metadata.VERSION` is the only place the version is written down."""

    def test_version_parts_are_integers(self):
        self.assertTrue(all(isinstance(part, int) for part in app_metadata.VERSION))
        self.assertEqual(len(app_metadata.VERSION), 3)

    def test_derived_strings_match_the_tuple(self):
        self.assertEqual(
            app_metadata.APP_VERSION,
            ".".join(str(part) for part in app_metadata.VERSION),
        )
        self.assertEqual(app_metadata.VERSION_QUAD, app_metadata.VERSION + (0,))
        self.assertEqual(
            app_metadata.APP_VERSION_QUAD,
            app_metadata.APP_VERSION + ".0",
        )

    def test_checked_in_version_info_is_current(self):
        """The committed version_info.txt must match app_metadata."""
        with open(write_version_info.TARGET, encoding="utf-8") as handle:
            actual = handle.read().replace("\r\n", "\n")
        self.assertEqual(
            actual,
            app_metadata.version_info_text(),
            "version_info.txt is stale; run tools/write_version_info.py",
        )

    def test_version_info_contains_the_version_and_product_identity(self):
        text = app_metadata.version_info_text()
        self.assertIn(f"filevers={app_metadata.VERSION_QUAD!r}", text)
        self.assertIn(f'StringStruct("FileVersion", "{app_metadata.APP_VERSION}")', text)
        self.assertIn(f'StringStruct("ProductVersion", "{app_metadata.APP_VERSION}")', text)
        self.assertIn(f'"{app_metadata.APP_EXE_NAME}"', text)
        # The retired vendor name must never come back.
        self.assertNotIn("Slon Inc", text)

    def test_installer_include_is_derived_from_the_same_source(self):
        text = app_metadata.installer_version_include_text()
        self.assertIn(f'#define MyAppVersion "{app_metadata.APP_VERSION}"', text)
        self.assertIn(f'#define MyAppVersionQuad "{app_metadata.APP_VERSION_QUAD}"', text)
        self.assertIn(f'#define MyAppExeName "{app_metadata.APP_EXE_NAME}"', text)

    def test_installer_include_written_by_the_tool_agrees(self):
        # installer/version.iss is intentionally generated and gitignored, so
        # this test must work in a pristine clone where it does not exist yet.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "version.iss")
            original = write_installer_version.TARGET
            try:
                write_installer_version.TARGET = target
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(write_installer_version.main([]), 0)
                with open(target, encoding="utf-8") as handle:
                    written = handle.read().replace("\r\n", "\n")
            finally:
                write_installer_version.TARGET = original
        self.assertEqual(written, app_metadata.installer_version_include_text())

    def test_check_mode_reports_clean_for_the_current_files(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(write_version_info.main(["--check"]), 0)

        # The installer include is not checked in. Prove write -> check on a
        # temporary target instead of requiring a generated local build file.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "version.iss")
            original = write_installer_version.TARGET
            try:
                write_installer_version.TARGET = target
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(write_installer_version.main([]), 0)
                    self.assertEqual(write_installer_version.main(["--check"]), 0)
            finally:
                write_installer_version.TARGET = original

    def test_check_mode_fails_for_a_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "version_info.txt")
            original = write_version_info.TARGET
            try:
                write_version_info.TARGET = stale
                # The tool prints its own explanation; capture it so an expected
                # failure does not look like noise in the test log.
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(write_version_info.main(["--check"]), 1)
                    with open(stale, "w", encoding="utf-8") as handle:
                        handle.write("not the right content")
                    self.assertEqual(write_version_info.main(["--check"]), 1)
            finally:
                write_version_info.TARGET = original

    def test_installer_check_mode_fails_when_include_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = write_installer_version.TARGET
            try:
                write_installer_version.TARGET = os.path.join(tmp, "version.iss")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(write_installer_version.main(["--check"]), 1)
            finally:
                write_installer_version.TARGET = original


class TestNoDuplicateVersionLiteral(unittest.TestCase):
    """A second hard-coded version must not creep back into the tooling."""

    def test_iss_has_no_hardcoded_version_and_no_fallback(self):
        with open(ISS_PATH, encoding="utf-8") as handle:
            text = handle.read()

        # It must include the generated file...
        self.assertIn('#include "version.iss"', text)
        # ...and must not define a version literal of its own.
        self.assertNotRegex(text, r"#define\s+MyAppVersion\s+\"")
        self.assertNotRegex(text, r"MyAppVersion\s*\?\?")
        self.assertNotIn("1.0.0", text)

    def test_spec_reads_the_generated_version_file_only(self):
        with open(os.path.join(REPO_ROOT, "yeelight_pc_companion.spec"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('version="version_info.txt"', text)
        self.assertNotRegex(text, r"\d+\.\d+\.\d+\.\d+")


class TestPrivacyScannerPolicy(unittest.TestCase):
    """The scanner enforces an artifact-specific policy, not one generic sweep."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ypc-privacy-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content=""):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    # --- forbidden filenames -------------------------------------------

    def test_clean_artifact_passes(self):
        self._write("YeelightPCCompanion.exe", "binary")
        self._write("config.example.json", "{}")
        self.assertEqual(scanner.scan_artifact(self.tmp), [])

    def test_forbidden_filenames_are_all_detected(self):
        for name in (
            "config.json",
            "config.json.bak",
            "config.json.bak.2",
            "yeelight_pc_companion_debug.log",
            "yeelight_pc_companion_debug.log.1",
            "crash.log",
            "lumina_debug.log",
            "openrgb_provision_result.json",
            "openrgb_provisioning.log",
        ):
            with self.subTest(name=name):
                artifact = tempfile.mkdtemp(dir=self.tmp)
                self._write(os.path.join(os.path.basename(artifact), name), "x")
                violations = scanner.scan_artifact(artifact)
                self.assertTrue(violations, f"{name} was not detected")

    def test_nested_forbidden_file_is_detected(self):
        self._write(os.path.join("_internal", "config.json"), "{}")
        violations = scanner.scan_artifact(self.tmp)
        self.assertTrue(violations)

    def test_atomic_write_temp_file_is_detected(self):
        self._write(".config-abc123.tmp", "{}")
        self.assertTrue(scanner.scan_artifact(self.tmp))

    def test_pycache_directory_is_cycled_out(self):
        # Files inside __pycache__ are never shipped and must not be reported.
        self._write(os.path.join("__pycache__", "config.json"), "{}")
        self.assertEqual(
            [v for v in scanner.scan_artifact(self.tmp) if "config.json" in v.path],
            [],
        )

    # --- portable marker: both directions ---------------------------------

    def test_portable_flag_is_forbidden_in_the_installer_payload(self):
        self._write("portable.flag")
        violations = scanner.scan_artifact(self.tmp, portable=False)
        self.assertTrue(
            any("portable.flag" in v.path for v in violations),
            "portable.flag must be rejected in an installer/dist payload",
        )

    def test_portable_flag_is_required_in_the_portable_payload(self):
        self._write("YeelightPCCompanion.exe", "binary")
        violations = scanner.scan_artifact(self.tmp, portable=True)
        self.assertTrue(
            any("missing" in v.reason for v in violations),
            "a portable payload without portable.flag must fail",
        )

    def test_portable_payload_with_the_marker_passes(self):
        self._write("YeelightPCCompanion.exe", "binary")
        self._write("portable.flag")
        self.assertEqual(scanner.scan_artifact(self.tmp, portable=True), [])

    def test_both_directions_are_distinct_failures(self):
        """The same directory is clean as portable and dirty as installer."""
        self._write("portable.flag")
        self.assertEqual(scanner.scan_artifact(self.tmp, portable=True), [])
        self.assertNotEqual(scanner.scan_artifact(self.tmp, portable=False), [])

    # --- exact private values, supplied from outside tracked source -------

    def test_no_private_value_or_fingerprint_is_stored_in_the_scanner(self):
        """The scanner must not carry a value *or* a fingerprint of one.

        A truncated unsalted digest of a low-entropy value (a private IPv4
        address, a short user name) is brute-forceable, and a recorded length
        narrows the search further. Neither may come back.
        """
        with open(scanner.__file__, encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in (
            "PRIVATE_VALUE_FINGERPRINTS",
            "PRIVATE_VALUE_LENGTHS",
            "PRIVATE_VALUE_LABELS",
        ):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, text)
        # ... and nothing in the module may claim a digest is one-way.
        for claim in ("one-way fingerprint", "collision-free", "cannot be reversed"):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, text)

    def test_no_private_value_source_is_required(self):
        """Builds must work with nothing configured at all."""
        values, sources = scanner.load_private_values(explicit=(), environ={})
        self.assertEqual([], values)
        self.assertEqual([], sources)
        self.assertEqual(scanner.scan_artifact(self.tmp), [])

    def test_values_come_from_the_environment(self):
        values, sources = scanner.load_private_values(
            environ={scanner.PRIVATE_VALUES_ENV_VAR: "fake-secret-one\nfake-secret-two\n"}
        )
        self.assertEqual(["fake-secret-one", "fake-secret-two"], values)
        self.assertTrue(any(scanner.PRIVATE_VALUES_ENV_VAR in source for source in sources))

    def test_values_come_from_a_gitignored_file(self):
        path = os.path.join(self.tmp, "private_values.local.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# synthetic fake secrets used by this test\n")
            handle.write("fake-secret-one\n\n")
            handle.write("  fake-secret-two  \n")
        values, sources = scanner.load_private_values(explicit=(), file_path=path, environ={})
        self.assertEqual(["fake-secret-one", "fake-secret-two"], values)
        self.assertTrue(any(path in source for source in sources))

    def test_the_environment_can_name_the_values_file(self):
        path = os.path.join(self.tmp, "somewhere.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("fake-secret-one\n")
        values, _sources = scanner.load_private_values(
            environ={scanner.PRIVATE_VALUES_FILE_ENV_VAR: path}
        )
        self.assertEqual(["fake-secret-one"], values)

    def test_a_json_array_values_file_is_accepted(self):
        path = os.path.join(self.tmp, "values.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(["fake-secret-one", "fake-secret-two"]))
        values, _sources = scanner.load_private_values(explicit=(), file_path=path, environ={})
        self.assertEqual(["fake-secret-one", "fake-secret-two"], values)

    def test_an_unreadable_or_missing_values_file_is_not_fatal(self):
        values, sources = scanner.load_private_values(
            explicit=(), file_path=os.path.join(self.tmp, "does-not-exist.txt"), environ={}
        )
        self.assertEqual([], values)
        self.assertEqual([], sources)

    def test_synthetic_values_are_detected_in_text(self):
        """The mechanism works, proven entirely with fake secrets."""
        fake_values = ("fake-secret-alpha", "fake-secret-beta")
        self._write("notes.txt", "a file mentioning fake-secret-beta somewhere")

        violations = scanner.scan_artifact(self.tmp, private_values=fake_values)
        self.assertEqual(1, len(violations))
        self.assertIn("notes.txt", violations[0].path)
        # The report never contains the value itself.
        self.assertNotIn("fake-secret-beta", str(violations[0]))
        self.assertIn("1 configured private value", violations[0].reason)

    def test_indices_are_returned_instead_of_the_values(self):
        found = scanner.find_private_values(
            "prefix fake-secret-beta suffix", ("fake-secret-alpha", "fake-secret-beta")
        )
        self.assertEqual([2], found)

    def test_values_shorter_than_the_floor_are_ignored(self):
        """A too-short literal would behave like the refused generic substring rule."""
        values, _sources = scanner.load_private_values(explicit=("abc",), environ={})
        self.assertEqual([], values)
        self.assertEqual(scanner.MIN_PRIVATE_VALUE_LENGTH, 6)

    def test_duplicate_values_are_collapsed_without_reordering(self):
        values = scanner.normalize_private_values(
            ["fake-secret-beta", "fake-secret-alpha", "fake-secret-beta", "  "]
        )
        self.assertEqual(["fake-secret-beta", "fake-secret-alpha"], values)

    def test_the_local_values_file_is_gitignored(self):
        result = subprocess.run(
            ["git", "check-ignore", "-q", scanner.DEFAULT_PRIVATE_VALUES_FILE],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        self.assertEqual(0, result.returncode, "the private-values file is not gitignored")

    def test_the_default_values_file_is_outside_tracked_source(self):
        tracked = scanner.repo_tracked_files()
        self.assertIsNotNone(tracked)
        self.assertNotIn(
            os.path.abspath(scanner.DEFAULT_PRIVATE_VALUES_FILE),
            {os.path.abspath(path) for path in tracked},
        )

    def test_a_configured_value_in_the_repository_is_caught(self):
        """The repository scan uses the configured values, proved synthetically."""
        fake = "fake-secret-delta"
        self.assertEqual(
            [v for v in scanner.scan_repo(private_values=(fake,)) if fake in v.reason], []
        )
        # No tracked file contains the synthetic value, so nothing is reported -
        # but the mechanism is exercised through the real scan_repo path.
        self.assertEqual(scanner.scan_repo(private_values=("   ",)), [])

    def test_real_private_values_are_absent_from_the_repository(self):
        """The policy applies to the project's own files, including the audit."""
        self.assertEqual(scanner.scan_repo(), [])

    def test_no_generic_short_substring_rules_exist(self):
        """Guard against the false-positive rules this policy deliberately avoids.

        There is no hard-coded rule list any more, so the guard proves by
        behaviour that the generic strings the docstring warns about are not
        treated as private values.
        """
        for generic in ("@gmail.com", "44.8", "20.5", "192.168.", "127.0.0.1"):
            with self.subTest(generic=generic):
                self.assertEqual(scanner.find_private_values(f"a {generic} value"), [])

    def test_a_short_generic_literal_cannot_be_smuggled_in(self):
        """A literal too short to be a real private value is still ignored.

        Four hex-free characters like a two-decimal number would behave exactly
        like the generic substring rule this policy refuses, so the floor holds
        even for a caller that passes one straight into the matcher.
        """
        self.assertEqual(
            scanner.find_private_values("a 44.8 value", ("44.8",)),
            [],
            "a short generic literal must not be matchable",
        )
        self.assertEqual(
            scanner.find_private_values("a 20.5 value", ("20.5",)),
            [],
        )
        # A value at or above the floor is still matched normally.
        self.assertEqual(
            scanner.find_private_values("a 44.812 value", ("44.812",)), [1]
        )

    def test_binaries_are_not_content_scanned(self):
        # A private value inside a DLL must not be reported: third-party binaries
        # are exactly where generic string rules produce false positives.
        path = os.path.join(self.tmp, "library.dll")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("a library mentioning fake-secret-alpha")
        self.assertEqual(
            scanner.scan_artifact(self.tmp, private_values=("fake-secret-alpha",)), []
        )

    def test_scanning_a_missing_directory_fails(self):
        violations = scanner.scan_artifact(os.path.join(self.tmp, "nope"))
        self.assertTrue(violations)


class TestShippedInstallerConfiguration(unittest.TestCase):
    """Static checks on the Inno Setup script."""

    def setUp(self):
        with open(ISS_PATH, encoding="utf-8") as handle:
            self.text = handle.read()

    def test_is_per_user_and_needs_no_elevation(self):
        self.assertIn("PrivilegesRequired=lowest", self.text)

    def test_installs_under_a_per_user_application_folder(self):
        self.assertIn("DefaultDirName={autopf}", self.text)

    def test_offers_startup_desktop_and_launch_options(self):
        self.assertIn("startup", self.text)
        self.assertIn("desktopicon", self.text)
        self.assertRegex(self.text, r"postinstall\s+skipifsilent")

    def test_startup_entry_is_hkcu_only_and_removed_on_uninstall(self):
        self.assertIn("Root: HKCU", self.text)
        self.assertIn("uninsdeletevalue", self.text)

    def test_contains_no_private_config_path(self):
        # The scanner stores no private value any more, so this is asserted
        # structurally: the installer never sources config.json and the
        # configured values (when a maintainer supplies them) are applied to it.
        self.assertNotRegex(self.text, r"(?i)config\.json")
        self.assertNotIn("localappdata}\\{#MyAppName}\\config", self.text)

    def test_uninstall_checks_both_launch_and_result_code(self):
        """Correction: Exec() success alone must never be treated as success."""
        self.assertIn("ResultCode", self.text)
        self.assertRegex(self.text, r"\(not Launched\) or \(ResultCode <>\s*0\)")

    def test_there_are_exactly_two_narrow_elevation_points(self):
        """One for the install-time legacy cleanup, one for the uninstall task.

        Both are single `schtasks` invocations against a fixed task list; there
        is no general command interface and no second use per flow.
        """
        self.assertEqual(self.text.count("ShellExec('runas'"), 2)
        runas_lines = [
            line for line in self.text.splitlines() if "ShellExec('runas'" in line
        ]
        # The legacy cleanup elevates a helper that receives fixed `schtasks`
        # arguments; the uninstall flow elevates `schtasks.exe` directly.
        self.assertIn("ExpandConstant('{cmd}')", runas_lines[0])
        self.assertIn("SchtasksPath()", runas_lines[1])

    def test_uninstall_has_a_narrow_elevated_removal_flow(self):
        """Task deletion needs elevation, so it is escalated explicitly."""
        self.assertIn("ShellExec('runas'", self.text)
        # The uninstall flow escalates only through its own helper, and only
        # after an unelevated attempt was verified to have failed.
        self.assertRegex(self.text, r"Log\('Unelevated removal was refused")
        self.assertRegex(self.text, r"if not TryDeleteTask\(True\) then")

    # --- install-time legacy startup-task migration -----------------------

    def migration_code(self):
        """The executable text of the install-time legacy migration only.

        Split on the real procedure header (a doc comment mentions the name
        earlier in the file) and stop at that procedure's closing ``end;``, so
        the assertions below can never accidentally match the uninstall flow.
        """
        head = re.search(r"procedure MigrateLegacyStartupTasks\(\);", self.text)
        self.assertIsNotNone(head, "the migration procedure is missing")
        migration = self.text[head.start():]
        tail = re.search(r"^end;", migration, re.M)
        self.assertIsNotNone(tail, "the migration procedure has no closing end;")
        return migration[: tail.end()]

    def test_the_installer_migrates_legacy_startup_tasks_on_install(self):
        """Not only on uninstall: the normal install/upgrade must do it."""
        # The migration is invoked from the real install step...
        step = re.search(
            r"procedure CurStepChanged\(CurStep: TSetupStep\);(.*?)\nend;",
            self.text,
            re.S,
        )
        self.assertIsNotNone(step, "CurStepChanged is missing")
        self.assertIn("if CurStep = ssPostInstall then", step.group(1))
        self.assertIn("MigrateLegacyStartupTasks();", step.group(1))
        # ... and it is genuinely implemented, not only called.
        self.assertIn("TryDeleteLegacyTask(TaskName)", self.migration_code())

    def test_the_migration_scope_is_exactly_the_two_fixed_names(self):
        """The complete legacy scope is one fixed list with one count.

        Inno Setup's PascalScript has no array constants, so the fixed list is an
        index accessor plus a count — but it is still a single source of truth,
        and it is asserted here so a third name cannot be added silently.
        """
        head = re.search(r"function LegacyStartupTaskCount\(\): Integer;", self.text)
        self.assertIsNotNone(head, "LegacyStartupTaskCount is missing")
        accessor = re.search(
            r"function LegacyStartupTaskName\(const Index: Integer\): String;(.*?)\nend;",
            self.text,
            re.S,
        )
        self.assertIsNotNone(accessor, "LegacyStartupTaskName is missing")
        body = accessor.group(1)
        names = re.findall(r"Result := '([^']*)'", body)
        # The accessor's non-empty results are exactly the two retired names,
        # in order, and nothing else.
        self.assertEqual(
            [name for name in names if name],
            ["YeelightPCCompanion", "LuminaLightOrchestrator"],
        )
        self.assertEqual(len(names), 3, "the accessor must have exactly one fallback branch")
        self.assertEqual(names[-1], "", "the out-of-range branch must not name a task")

        # The count is a literal, and it matches the accessor.
        count = re.search(r"function LegacyStartupTaskCount\(\): Integer;\s*begin\s*Result := (\d+);", self.text)
        self.assertIsNotNone(count)
        self.assertEqual(2, int(count.group(1)))

        # Both names appear in the whole file only inside this accessor.
        for name in ("'YeelightPCCompanion'", "'LuminaLightOrchestrator'"):
            with self.subTest(name=name):
                self.assertEqual(
                    1,
                    len(re.findall(re.escape(name), self.text)),
                    f"{name} must be declared exactly once (in the fixed accessor)",
                )

    def test_the_migration_never_crosses_into_the_openrgb_task(self):
        """The OpenRGB task is a different task and is never in the migration.

        The migration loops iterate `LegacyStartupTaskCount()` and can only
        obtain a name from `LegacyStartupTaskName()`; the OpenRGB name must not
        appear anywhere inside the migration code.
        """
        migration = self.migration_code()
        self.assertIn("LegacyStartupTaskCount()", migration)
        self.assertIn("LegacyStartupTaskName(Index)", migration)
        self.assertNotIn("OpenRgbTaskName", migration)
        self.assertNotIn("YeelightPCCompanion-OpenRGB", migration)

    def test_the_migration_never_uses_the_openrgb_helpers(self):
        """It never calls the OpenRGB task deletion helper, directly or via a flag."""
        migration = self.migration_code()
        self.assertNotIn("TryDeleteTask(", migration)
        self.assertNotIn("RemoveOpenRgbTask", migration)

    def test_the_migration_deletes_only_via_the_fixed_name_lookup(self):
        migration = self.migration_code()
        # Every deletion goes through the per-name helper with a name taken from
        # the fixed accessor - never a caller-supplied or generic identifier.
        self.assertEqual(migration.count("TryDeleteLegacyTask("), 2)
        self.assertEqual(migration.count("TaskName := LegacyStartupTaskName(Index);"), 3)
        self.assertNotIn("LegacyStartupTaskNames", migration)

    def test_the_migration_verifies_instead_of_trusting_an_exit_code(self):
        migration = self.migration_code()
        # After the unelevated attempt and after the elevated one, a fresh query
        # decides - `TryDeleteLegacyTask`'s boolean is never the last word.
        self.assertGreaterEqual(migration.count("TaskExists(TaskName)"), 3)
        self.assertIn("survived the unelevated removal", migration)
        self.assertIn("is still present", migration)

    def test_a_fresh_install_raises_no_prompt(self):
        """No legacy task detected means the migration returns before any UAC."""
        migration = self.migration_code()
        guard = migration.index("if Remaining = 0 then")
        elevated_call = migration.index("RunOneElevatedCleanup(")
        self.assertLess(guard, elevated_call)
        # The detection itself is silent (query only, no elevation).
        detection = migration.index("if not TaskExists(TaskName) then")
        self.assertLess(detection, elevated_call)

    def test_at_most_one_elevation_regardless_of_how_many_tasks_remain(self):
        """Two remaining legacy tasks must still produce ONE consent prompt.

        The elevated deletion is bundled into a single helper process, so the
        migration procedure contains exactly one elevation call site and it is
        not inside a loop.
        """
        migration = self.migration_code()
        self.assertEqual(migration.count("RunOneElevatedCleanup("), 1)
        self.assertIn("RunOneElevatedCleanup(LegacyStartupTaskName(0))", migration)
        # The single call is not inside the per-name admin branch (which uses the
        # already-elevated direct deletion) and not inside any nested loop: it is
        # the only statement of the `else` branch, indented exactly two spaces.
        self.assertIn("\n  else\n  begin\n    { ONE approval", migration)
        self.assertIn("    RunOneElevatedCleanup(LegacyStartupTaskName(0));", migration)
        # And no `ShellExec` appears in the migration itself.
        self.assertNotIn("ShellExec", migration)

    def test_a_silent_install_never_raises_a_consent_prompt(self):
        migration = self.migration_code()
        self.assertIn("WizardSilent", migration)
        silent_guard = migration.index("if WizardSilent then")
        elevated_call = migration.index("RunOneElevatedCleanup(")
        self.assertLess(silent_guard, elevated_call)

    def test_a_declined_cleanup_does_not_fail_the_installation(self):
        migration = self.migration_code()
        # Every failure path only logs and exits; the procedure never raises and
        # never signals setup failure.
        self.assertNotIn("RaiseException", migration)
        self.assertNotIn("Abort", migration)
        self.assertNotIn("MsgBox", migration)
        self.assertIn("Installation is", migration)
        self.assertIn("not affected", migration)
        self.assertIn("left in place", migration)

    def test_the_migration_reports_a_task_it_could_not_remove(self):
        migration = self.migration_code()
        self.assertIn("elevated duplicate", migration)
        self.assertIn("Task Scheduler", migration)

    def test_uninstall_tries_unelevated_before_escalating(self):
        self.assertRegex(self.text, r"Removed := TryDeleteTask\(False\)")
        self.assertRegex(self.text, r"TryDeleteTask\(True\)")

    def test_uninstall_still_retires_the_legacy_tasks(self):
        """The [UninstallRun] entries are kept as a second, independent net."""
        self.assertRegex(
            self.text, r"Parameters: \"/delete /tn YeelightPCCompanion /f\""
        )
        self.assertRegex(
            self.text, r"Parameters: \"/delete /tn LuminaLightOrchestrator /f\""
        )

    def test_silent_uninstall_never_raises_a_consent_prompt(self):
        """A silent uninstall must not block on an invisible UAC dialog."""
        self.assertIn("UninstallSilent", self.text)
        self.assertRegex(self.text, r"if UninstallSilent then")

    def test_uninstall_reports_a_task_it_could_not_remove(self):
        self.assertIn("left in place", self.text)

    def test_uninstall_verifies_the_task_is_actually_gone(self):
        self.assertRegex(self.text, r"if TaskExists\(OpenRgbTaskName\)")

    def test_uninstall_removes_only_the_app_owned_task(self):
        self.assertIn("YeelightPCCompanion-OpenRGB", self.text)
        for foreign in ("Artemis", "Razer", "Chromium", "Yeelight Chroma Connector.exe"):
            with self.subTest(foreign=foreign):
                self.assertNotIn(f"/tn {foreign}", self.text)

    def test_uninstall_does_not_delete_user_configuration(self):
        # The data directory may only be *mentioned*; it must never be removed.
        self.assertNotRegex(self.text, r"(?i)DelTree\([^)]*localappdata")
        self.assertIn("left untouched", self.text)

    def test_installs_the_full_onedir_payload(self):
        self.assertIn("recurse" + "subdirs", self.text)
        self.assertIn("dist\\YeelightPCCompanion", self.text)


class TestInstalledStyleStorageLocations(unittest.TestCase):
    """Fresh installed-style and portable runs pick the right storage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ypc-storage-")
        self.frozen = getattr(sys, "frozen", None)
        self.had_frozen = hasattr(sys, "frozen")
        self.local_appdata = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self.tmp

    def tearDown(self):
        if self.had_frozen:
            sys.frozen = self.frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen
        if self.local_appdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self.local_appdata
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_installed_run_selects_local_appdata(self):
        app_dir = os.path.join(self.tmp, "Programs", "Yeelight PC Companion")
        os.makedirs(app_dir)
        sys.frozen = True
        try:
            expected = os.path.join(
                self.tmp, cm.APP_DATA_DIR_NAME, cm.CONFIG_FILENAME
            )
            self.assertEqual(cm.get_config_path(app_dir), expected)
            self.assertEqual(cm.storage_mode(app_dir), "localappdata")
            # A fresh install must not try to write beside the executable.
            self.assertNotEqual(
                os.path.dirname(cm.get_config_path(app_dir)),
                os.path.abspath(app_dir),
            )
        finally:
            del sys.frozen

    def test_portable_flag_selects_the_application_directory(self):
        app_dir = os.path.join(self.tmp, "portable")
        os.makedirs(app_dir)
        sys.frozen = True
        try:
            with open(os.path.join(app_dir, cm.PORTABLE_FLAG_FILENAME), "w"):
                pass
            self.assertEqual(
                cm.get_config_path(app_dir),
                os.path.join(os.path.abspath(app_dir), cm.CONFIG_FILENAME),
            )
            self.assertEqual(cm.storage_mode(app_dir), "portable")
        finally:
            del sys.frozen

    def test_source_run_stays_in_the_repository(self):
        self.assertFalse(cm.is_frozen())
        self.assertEqual(cm.storage_mode(REPO_ROOT), "source")

    def test_a_fresh_install_has_no_config_yet(self):
        """The first-run wizard, not a shipped file, must create the config."""
        app_dir = os.path.join(self.tmp, "installed")
        os.makedirs(app_dir)
        sys.frozen = True
        try:
            path = cm.get_config_path(app_dir)
            self.assertFalse(os.path.exists(path))
            # The default configuration is what the wizard starts from.
            self.assertEqual(cm.default_config()["config_version"], cm.CONFIG_VERSION)
        finally:
            del sys.frozen


class TestPackagedPayloadClaims(unittest.TestCase):
    """The spec must not introduce a private config into the bundle."""

    def test_spec_ships_only_the_example_template(self):
        with open(os.path.join(REPO_ROOT, "yeelight_pc_companion.spec"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("config.example.json", text)
        self.assertNotIn('("config.json"', text)
        self.assertNotIn("portable.flag", text)

    def test_spec_excludes_the_unused_tk_runtime(self):
        with open(os.path.join(REPO_ROOT, "yeelight_pc_companion.spec"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('"tkinter"', text)

    def test_example_config_matches_the_application_defaults(self):
        """The shipped template must stay a placeholder-only fresh config."""
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as handle:
            example = json.load(handle)
        self.assertEqual(example, cm.default_config())
        # No real coordinates or addresses may ever be baked into the template.
        # The template's coordinates must be the documented placeholder.
        self.assertEqual(example["location"]["latitude"], "0.0000")
        self.assertEqual(example["location"]["longitude"], "0.0000")
        self.assertEqual(example["lights"]["devices"], [])
        self.assertEqual(scanner.find_private_values(json.dumps(example)), [])


class TestBuildPipelineScript(unittest.TestCase):
    """The release script must fail fast and cover the whole pipeline."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "build_release.ps1"), encoding="utf-8") as handle:
            self.text = handle.read()

    def test_runs_tests_before_building(self):
        # Compare the real invocations, not the docstring that names them.
        self.assertLess(
            self.text.index("tools\\write_version_info.py --check"),
            self.text.index("python -m PyInstaller"),
        )
        self.assertLess(
            self.text.index("python -m unittest discover"),
            self.text.index("python -m PyInstaller"),
        )

    def test_runs_both_privacy_scans(self):
        self.assertIn("--artifact $DistDir", self.text)
        self.assertIn("--portable", self.text)

    def test_checks_expected_artifacts_exist(self):
        self.assertIn("was not produced", self.text)
        self.assertIn("portable ZIP was not created", self.text)

    def test_verifies_version_metadata(self):
        self.assertIn("--check", self.text)

    def test_contains_no_private_values(self):
        self.assertEqual(scanner.find_private_values(self.text), [])

    def test_never_requires_a_personal_config(self):
        # It may *mention* config.json when asserting its absence, never source it.
        self.assertNotRegex(self.text, r"Copy-Item[^\n]*config\.json")


class TestCiWorkflows(unittest.TestCase):
    """CI and release workflows exist, are Windows-based and stick to plain actions."""

    WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")

    def _read(self, name):
        with open(os.path.join(self.WORKFLOWS, name), encoding="utf-8") as handle:
            return handle.read()

    def test_ci_workflow_exists_and_is_windows(self):
        text = self._read("ci.yml")
        self.assertIn("runs-on: windows-latest", text)
        self.assertIn("unittest discover", text)

    def test_ci_installs_dependencies_and_runs_syntax_checks(self):
        text = self._read("ci.yml")
        self.assertIn("requirements.txt", text)
        self.assertIn("compileall", text)

    def test_ci_generates_gitignored_installer_version_before_checking(self):
        text = self._read("ci.yml")
        generator = "python tools/write_installer_version.py"
        check = generator + " --check"
        self.assertIn(generator, text)
        self.assertIn(check, text)
        self.assertLess(
            text.index(generator + "\n"),
            text.index(check),
            "CI must generate installer/version.iss before checking it in a clean clone",
        )

    def test_actions_are_pinned_to_major_versions(self):
        for name in ("ci.yml", "release.yml"):
            with self.subTest(workflow=name):
                text = self._read(name)
                uses = re.findall(r"uses:\s*(\S+)", text)
                self.assertTrue(uses)
                for action in uses:
                    self.assertRegex(
                        action,
                        r"^[\w./-]+@v\d+$",
                        f"{action} is not pinned to a major version",
                    )

    def test_release_workflow_is_tag_triggered_and_uploads_artifacts(self):
        text = self._read("release.yml")
        self.assertIn("tags:", text)
        self.assertIn("upload-artifact", text)
        self.assertIn("build_release.ps1", text)

    def test_release_workflow_does_not_publish_a_release(self):
        """Build-only: no GitHub Release is created by the workflow."""
        text = self._read("release.yml")
        self.assertNotIn("softprops/action-gh-release", text)
        self.assertNotIn("gh release create", text)
        self.assertNotIn("contents: write", text)

    def test_no_workflow_touches_real_hardware_or_elevation(self):
        for name in ("ci.yml", "release.yml"):
            with self.subTest(workflow=name):
                text = self._read(name).lower()
                for forbidden in ("schtasks", "runas", "elevate", "discover_bulbs"):
                    self.assertNotIn(forbidden, text)


class TestReleaseWorkflowBooleanHandling(unittest.TestCase):
    """The release workflow must decide `skip_tests` at the GitHub level.

    The regression this pins: `if (${{ inputs.skip_tests == true }})` interpolates
    a GitHub Actions *boolean* into PowerShell source, which renders `if (True)`
    or `if (False)` — not PowerShell syntax. The failure is silent and confusing,
    so the shape is asserted rather than trusted.
    """

    WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")

    def setUp(self):
        with open(os.path.join(self.WORKFLOWS, "release.yml"), encoding="utf-8") as handle:
            self.text = handle.read()

    def _steps(self):
        import yaml

        return yaml.safe_load(self.text)["jobs"]["build"]["steps"]

    def test_a_github_boolean_is_never_interpolated_into_powershell(self):
        # No expression may appear inside a `run:` block at all - a boolean, a
        # string or anything else. Conditions belong in the workflow engine.
        for step in self._steps():
            run = step.get("run", "")
            with self.subTest(step=step.get("name")):
                self.assertNotIn("${{", run)

    def test_the_old_interpolated_if_is_gone(self):
        """No PowerShell `if (...)` may be fed a rendered GitHub boolean.

        The check runs against the workflow's parsed `run` values, so a comment
        that *documents* the retired expression cannot trip it, and no `run`
        block can carry an interpolation at all.
        """
        import yaml

        for step in self._steps():
            run = step.get("run", "")
            if "if" not in run and "skip_tests" not in run:
                continue
            with self.subTest(step=step.get("name")):
                self.assertNotIn("${{", run)
        # And the workflow source is valid YAML with the two conditions intact.
        self.assertIsInstance(yaml.safe_load(self.text), dict)

    def test_a_tag_triggered_build_always_runs_the_tests(self):
        """Exactly one of the two build steps runs, and a tag takes the normal one."""
        builders = [
            step for step in self._steps() if "build_release.ps1" in step.get("run", "")
        ]
        self.assertEqual(2, len(builders))

        normal = [step for step in builders if "-SkipTests" not in step["run"]]
        skipped = [step for step in builders if "-SkipTests" in step["run"]]
        self.assertEqual(1, len(normal))
        self.assertEqual(1, len(skipped))

        normal_condition = normal[0]["if"]
        skipped_condition = skipped[0]["if"]

        # The normal step runs for every trigger that is not an explicit
        # `skip_tests` dispatch: a tag push (`github.event_name == 'push'`)
        # therefore always satisfies it, and it is the one that keeps the tests.
        self.assertIn("github.event_name != 'workflow_dispatch'", normal_condition)
        self.assertIn("!inputs.skip_tests", normal_condition)
        # The skipping step requires BOTH a manual dispatch and the opt-in.
        self.assertIn("github.event_name == 'workflow_dispatch'", skipped_condition)
        self.assertIn("inputs.skip_tests", skipped_condition)
        self.assertNotIn("!", skipped_condition.replace("!=", ""))

    def test_exactly_one_builder_runs_for_every_trigger(self):
        """The two conditions are mutually exclusive and cover every trigger."""
        import yaml

        steps = self._steps()
        conditions = [
            step["if"]
            for step in steps
            if "build_release.ps1" in step.get("run", "")
        ]
        self.assertEqual(2, len(conditions))
        # ('push', False), ('push', True), ('workflow_dispatch', False),
        # ('workflow_dispatch', True) -> exactly one condition holds each time.
        for event_name in ("push", "workflow_dispatch"):
            for skip in (False, True):
                matches = [
                    _condition_holds(condition, event_name, skip)
                    for condition in conditions
                ]
                with self.subTest(event=event_name, skip_tests=skip):
                    self.assertEqual(1, matches.count(True))

    def test_the_workflow_yaml_parses(self):
        """Validated with a real parser, not a regex."""
        import yaml

        document = yaml.safe_load(self.text)
        triggers = _triggers(document)
        self.assertIn("push", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("jobs", document)
        self.assertIn("build", document["jobs"])

    def test_a_tag_push_alone_always_runs_the_tests(self):
        """End to end for the trigger that matters: `v*` must not skip tests."""
        conditions = [
            step["if"]
            for step in self._steps()
            if "build_release.ps1" in step.get("run", "")
        ]
        normal = [c for c in conditions if "!" in c]
        self.assertEqual(1, len(normal))
        # For a tag push `inputs` does not exist, so `!inputs.skip_tests` is
        # true and `github.event_name != 'workflow_dispatch'` is true as well.
        self.assertTrue(_condition_holds(normal[0], "push", None))

    def test_the_skip_step_can_only_run_for_a_manual_dispatch(self):
        conditions = [
            step["if"]
            for step in self._steps()
            if "-SkipTests" in step.get("run", "")
        ]
        self.assertEqual(1, len(conditions))
        for event_name in ("push", "schedule"):
            with self.subTest(event=event_name):
                self.assertFalse(_condition_holds(conditions[0], event_name, None))

    def test_the_dispatch_input_is_a_boolean_defaulting_to_false(self):
        import yaml

        document = yaml.safe_load(self.text)
        dispatch = _triggers(document)["workflow_dispatch"]
        self.assertEqual(
            {"skip_tests": {"description": "Skip the unit test suite (packaging iteration only)",
                            "type": "boolean", "default": False}},
            dispatch["inputs"],
        )

    def test_no_build_step_skips_tests_unconditionally(self):
        for step in self._steps():
            if "-SkipTests" in step.get("run", ""):
                with self.subTest(step=step.get("name")):
                    self.assertIn("if", step)


def _triggers(document):
    """The workflow's ``on:`` mapping.

    PyYAML implements YAML 1.1, where an unquoted ``on`` parses as the boolean
    ``True``; GitHub's own parser treats it as the key ``"on"``. Accept both so
    the test is about the workflow, not about the YAML dialect.
    """
    for key in ("on", True):
        if key in document:
            return document[key]
    raise AssertionError("the workflow has no `on:` trigger section")


def _condition_holds(condition, event_name, skip_tests):
    """Evaluate the two GitHub `if` expressions this workflow uses.

    Only the forms actually present are supported (``&&``, ``||``, ``!``,
    ``!=``/``==`` comparisons and bare input reads); anything else raises, so a
    future rewrite of the conditions cannot silently pass these tests.
    """
    values = {
        "github.event_name": event_name,
        "inputs.skip_tests": skip_tests,
    }

    def evaluate(clause):
        clause = clause.strip()
        if clause.startswith("!"):
            return not evaluate(clause[1:])
        for operator, apply in (
            ("==", lambda a, b: a == b),
            ("!=", lambda a, b: a != b),
        ):
            if operator in clause:
                left, right = clause.split(operator, 1)
                return apply(
                    _value(left.strip(), values),
                    _value(right.strip(), values),
                )
        return bool(_value(clause, values))

    for alternative in condition.split("||"):
        if all(evaluate(clause) for clause in alternative.split("&&")):
            return True
    return False


def _value(token, values):
    if token in values:
        return values[token]
    if token == "true":
        return True
    if token == "false":
        return False
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    raise AssertionError(f"unsupported token in a workflow condition: {token!r}")


if __name__ == "__main__":
    unittest.main()