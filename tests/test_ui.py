"""UI regression tests for the Stage 5 redesigned interface.

Presentation-only coverage: the suite constructs the real widgets (offscreen)
against an isolated temporary configuration and never

* touches the real user profile, Task Scheduler, or `config.json`,
* runs a real network discovery,
* runs a real sleep/wake or restore sequence,
* or requests elevation.

Where a test must observe a side effect (validation, the atomic write, the
import path, the process-status check) it patches exactly that boundary, so the
assertions describe the wiring the application really uses.
"""

import json
import logging
import logging.handlers
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# The Qt platform must be chosen before QApplication exists.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - environment dependent
    from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import QPalette
    from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
except Exception as exc:  # pragma: no cover - environment dependent
    QObject = None
    Qt = None
    QTimer = None
    pyqtSignal = None
    QPalette = None
    QApplication = None
    QDialog = None
    QMessageBox = None
    QT_IMPORT_ERROR = exc
else:
    QT_IMPORT_ERROR = None

try:  # pragma: no cover - environment dependent
    import config_manager as cm
    import windows_tasks as wt
    import yeelight_devices as yd
    import ui_components as components
    import ui_theme as theme
    import yeelight_device_ui as device_ui
    import first_run_wizard as wizard
    import yeelight_pc_companion as app
except Exception as exc:  # pragma: no cover - environment dependent
    cm = wt = yd = components = theme = device_ui = wizard = app = None
    APP_IMPORT_ERROR = exc
else:
    APP_IMPORT_ERROR = None

SKIP_REASON = (
    "the UI tests need an importable PyQt6 and application stack: %s"
    % (QT_IMPORT_ERROR or APP_IMPORT_ERROR)
)
SKIP = QT_IMPORT_ERROR is not None or APP_IMPORT_ERROR is not None

_QT_APPLICATION = None


def qt_application():
    """A single offscreen QApplication for the whole module."""
    global _QT_APPLICATION
    if _QT_APPLICATION is None:
        _QT_APPLICATION = QApplication.instance() or QApplication([])
    return _QT_APPLICATION


def find_children(widget, predicate, found=None):
    """Depth-first search over a widget tree (used instead of pixel tests)."""
    found = [] if found is None else found
    for child in widget.children():
        if predicate(child):
            found.append(child)
        find_children(child, predicate, found)
    return found


def texts(widget):
    return [child.text() for child in find_children(widget, lambda w: hasattr(w, "text"))]


def elevation_status(state, label, detail="", action_label=None):
    """A real `OpenRgbElevationStatus` (never queried from the machine)."""
    return wt.OpenRgbElevationStatus(state, label, detail, action_label)


class RecordingRestoreThread(QObject):
    """Stand-in for `RestoreEngineThread` that only records its construction."""

    progress_update = pyqtSignal(str)
    finished_sequence = pyqtSignal()

    def __init__(self, config_manager, created):
        super().__init__()
        self.config_manager = config_manager
        created.append(self)

    def start(self):
        pass

    def isRunning(self):
        return False


class UiTestCase(unittest.TestCase):
    """Base class: an isolated configuration and clean widget teardown."""

    @classmethod
    def setUpClass(cls):
        qt_application()

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="yeelight-ui-test-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.config_path = os.path.join(self.directory, "config.json")
        self.manager = cm.ConfigManager(self.config_path)
        self.manager.save(self.default_config())
        self.elevation_queries = []

        # Read-only inspection only: the elevation status is never taken from
        # the real Task Scheduler in this suite.
        self._patch(
            app, "openrgb_elevation_status", self.recorded_elevation_status
        )
        self._patch(app, "get_running_processes_win32", lambda: set())
        # Nothing in this module may broadcast on the real LAN. A test that
        # wants to prove the Discover wiring replaces this with its own recorder.
        self._patch(
            device_ui,
            "run_discovery",
            mock.Mock(side_effect=AssertionError("a UI test must never search the network")),
        )
        self._patch(
            device_ui,
            "discover_devices",
            mock.Mock(side_effect=AssertionError("a UI test must never search the network")),
        )

    # --- helpers --------------------------------------------------------
    def _patch(self, target, attribute, new):
        patcher = mock.patch.object(target, attribute, new)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def default_config(self):
        config = self.manager.default_config()
        config["location"].update(
            {
                "latitude": "11.1111",
                "longitude": "-22.2222",
                "elevation": "123",
                "light_buffer_hours": "2",
            }
        )
        config["lights"]["devices"] = [
            {
                "id": "yeelight:0x0000000012345678",
                "name": "Desk Lamp",
                "ip": "192.168.1.50",
                "enabled": True,
                "model": "color4",
            },
            {
                "id": "manual:5b0a1f6c-1111-2222-3333-444455556666",
                "name": "Lightstrip",
                "ip": "192.168.1.51",
                "enabled": False,
            },
        ]
        config["paths"].update(
            {
                "openrgb": r"C:\Program Files\OpenRGB\OpenRGB.exe",
                "yeelight_connector": r"C:\Program Files\Yeelight\Connector.exe",
                "razer_synapse": "",
                "artemis": "",
            }
        )
        config["integrations"]["openrgb"]["enabled"] = True
        config["integrations"]["yeelight_connector"]["enabled"] = True
        config["integrations"]["razer_synapse"]["enabled"] = False
        config["integrations"]["artemis"]["enabled"] = False
        return config

    def recorded_elevation_status(self, path, integration_is_enabled=True):
        self.elevation_queries.append((path, integration_is_enabled))
        if not integration_is_enabled:
            return elevation_status(wt.STATUS_DISABLED, "Not used", "disabled")
        return elevation_status(
            wt.STATUS_READY, "Ready", "The seamless elevated launch is set up."
        )

    def reload_config(self):
        return self.manager.load()

    def save_config(self, config):
        self.manager.save(config)
        return config

    def window(self, config=None):
        """Construct the real main window against the isolated configuration."""
        if config is not None:
            self.save_config(config)
        window = app.YeelightPCCompanionWindow(
            self.config_path,
            auto_restore=False,
            automation_paused=True,
            config_manager=self.manager,
        )
        self.addCleanup(self.dispose, window)
        return window

    def dispose(self, window):
        """Stop the real timers/threads so a test never leaves work behind."""
        for name in ("status_timer", "_watchdog_timer"):
            timer = getattr(window, name, None)
            if timer is not None:
                timer.stop()
        thread = getattr(window, "solar_thread", None)
        if thread is not None:
            thread.stop()
        tray = getattr(window, "tray_icon", None)
        if tray is not None:
            tray.hide()
        handler = getattr(window, "log_handler", None)
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            window.log_handler = None
        window.setParent(None)
        window.deleteLater()
        QApplication.processEvents()

    def pump(self):
        QApplication.processEvents()


class TestWindowConstruction(UiTestCase):
    """The window builds, and building it does nothing dangerous."""

    def test_the_main_window_constructs(self):
        window = self.window()
        self.assertTrue(window.windowTitle())
        self.assertEqual(len(app.NAV_PAGES), window.pages.count())
        self.assertIsNotNone(window.centralWidget())

    def test_the_sidebar_contains_every_intended_page(self):
        window = self.window()
        self.assertEqual(list(app.NAV_PAGES), list(window.nav_buttons))
        self.assertEqual(
            ["Overview", "Devices", "Integrations", "Automation", "Logs"],
            [button.text() for button in window.nav_buttons.values()],
        )
        self.assertEqual(len(app.NAV_PAGES), window.pages.count())

    def test_navigation_selects_the_matching_content(self):
        window = self.window()
        for index, name in enumerate(app.NAV_PAGES):
            self.assertTrue(window.select_page(name))
            self.assertEqual(index, window.pages.currentIndex())
            self.assertEqual(name, window.pages.currentWidget().objectName() or name)
            self.assertEqual(name, window.lbl_page_title.text())
            self.assertTrue(window.nav_buttons[name].isChecked())

        self.assertFalse(window.select_page("Not a page"))
        self.assertNotEqual("Not a page", window.lbl_page_title.text())

    def test_clicking_a_navigation_button_selects_its_page(self):
        window = self.window()
        window.nav_buttons["Automation"].click()
        self.assertEqual(
            window.page_index["Automation"], window.pages.currentIndex()
        )
        self.assertEqual("Automation", window.lbl_page_title.text())

    def test_the_action_bar_only_appears_where_there_is_something_to_save(self):
        window = self.window()
        for name in app.NAV_PAGES:
            window.select_page(name)
            self.assertEqual(
                name not in app.PAGES_WITH_ACTION_BAR, window.action_bar.isHidden()
            )

    def test_the_window_is_usable_at_its_minimum_size(self):
        window = self.window()
        window.resize(window.minimumSize())
        self.pump()
        for name in app.NAV_PAGES:
            window.select_page(name)
            self.pump()
        self.assertGreaterEqual(window.minimumWidth(), 780)
        self.assertLessEqual(window.minimumWidth(), 900)

    def test_construction_runs_no_discovery(self):
        started = []

        def forbidden_start(self):
            started.append(self)
            raise AssertionError("discovery must never start by itself")

        self._patch(device_ui.DeviceDiscoveryThread, "start", forbidden_start)
        self._patch(device_ui, "discover_devices", lambda *a, **k: self.fail("discovery ran"))
        window = self.window()
        for name in app.NAV_PAGES:
            window.select_page(name)
        self.assertEqual([], started)
        self.assertEqual([], device_ui.LIVE_DISCOVERY_WORKERS)

    def test_construction_never_provisions_or_requests_elevation(self):
        self._patch(app, "apply_openrgb_task_action", mock.Mock(side_effect=AssertionError))
        self._patch(app, "run_provisioning_cli", mock.Mock(side_effect=AssertionError))
        self._patch(app, "run_openrgb_task", mock.Mock(side_effect=AssertionError))
        window = self.window()
        for name in app.NAV_PAGES:
            window.select_page(name)
        # The state was inspected (read-only), never changed.
        self.assertTrue(self.elevation_queries)

    def test_construction_never_runs_restore_or_suspend_work(self):
        self._patch(app, "RestoreEngineThread", mock.Mock(side_effect=AssertionError))
        self._patch(app, "terminate_processes_win32", mock.Mock(side_effect=AssertionError))
        self._patch(app, "fire_and_forget_off_devices", mock.Mock(side_effect=AssertionError))
        window = self.window()
        self.assertIsNone(window.restore_thread)
        self.assertTrue(window._automation_paused)


class TestOverviewPage(UiTestCase):
    """The Overview answers: environment, services, manual actions."""

    def test_overview_exposes_solar_device_and_service_information(self):
        window = self.window()
        self.assertIn("11.1111", window.lbl_lat.text())
        self.assertIn("-22.2222", window.lbl_lon.text())
        self.assertIn("1 enabled", window.lbl_device_summary.text())
        self.assertIn("2 configured", window.lbl_device_summary.text())
        self.assertEqual("Calculating...", window.lbl_sun_state.text())
        self.assertEqual(4, len(window.service_badges))
        self.assertEqual(
            {
                "OpenRGB.exe",
                "Yeelight Chroma Connector.exe",
                "Razer Synapse 3.exe",
                "Artemis.UI.Windows.exe",
            },
            set(window.service_badges),
        )

    def test_service_badges_report_running_and_stopped_in_text(self):
        window = self.window()
        self._patch(
            app,
            "get_running_processes_win32",
            lambda: {"openrgb.exe", "artemis.ui.windows.exe"},
        )
        window.check_system_statuses()
        self.assertEqual("Running", window.service_badges["OpenRGB.exe"].text())
        self.assertEqual("Running", window.service_badges["Artemis.UI.Windows.exe"].text())
        self.assertEqual(
            "Stopped", window.service_badges["Yeelight Chroma Connector.exe"].text()
        )
        self.assertEqual("Stopped", window.service_badges["Razer Synapse 3.exe"].text())
        # The state is carried by the text, and the tone only reinforces it.
        self.assertNotEqual(
            window.service_badges["OpenRGB.exe"].styleSheet(),
            window.service_badges["Razer Synapse 3.exe"].styleSheet(),
        )

    def test_a_failing_process_listing_leaves_the_badges_alone(self):
        window = self.window()
        window.check_system_statuses()
        before = window.service_badges["OpenRGB.exe"].text()
        self._patch(app, "get_running_processes_win32", mock.Mock(side_effect=OSError))
        window.check_system_statuses()
        self.assertEqual("Stopped", before)
        self.assertEqual("Stopped", window.service_badges["OpenRGB.exe"].text())

    def test_solar_updates_reach_the_overview(self):
        window = self.window()
        window.on_solar_update(True, "Nighttime")
        self.assertEqual("Nighttime", window.lbl_sun_state.text())
        self.assertIn("Run Yeelight Connector", window.lbl_action_req.text())

        window.on_solar_update(False, "Daytime")
        self.assertEqual("Daytime", window.lbl_sun_state.text())
        self.assertIn("Kill Connector & Turn Off", window.lbl_action_req.text())

    def test_the_device_count_refreshes_from_the_configuration(self):
        window = self.window()
        config = window.config
        config["lights"]["devices"][1]["enabled"] = True
        self.save_config(config)
        window.load_config()
        window._refresh_dashboard_labels()
        self.assertIn("2 enabled", window.lbl_device_summary.text())

    def test_the_manual_actions_are_wired_to_the_real_handlers(self):
        created = []
        self._patch(
            app, "RestoreEngineThread", lambda manager: RecordingRestoreThread(manager, created)
        )
        stopped_processes = []
        off_batches = []
        self._patch(app, "terminate_processes_win32", lambda *names: stopped_processes.append(names) or 0)
        self._patch(app, "fire_and_forget_off_devices", lambda ips, **kwargs: off_batches.append(list(ips)))
        window = self.window()

        window.btn_force_sync.click()
        self.assertEqual(1, len(created), "Force System Sync must run the wake sequence")
        self.assertEqual("Restoring...", window.lbl_system_status.text())
        self.assertEqual([], off_batches)

        window.btn_sleep_actions.click()
        self.assertEqual(1, len(off_batches), "the sleep button must run the sleep steps")
        self.assertIn("192.168.1.50", off_batches[0])
        self.assertNotIn("192.168.1.51", off_batches[0], "only enabled devices are addressed")
        self.assertTrue(stopped_processes, "the sleep steps must close the light-control applications")
        self.assertEqual("Suspending", window.lbl_system_status.text())

        # The sleep button must say what it does: this app's steps, not Windows'.
        tooltip = window.btn_sleep_actions.toolTip().lower()
        self.assertIn("sleep", tooltip)
        self.assertIn("does not put windows to sleep", tooltip)


class TestDevicesPage(UiTestCase):
    """Device management keeps the existing model and wiring."""

    def test_the_devices_page_uses_the_existing_device_model(self):
        window = self.window()
        self.assertEqual(window.config["lights"]["devices"], window.device_list.devices())
        self.assertIn("1 enabled", window.device_list.lbl_summary.text())
        row_texts = " ".join(texts(window.device_list))
        self.assertIn("Desk Lamp", row_texts)
        self.assertIn("192.168.1.50", row_texts)
        self.assertIn("model color4", row_texts)

    def test_no_reachability_state_is_invented(self):
        window = self.window()
        row_texts = " ".join(texts(window.device_list)).lower()
        for word in ("online", "offline", "unreachable", "last seen"):
            self.assertNotIn(word, row_texts)

    def test_discover_and_add_manually_remain_wired(self):
        window = self.window()
        searches = []
        self._patch(
            device_ui,
            "run_discovery",
            lambda parent=None, timeout=None: searches.append(parent) or yd.DiscoveryReport([], ""),
        )
        self._patch(device_ui.QMessageBox, "information", mock.Mock())
        editors = []

        class RecordingEditor(device_ui.DeviceEditorDialog):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                editors.append(self)

            def exec(self):
                return QDialog.DialogCode.Rejected

        self._patch(device_ui, "DeviceEditorDialog", RecordingEditor)

        window.btn_discover.click()
        window.btn_add_device.click()

        self.assertEqual(1, len(searches), "Discover must run the discovery flow")
        self.assertEqual(1, len(editors), "Add Manually must open the device editor")
        self.assertEqual(2, len(window.device_list.devices()), "a cancelled flow changes nothing")

    def test_toggling_a_device_updates_the_working_list(self):
        window = self.window()
        first = window.device_list.devices()[0]
        window.device_list.on_toggle(first["id"], False)
        self.assertFalse(window.device_list.devices()[0]["enabled"])
        self.assertIn("0 enabled", window.device_list.lbl_summary.text())
        collected = window._collect_settings_config()
        self.assertFalse(collected["lights"]["devices"][0]["enabled"])


class TestIntegrationsPage(UiTestCase):
    """Four cards, the same enable/path/Browse semantics as before."""

    def test_all_four_integrations_have_a_card(self):
        window = self.window()
        self.assertEqual(set(app.INTEGRATION_KEYS), set(window.integration_widgets))
        self.assertEqual(set(app.INTEGRATION_KEYS), set(window.integration_cards))
        for key, card in window.integration_cards.items():
            self.assertEqual(app.INTEGRATIONS[key]["label"], card.lbl_title.text())
            self.assertTrue(card.chk_enabled.isEnabled())
            self.assertEqual(
                bool(window.config["integrations"][key]["enabled"]),
                card.chk_enabled.isChecked(),
            )

    def test_paths_and_enable_flags_populate_from_the_configuration(self):
        window = self.window()
        for key in app.INTEGRATION_KEYS:
            checkbox, path_edit, browse = window.integration_widgets[key]
            self.assertEqual(
                bool(window.config["integrations"][key]["enabled"]), checkbox.isChecked()
            )
            self.assertEqual(
                window.config["paths"].get(key, ""), path_edit.text()
            )
            del browse

    def test_disabling_an_integration_disables_its_path_and_browse(self):
        window = self.window()
        for key in app.INTEGRATION_KEYS:
            checkbox, path_edit, browse = window.integration_widgets[key]
            checkbox.setChecked(True)
            self.assertTrue(path_edit.isEnabled())
            self.assertTrue(browse.isEnabled())
            checkbox.setChecked(False)
            self.assertFalse(path_edit.isEnabled())
            self.assertFalse(browse.isEnabled())

    def test_browse_uses_the_native_file_dialog(self):
        window = self.window()
        chosen = [r"C:\Program Files\Razer\Synapse3.exe"]
        self._patch(
            app.QFileDialog, "getOpenFileName", lambda *a, **k: (chosen[0], "")
        )
        checkbox, path_edit, browse = window.integration_widgets["razer_synapse"]
        self.assertFalse(browse.isEnabled())
        checkbox.setChecked(True)
        self.assertTrue(browse.isEnabled())
        browse.click()
        self.assertEqual(chosen[0], path_edit.text())

    def test_the_openrgb_elevation_state_still_refreshes(self):
        window = self.window()
        self.assertEqual("Ready", window.lbl_openrgb_elevation.text())
        self.assertIn("set up", window.lbl_openrgb_elevation_hint.text())

        self._patch(
            app,
            "openrgb_elevation_status",
            lambda path, integration_is_enabled=True: elevation_status(
                app.STATUS_UNSAFE_TARGET, "Unsafe location", "The location is user-writable.",
                app.SET_UP_ACTION_LABEL,
            ),
        )
        window._refresh_openrgb_elevation_status()
        self.assertEqual("Unsafe location", window.lbl_openrgb_elevation.text())
        self.assertEqual(
            app.SET_UP_ACTION_LABEL, window.btn_openrgb_elevation.text()
        )

    def test_the_elevation_status_is_unavailable_not_fatal(self):
        window = self.window()
        self._patch(
            app, "openrgb_elevation_status", mock.Mock(side_effect=RuntimeError("boom"))
        )
        self.assertIsNone(window._refresh_openrgb_elevation_status())
        self.assertEqual("Unavailable", window.lbl_openrgb_elevation.text())
        self.assertFalse(window.btn_openrgb_elevation.isEnabled())

    def test_opening_the_page_does_not_provision(self):
        self._patch(app, "apply_openrgb_task_action", mock.Mock(side_effect=AssertionError))
        window = self.window()
        window.select_page("Integrations")
        window._refresh_openrgb_elevation_status()
        self.assertTrue(self.elevation_queries)


class TestOpenRgbServiceConflictUi(UiTestCase):
    """The OpenRGB Windows-service conflict row on the Integrations page."""

    EXPECTED = r"C:\Program Files\OpenRGB\OpenRGB.exe"

    def probe(self, **overrides):
        import openrgb_service as svc

        defaults = {
            "exists": True,
            "state": "stopped",
            "start_type": "manual",
            "binary_path": self.EXPECTED,
            "error": "",
        }
        defaults.update(overrides)
        return svc.OpenRgbServiceProbe(**defaults)

    def patch_probe(self, probe):
        import openrgb_service as svc

        self._patch(svc, "probe_openrgb_service", lambda: probe)
        self._patch(app, "probe_openrgb_service", lambda: probe)

    def test_the_service_row_is_separate_from_the_elevation_row(self):
        window = self.window()
        card = window.integration_cards["openrgb"]
        self.assertIsNotNone(card.lbl_service_status)
        self.assertIsNotNone(card.btn_service_action)
        self.assertIsNotNone(card.lbl_service_hint)
        self.assertIsNot(card.lbl_service_status, card.lbl_status)
        self.assertIsNot(card.btn_service_action, card.btn_action)

    def test_no_conflict_pill_and_disabled_button(self):
        self.patch_probe(self.probe(state="stopped", start_type="disabled"))
        window = self.window()
        status = window._refresh_openrgb_service_status()
        import openrgb_service as svc

        self.assertEqual(status.state, svc.SERVICE_STATE_NO_CONFLICT)
        self.assertEqual("No conflict", window.lbl_openrgb_service.text())
        self.assertFalse(window.btn_openrgb_service.isEnabled())

    def test_conflict_pill_and_enabled_button(self):
        import openrgb_service as svc

        self.patch_probe(self.probe(state="running", start_type="automatic"))
        window = self.window()
        status = window._refresh_openrgb_service_status()
        self.assertEqual(status.state, svc.SERVICE_STATE_CONFLICT)
        self.assertEqual("Conflict detected", window.lbl_openrgb_service.text())
        self.assertTrue(window.btn_openrgb_service.isEnabled())
        self.assertEqual(
            svc.DISABLE_SERVICE_ACTION_LABEL, window.btn_openrgb_service.text()
        )

    def test_manual_idle_is_not_treated_as_disabled(self):
        import openrgb_service as svc

        self.patch_probe(self.probe(state="stopped", start_type="manual"))
        window = self.window()
        status = window._refresh_openrgb_service_status()
        self.assertEqual(status.state, svc.SERVICE_STATE_INSTALLED_IDLE)
        self.assertFalse(window.btn_openrgb_service.isEnabled())
        self.assertIn("Manual", window.lbl_openrgb_service_hint.text())

    def test_path_mismatch_disables_the_fix_button(self):
        import openrgb_service as svc

        self.patch_probe(
            self.probe(
                state="running", start_type="automatic", binary_path=r"C:\Other\App.exe"
            )
        )
        window = self.window()
        status = window._refresh_openrgb_service_status()
        self.assertEqual(status.state, svc.SERVICE_STATE_BINARY_MISMATCH)
        self.assertFalse(window.btn_openrgb_service.isEnabled())

    def test_unknown_disables_the_fix_button(self):
        import openrgb_service as svc

        self.patch_probe(self.probe(exists=False, error="access denied"))
        window = self.window()
        status = window._refresh_openrgb_service_status()
        self.assertEqual(status.state, svc.SERVICE_STATE_UNKNOWN)
        self.assertFalse(window.btn_openrgb_service.isEnabled())

    def test_explicit_confirmation_is_required_before_elevation(self):
        self.patch_probe(self.probe(state="running", start_type="automatic"))
        window = self.window()
        asked = []

        def decline(*_args, **_kwargs):
            asked.append(True)
            return QMessageBox.StandardButton.No

        box = mock.Mock()
        box.question = decline
        box.StandardButton = QMessageBox.StandardButton
        self._patch(app, "QMessageBox", box)
        elevate = mock.Mock()
        self._patch(app, "request_elevated_disable_openrgb_service", elevate)
        window.repair_openrgb_service_conflict()
        self.assertTrue(asked)
        elevate.assert_not_called()

    def test_failed_uac_is_nonfatal_and_reaches_warning(self):
        import windows_tasks as wt

        self.patch_probe(self.probe(state="running", start_type="automatic"))
        window = self.window()
        box = mock.Mock()
        box.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        box.StandardButton = QMessageBox.StandardButton
        self._patch(app, "QMessageBox", box)
        elevate = mock.Mock(
            return_value=wt.ProvisionOutcome(
                False, "Administrator approval was declined.", None
            )
        )
        self._patch(app, "request_elevated_disable_openrgb_service", elevate)
        window.repair_openrgb_service_conflict()  # must not raise
        self.assertTrue(box.warning.called)
        elevate.assert_called_once()

    def test_successful_repair_reaches_trigger_resume(self):
        import windows_tasks as wt

        self.patch_probe(self.probe(state="running", start_type="automatic"))
        window = self.window()
        box = mock.Mock()
        box.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        box.StandardButton = QMessageBox.StandardButton
        self._patch(app, "QMessageBox", box)
        elevate = mock.Mock(
            return_value=wt.ProvisionOutcome(
                True, "The OpenRGB Windows service is stopped and disabled.", 0
            )
        )
        self._patch(app, "request_elevated_disable_openrgb_service", elevate)
        resumed = []
        self._patch(window, "trigger_resume", lambda: resumed.append(True))
        window.repair_openrgb_service_conflict()
        self.assertEqual([True], resumed)

    def test_unreadable_service_status_never_reaches_uac(self):
        """Finding 3: status None must fail closed before confirmation/UAC."""
        self.patch_probe(self.probe(state="running", start_type="automatic"))
        window = self.window()
        self._patch(window, "_refresh_openrgb_service_status", lambda: None)
        box = mock.Mock()
        box.question = mock.Mock(side_effect=AssertionError("confirmation must not be reached"))
        box.StandardButton = QMessageBox.StandardButton
        self._patch(app, "QMessageBox", box)
        elevate = mock.Mock(
            side_effect=AssertionError("elevation must not be requested")
        )
        self._patch(app, "request_elevated_disable_openrgb_service", elevate)
        window.repair_openrgb_service_conflict()  # must not raise
        elevate.assert_not_called()
        box.question.assert_not_called()
        self.assertTrue(box.warning.called)

    def test_active_restore_refuses_service_repair_before_uac(self):
        """Finding 4: an in-progress restore must block service repair/UAC."""
        self.patch_probe(self.probe(state="running", start_type="automatic"))
        window = self.window()
        window.restore_thread = mock.Mock()
        window.restore_thread.isRunning.return_value = True
        box = mock.Mock()
        box.question = mock.Mock(side_effect=AssertionError("confirmation must not be reached"))
        box.StandardButton = QMessageBox.StandardButton
        self._patch(app, "QMessageBox", box)
        elevate = mock.Mock(
            side_effect=AssertionError("elevation must not be requested")
        )
        self._patch(app, "request_elevated_disable_openrgb_service", elevate)
        mutate = mock.Mock(
            side_effect=AssertionError("service mutation must not run")
        )
        import openrgb_service as svc

        self._patch(svc, "disable_openrgb_service", mutate)
        window.repair_openrgb_service_conflict()  # must not raise
        elevate.assert_not_called()
        box.question.assert_not_called()
        mutate.assert_not_called()
        self.assertTrue(box.information.called)
        # Read-only refresh still runs in the outer finally.
        self.assertIsNotNone(window._openrgb_service_status_obj)

    def test_repair_with_no_active_restore_still_reaches_trigger_resume(self):
        """Finding 4 control: idle restore keeps the existing success path."""
        import windows_tasks as wt

        self.patch_probe(self.probe(state="running", start_type="automatic"))
        window = self.window()
        window.restore_thread = None
        box = mock.Mock()
        box.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        box.StandardButton = QMessageBox.StandardButton
        self._patch(app, "QMessageBox", box)
        elevate = mock.Mock(
            return_value=wt.ProvisionOutcome(
                True, "The OpenRGB Windows service is stopped and disabled.", 0
            )
        )
        self._patch(app, "request_elevated_disable_openrgb_service", elevate)
        resumed = []
        self._patch(window, "trigger_resume", lambda: resumed.append(True))
        window.repair_openrgb_service_conflict()
        elevate.assert_called_once()
        self.assertEqual([True], resumed)


class TestAutomationPage(UiTestCase):
    """The split automation form keeps every field and its mapping."""

    def test_the_location_and_automation_fields_populate(self):
        window = self.window()
        self.assertEqual("11.1111", window.txt_lat.text())
        self.assertEqual("-22.2222", window.txt_lon.text())
        self.assertEqual("123", window.txt_elev.text())
        self.assertEqual("2", window.txt_buf.text())
        self.assertTrue(window.chk_close_apps.isChecked())
        self.assertTrue(window.chk_turn_off_yeelight.isChecked())
        self.assertTrue(window.chk_restore_apps.isChecked())
        self.assertTrue(window.chk_turn_on_yeelight.isChecked())
        self.assertFalse(window.chk_launch_synapse.isChecked())
        self.assertTrue(window.chk_wait_synapse.isChecked())
        self.assertEqual("45", window.txt_synapse_timeout.text())

    def test_the_razer_dependency_is_explained_from_the_configuration(self):
        window = self.window()
        self.assertIn("disabled", window.lbl_synapse_dependency.text().lower())

        config = window.config
        config["integrations"]["razer_synapse"]["enabled"] = True
        config["paths"]["razer_synapse"] = r"C:\Razer\Synapse3.exe"
        self.save_config(config)
        window.config = config
        window._populate_settings_widgets(config)
        self.assertIn("in effect", window.lbl_synapse_dependency.text())

    def test_collecting_the_settings_produces_the_expected_configuration(self):
        window = self.window()
        window.txt_lat.setText(" 45.5 ")
        window.txt_lon.setText("-12.25")
        window.txt_elev.setText("80")
        window.txt_buf.setText("1.5")
        window.chk_close_apps.setChecked(False)
        window.chk_turn_off_yeelight.setChecked(False)
        window.chk_restore_apps.setChecked(True)
        window.chk_turn_on_yeelight.setChecked(False)
        window.chk_launch_synapse.setChecked(True)
        window.chk_wait_synapse.setChecked(False)
        window.txt_synapse_timeout.setText("30")
        _checkbox, path_edit, _browse = window.integration_widgets["artemis"]
        _checkbox.setChecked(True)
        path_edit.setText(r"C:\Artemis\Artemis.UI.Windows.exe")

        collected = window._collect_settings_config()

        self.assertEqual(
            {
                "latitude": "45.5",
                "longitude": "-12.25",
                "elevation": "80",
                "light_buffer_hours": "1.5",
            },
            collected["location"],
        )
        self.assertEqual("30", collected["automation"]["razer_synapse_timeout_seconds"])
        self.assertIs(False, collected["automation"]["close_apps_on_sleep"])
        self.assertIs(False, collected["automation"]["turn_off_yeelight_on_sleep"])
        self.assertIs(True, collected["automation"]["restore_apps_on_wake"])
        self.assertIs(False, collected["automation"]["turn_on_yeelight_on_wake_night"])
        self.assertIs(True, collected["automation"]["launch_razer_synapse"])
        self.assertIs(False, collected["automation"]["wait_for_razer_synapse"])
        self.assertIs(True, collected["integrations"]["artemis"]["enabled"])
        self.assertEqual(
            r"C:\Artemis\Artemis.UI.Windows.exe", collected["paths"]["artemis"]
        )
        self.assertEqual(cm.CONFIG_VERSION, collected["config_version"])
        self.assertEqual([], cm.validate_config(collected).errors)

    def test_collecting_the_settings_preserves_unknown_keys(self):
        config = self.default_config()
        config["custom_section"] = {"kept": True}
        config["automation"]["force_silent_launch"] = True
        config["automation"]["another_unknown"] = "kept"
        window = self.window(config)

        collected = window._collect_settings_config()

        self.assertEqual({"kept": True}, collected["custom_section"])
        self.assertIs(True, collected["automation"]["force_silent_launch"])
        self.assertEqual("kept", collected["automation"]["another_unknown"])


class TestSaveImportExport(UiTestCase):
    """Saving keeps validation, the atomic write and the refresh steps."""

    def test_save_validates_then_writes_atomically(self):
        window = self.window()
        calls = []

        real_validate = cm.validate_config

        def recording_validate(config):
            calls.append(("validate", config["location"]["latitude"]))
            return real_validate(config)

        self._patch(app, "validate_config", recording_validate)
        self._patch(
            window.config_manager, "save", lambda config, **kwargs: calls.append(("save", config))
        )
        self._patch(app.QMessageBox, "information", mock.Mock())
        self._patch(app.QMessageBox, "warning", mock.Mock())
        window.txt_lat.setText("11.5")

        window.btn_save.click()

        self.assertEqual(["validate", "save"], [name for name, _value in calls])
        self.assertEqual("11.5", calls[0][1])
        self.assertEqual("11.5", calls[1][1]["location"]["latitude"])
        # The window now works with the saved configuration.
        self.assertEqual("11.5", window.config["location"]["latitude"])
        self.assertEqual("11.5", window.lbl_lat.text())

    def test_save_reports_validation_errors_without_writing(self):
        window = self.window()
        writes = []
        self._patch(
            window.config_manager, "save", lambda config, **kwargs: writes.append(config)
        )
        errors = []
        self._patch(
            app.QMessageBox, "critical", lambda *args, **kwargs: errors.append(args)
        )
        window.txt_lat.setText("not a number")

        window.btn_save.click()

        self.assertEqual([], writes)
        self.assertTrue(errors)
        self.assertEqual("11.1111", window.config["location"]["latitude"])

    def test_import_refreshes_every_redesigned_control(self):
        window = self.window()
        imported = self.default_config()
        imported["location"]["latitude"] = "55.5"
        imported["lights"]["devices"] = [
            {"id": "manual:imported", "name": "Imported", "ip": "10.0.0.9", "enabled": True}
        ]
        imported["integrations"]["razer_synapse"]["enabled"] = True
        imported["paths"]["razer_synapse"] = r"C:\Razer\Synapse3.exe"

        self._patch(
            app.QFileDialog, "getOpenFileName", lambda *a, **k: (r"C:\cfg.json", "")
        )
        self._patch(window.config_manager, "import_config", lambda path: imported)
        self._patch(app.QMessageBox, "information", mock.Mock())
        self._patch(app.QMessageBox, "warning", mock.Mock())

        window.import_config_from_file()
        self.pump()

        self.assertEqual("55.5", window.txt_lat.text())
        self.assertEqual(["Imported"], [d["name"] for d in window.device_list.devices()])
        checkbox, path_edit, _browse = window.integration_widgets["razer_synapse"]
        self.assertTrue(checkbox.isChecked())
        self.assertEqual(r"C:\Razer\Synapse3.exe", path_edit.text())
        self.assertEqual("55.5", window.lbl_lat.text())
        self.assertIn("1 enabled", window.lbl_device_summary.text())

    def test_import_of_an_invalid_file_changes_nothing(self):
        window = self.window()
        before = window.txt_lat.text()
        self._patch(
            app.QFileDialog, "getOpenFileName", lambda *a, **k: (r"C:\bad.json", "")
        )
        self._patch(
            window.config_manager,
            "import_config",
            mock.Mock(side_effect=cm.ConfigValidationError(["bad"])),
        )
        errors = []
        self._patch(app.QMessageBox, "critical", lambda *args, **kwargs: errors.append(args))

        window.import_config_from_file()

        self.assertEqual(before, window.txt_lat.text())
        self.assertTrue(errors)

    def test_export_keeps_the_privacy_warning_and_the_config_format(self):
        window = self.window()
        exported = os.path.join(self.directory, "exported.json")
        self._patch(
            app.QFileDialog, "getSaveFileName", lambda *a, **k: (exported, "")
        )
        answers = []
        self._patch(
            app.QMessageBox,
            "warning",
            lambda *args, **kwargs: answers.append(args) or app.QMessageBox.StandardButton.Yes,
        )
        self._patch(app.QMessageBox, "information", mock.Mock())

        window.export_config_to_file()

        self.assertTrue(answers)
        self.assertIn("private", answers[0][2].lower())
        with open(exported, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(cm.CONFIG_VERSION, written["config_version"])
        self.assertEqual(window.config["location"], written["location"])


class TestStatusAndLogs(UiTestCase):
    """The status hook and the log page keep working."""

    def test_trigger_suspend_updates_the_status_without_attribute_error(self):
        window = self.window()
        stopped = []
        self._patch(app, "terminate_processes_win32", lambda *names: stopped.append(names) or 0)
        self._patch(app, "fire_and_forget_off_devices", mock.Mock())

        window.trigger_suspend()

        self.assertTrue(stopped)
        self.assertEqual("Suspending", window.lbl_system_status.text())

    def test_the_suspend_actions_do_not_run_twice(self):
        window = self.window()
        self._patch(app, "terminate_processes_win32", lambda *names: 0)
        batches = []
        self._patch(app, "fire_and_forget_off_devices", lambda ips, **kwargs: batches.append(list(ips)))

        window.trigger_suspend()
        window.trigger_suspend()

        self.assertEqual(1, len(batches), "the deduplication must still hold")

    def test_the_status_transitions_stay_distinct(self):
        window = self.window()
        self._patch(app, "terminate_processes_win32", lambda *names: 0)
        self._patch(app, "fire_and_forget_off_devices", mock.Mock())

        active = (window.lbl_system_status.text(), window.lbl_system_status.styleSheet())
        window.trigger_suspend()
        suspending = (window.lbl_system_status.text(), window.lbl_system_status.styleSheet())
        window.on_resume_completed()
        restored = (window.lbl_system_status.text(), window.lbl_system_status.styleSheet())

        self.assertIn("System active", active[0])
        self.assertEqual("Suspending", suspending[0])
        self.assertEqual("System active", restored[0])
        # Two different states can never share the same presentation.
        self.assertNotEqual(active[1], suspending[1])
        self.assertEqual(active[1], restored[1])

    def test_the_logs_page_receives_appended_text(self):
        window = self.window()
        window.append_log("hello from the test", logging.INFO)
        self.assertIn("hello from the test", window.log_display.toPlainText())

    def test_the_log_clear_button_works(self):
        window = self.window()
        window.append_log("something", logging.WARNING)
        self.assertTrue(window.log_display.toPlainText())
        window.btn_clear_logs.click()
        self.assertEqual("", window.log_display.toPlainText())

    def test_the_log_view_uses_a_monospace_font(self):
        window = self.window()
        self.assertEqual("log_display", window.log_display.objectName())
        self.assertIn(
            "QTextEdit#log_display",
            theme.APP_QSS,
            "the log view must be styled by the shared theme",
        )
        log_rule = theme.APP_QSS.split("QTextEdit#log_display", 1)[1].split("}", 1)[0]
        self.assertIn(theme.MONO_FONT_FAMILY, log_rule)


class TestStatusPollingCadence(UiTestCase):
    """The Overview check follows the window's visibility, and nothing else.

    The native process listing is the largest recurring cost of an idle session,
    so it runs at the fast cadence only while the dashboard is on screen and at
    the slow cadence while the app is only in the tray - with an immediate
    refresh whenever the dashboard becomes visible, so a result that is stale
    from the hidden period is never displayed.
    """

    def test_a_tray_only_window_polls_at_the_slow_cadence(self):
        window = self.window()
        self.assertFalse(window.isVisible())
        self.assertEqual(
            app.STATUS_POLL_INTERVAL_HIDDEN_MS, window.status_timer.interval()
        )

    def test_showing_and_hiding_switches_the_cadence_both_ways(self):
        window = self.window()
        window.show()
        self.pump()
        self.assertEqual(
            app.STATUS_POLL_INTERVAL_VISIBLE_MS, window.status_timer.interval()
        )
        window.hide()
        self.pump()
        self.assertEqual(
            app.STATUS_POLL_INTERVAL_HIDDEN_MS, window.status_timer.interval()
        )

    def test_showing_the_window_refreshes_exactly_once(self):
        window = self.window()
        window.hide()
        self.pump()
        scans = []
        self._patch(app, "get_running_processes_win32", lambda: scans.append(1) or set())

        window.show()
        self.pump()
        self.assertEqual(1, len(scans), "becoming visible must refresh exactly once")

        # The cadence is already correct now, so a second show must not scan
        # again: the refresh is tied to the hidden period, not to the event.
        window.show()
        self.pump()
        self.assertEqual(1, len(scans), "no extra scan when the cadence is already correct")

    def test_the_tray_cadence_is_much_slower_than_the_visible_one(self):
        self.assertEqual(3000, app.STATUS_POLL_INTERVAL_VISIBLE_MS)
        self.assertEqual(15000, app.STATUS_POLL_INTERVAL_HIDDEN_MS)

    def test_repeated_show_and_hide_never_adds_a_timer(self):
        window = self.window()
        for _ in range(3):
            window.show()
            self.pump()
            window.hide()
            self.pump()

        timers = find_children(window, lambda widget: isinstance(widget, QTimer))
        self.assertIn(window.status_timer, timers)
        self.assertEqual(
            2,
            len(timers),
            "only the process-status and watchdog timers may exist",
        )
        self.assertTrue(window.status_timer.isActive())

    def test_the_check_still_only_reports_process_state(self):
        # The cadence changed; what the check does did not.
        window = self.window()
        self._patch(
            app, "get_running_processes_win32", lambda: {"openrgb.exe"}
        )
        window.show()
        self.pump()
        self.assertEqual("Running", window.service_badges["OpenRGB.exe"].text())
        self.assertEqual(
            "Stopped", window.service_badges["Razer Synapse 3.exe"].text()
        )


class TestUiLogIsBounded(UiTestCase):
    """The in-app log keeps a bounded number of recent lines.

    A tray session runs for days and the rich-text document behind the log view
    grows by roughly 8 KB per appended line, so the bound is what keeps that
    memory from growing for the lifetime of the process.

    The bound is enforced in batches rather than per line, because Qt's own
    per-append block limit was measured at 2.8-4.6 ms per appended line once it
    was reached. Most of these tests shrink the cap and the batch so the
    mechanism can be exercised without appending thousands of lines;
    `test_the_configured_bound_holds_the_document_down` covers the configured
    values.
    """

    def test_the_bound_and_its_batch_are_configured(self):
        self.assertGreaterEqual(app.UI_LOG_MAX_BLOCKS, 2000)
        self.assertLessEqual(app.UI_LOG_MAX_BLOCKS, 5000)
        self.assertGreater(app.UI_LOG_TRIM_BATCH, 0)
        self.assertLess(app.UI_LOG_TRIM_BATCH, app.UI_LOG_MAX_BLOCKS)

    def test_the_configured_bound_holds_the_document_down(self):
        window = self.window()
        total = app.UI_LOG_MAX_BLOCKS + app.UI_LOG_TRIM_BATCH + 40
        for index in range(total):
            window.append_log(f"bounded-log line {index}", logging.INFO)

        document = window.log_display.document()
        self.assertLessEqual(
            document.blockCount(), app.UI_LOG_MAX_BLOCKS + app.UI_LOG_TRIM_BATCH
        )
        text = window.log_display.toPlainText()
        self.assertIn(f"bounded-log line {total - 1}", text)
        self.assertNotIn("bounded-log line 0\n", text)

    def test_the_newest_lines_are_the_ones_kept(self):
        window = self.window()
        self._patch(app, "UI_LOG_MAX_BLOCKS", 10)
        self._patch(app, "UI_LOG_TRIM_BATCH", 5)
        for index in range(40):
            window.append_log(f"bounded-log line {index}", logging.INFO)

        text = window.log_display.toPlainText()
        self.assertIn("bounded-log line 39", text)
        self.assertNotIn("bounded-log line 0\n", text)
        self.assertLessEqual(window.log_display.document().blockCount(), 15)

    def test_the_view_is_trimmed_in_batches_not_on_every_line(self):
        window = self.window()
        self._patch(app, "UI_LOG_MAX_BLOCKS", 10)
        self._patch(app, "UI_LOG_TRIM_BATCH", 5)
        trimmed_at = []
        original = window._trim_log_display

        def counting_trim():
            before = window.log_display.document().blockCount()
            original()
            if window.log_display.document().blockCount() != before:
                trimmed_at.append(before)

        window._trim_log_display = counting_trim

        total = 40
        for index in range(total):
            window.append_log(f"bounded-log line {index}", logging.INFO)

        # One effective trim per batch at most, and each of them only after the
        # document had grown past the bound.
        self.assertGreaterEqual(len(trimmed_at), 1)
        self.assertLessEqual(len(trimmed_at), total // 5 + 1)
        self.assertTrue(all(count > 10 for count in trimmed_at))
        self.assertLessEqual(window.log_display.document().blockCount(), 15)

    def test_clear_still_works_with_the_bound_in_place(self):
        window = self.window()
        self._patch(app, "UI_LOG_MAX_BLOCKS", 10)
        self._patch(app, "UI_LOG_TRIM_BATCH", 5)
        for index in range(30):
            window.append_log(f"bounded-log line {index}", logging.INFO)

        window.btn_clear_logs.click()
        self.assertEqual("", window.log_display.toPlainText())

        window.append_log("after clear", logging.INFO)
        self.assertIn("after clear", window.log_display.toPlainText())

    def test_the_file_log_still_receives_every_record(self):
        window = self.window()
        self._patch(app, "UI_LOG_MAX_BLOCKS", 10)
        self._patch(app, "UI_LOG_TRIM_BATCH", 5)
        path = os.path.join(self.directory, "bounded-log-test.log")
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(handler.close)

        total = 40
        for index in range(total):
            # Delivered straight to the root logger's handlers, so this asserts
            # the handlers' behaviour rather than the logger's level filtering.
            root.handle(
                logging.LogRecord(
                    "cap-test",
                    logging.INFO,
                    __file__,
                    0,
                    "file-log line %d",
                    (index,),
                    None,
                )
            )
        handler.flush()

        with open(path, encoding="utf-8") as handle:
            written = handle.read().splitlines()
        self.assertIn("file-log line 0", written)
        self.assertIn(f"file-log line {total - 1}", written)
        # The view dropped older lines; the file did not.
        self.assertLessEqual(window.log_display.document().blockCount(), 15)
        self.assertNotIn("file-log line 0\n", window.log_display.toPlainText())


class TestStatusPillUpdates(UiTestCase):
    """A pill that did not change is not restyled again.

    Qt re-applies a stylesheet even when it is identical, and the service rows
    set status on a repeating timer where nearly every tick repeats the previous
    state.
    """

    @staticmethod
    def record_restyles(pill):
        """Count calls to `pill.setStyleSheet`, still forwarding to the original."""
        stylesheets = []
        original = pill.setStyleSheet

        def recording(stylesheet):
            stylesheets.append(stylesheet)
            return original(stylesheet)

        pill.setStyleSheet = recording
        return stylesheets

    def test_repeating_the_same_state_does_not_restyle(self):
        window = self.window()
        pill = window.service_badges["OpenRGB.exe"]
        self._patch(app, "get_running_processes_win32", lambda: {"openrgb.exe"})
        window.check_system_statuses()
        stylesheets = self.record_restyles(pill)

        for _ in range(5):
            window.check_system_statuses()

        self.assertEqual([], stylesheets, "an unchanged pill must not be restyled")
        self.assertEqual("Running", pill.text())

    def test_a_changed_state_is_still_applied(self):
        window = self.window()
        pill = window.service_badges["OpenRGB.exe"]
        self.assertEqual("Stopped", pill.text())

        self._patch(app, "get_running_processes_win32", lambda: {"openrgb.exe"})
        window.check_system_statuses()
        self.assertEqual("Running", pill.text())
        self.assertEqual(theme.pill_qss(theme.TONE_OK), pill.styleSheet())

        self._patch(app, "get_running_processes_win32", lambda: set())
        window.check_system_statuses()
        self.assertEqual("Stopped", pill.text())
        self.assertEqual(theme.pill_qss(theme.TONE_NEUTRAL), pill.styleSheet())

    def test_the_tone_alone_is_enough_to_restyle(self):
        pill = components.StatusPill("System active", theme.TONE_OK)
        self.addCleanup(pill.deleteLater)
        stylesheets = self.record_restyles(pill)

        pill.set_status("System active", theme.TONE_WARN)

        self.assertEqual([theme.pill_qss(theme.TONE_WARN)], stylesheets)
        self.assertEqual(theme.pill_qss(theme.TONE_WARN), pill.styleSheet())


class TestDialogsAndWizard(UiTestCase):
    """Dialogs and the wizard share the theme and keep their behaviour."""

    def make_plan(self):
        configured = [{"id": "yeelight:0x0000000012345678", "name": "Desk Lamp", "ip": "192.168.1.50", "enabled": True}]
        discovered = yd.normalize_discovery_results(
            [
                {
                    "ip": "192.168.1.60",
                    "capabilities": {"id": "0x00000000037073d2", "model": "color4"},
                    "name": "yeelink-light-color4_miio1",
                },
                {
                    "ip": "192.168.1.50",
                    "capabilities": {"id": "0x0000000012345678", "model": "color4"},
                    "name": "desk lamp",
                },
            ]
        )
        return yd.plan_discovery(discovered, configured)

    def test_the_discovery_results_dialog_keeps_its_selection_behaviour(self):
        plan = self.make_plan()
        dialog = device_ui.DiscoveryResultsDialog(plan)
        self.addCleanup(dialog.deleteLater)

        selected, names = dialog.selection()
        self.assertEqual([item["key"] for item in plan if item["state"] == yd.MATCH_NEW], selected)
        self.assertTrue(names)
        self.assertEqual(
            "Add Selected",
            dialog.buttons.button(device_ui.QDialogButtonBox.StandardButton.Ok).text(),
        )
        for row, item in enumerate(plan):
            if item["state"] == yd.MATCH_NEW:
                dialog.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual([], dialog.selection()[0])

    def test_the_discovery_results_dialog_refuses_an_all_known_plan(self):
        configured = [{"id": "yeelight:1", "name": "Known", "ip": "10.0.0.1", "enabled": True}]
        discovered = yd.normalize_discovery_results(
            [{"ip": "10.0.0.1", "capabilities": {"id": "1", "model": "color4"}}]
        )
        plan = yd.plan_discovery(discovered, configured)
        dialog = device_ui.DiscoveryResultsDialog(plan)
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(
            dialog.buttons.button(device_ui.QDialogButtonBox.StandardButton.Ok).isEnabled()
        )

    def test_the_device_editor_dialog_still_validates_input(self):
        dialog = device_ui.DeviceEditorDialog()
        self.addCleanup(dialog.deleteLater)
        warnings = []
        self._patch(device_ui.QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args))

        self.assertTrue(dialog.problems())
        dialog.accept()
        self.assertEqual(QDialog.DialogCode.Rejected, dialog.result())
        self.assertTrue(warnings)

        dialog.txt_name.setText("New Lamp")
        dialog.txt_ip.setText("192.168.1.77")
        self.assertEqual([], dialog.problems())
        dialog.accept()
        self.assertEqual(QDialog.DialogCode.Accepted, dialog.result())

    def test_the_device_editor_dialog_rejects_a_duplicate_address(self):
        devices = [{"id": "yeelight:1", "name": "Known", "ip": "10.0.0.1", "enabled": True}]
        dialog = device_ui.DeviceEditorDialog(devices=devices)
        self.addCleanup(dialog.deleteLater)
        dialog.txt_name.setText("Copy")
        dialog.txt_ip.setText("10.0.0.1")
        self.assertTrue(dialog.problems())

    def test_the_first_run_wizard_constructs_all_pages(self):
        instance = wizard.FirstRunWizard(self.manager)
        self.addCleanup(instance.deleteLater)
        # Rendered offscreen only: every page is built and initialized for real.
        instance.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        instance.show()
        self.addCleanup(instance.hide)

        titles = []
        for page_id in range(6):
            instance.setCurrentId(page_id)
            page = instance.currentPage()
            self.assertIsNotNone(page)
            titles.append(page.title())
        self.assertEqual(
            ["Welcome", "Location", "Yeelight devices", "Applications to manage",
             "Automation", "Review and finish"],
            titles,
        )
        self.assertEqual(6, len(instance.pageIds()))

        # The import shortcut still skips straight to the review page.
        instance.setCurrentId(instance.ids["welcome"])
        instance.apply_imported_config(cm.default_config())
        self.assertEqual(instance.ids["review"], instance.nextId())

    def test_the_wizard_can_still_complete_with_zero_yeelights(self):
        instance = wizard.FirstRunWizard(self.manager)
        self.addCleanup(instance.deleteLater)
        page = instance.page_devices
        page.initializePage()
        page.device_list.set_devices([])

        self.assertTrue(page.validatePage())
        self.assertEqual([], instance.working["lights"]["devices"])
        self.assertEqual([], cm.validate_config(instance.working).errors)

    def test_the_wizard_location_page_still_validates(self):
        instance = wizard.FirstRunWizard(self.manager)
        self.addCleanup(instance.deleteLater)
        page = instance.page_location
        self._patch(wizard.QMessageBox, "warning", mock.Mock())

        page.txt_lat.setText("nonsense")
        self.assertFalse(page.validatePage())

        page.txt_lat.setText("11.1111")
        page.txt_lon.setText("-22.2222")
        page.txt_elev.setText("123")
        page.txt_buffer.setText("2")
        self.assertTrue(page.validatePage())
        self.assertEqual("11.1111", instance.working["location"]["latitude"])

    def test_the_wizard_shares_the_application_theme(self):
        instance = wizard.FirstRunWizard(self.manager)
        self.addCleanup(instance.deleteLater)
        stylesheet = instance.styleSheet()
        self.assertIn(theme.ACCENT, stylesheet)
        self.assertIn("QWizard", stylesheet)

    def test_the_dialogs_use_the_shared_dialog_theme(self):
        dialog = device_ui.DeviceEditorDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertIn(theme.BG_APP, dialog.styleSheet())
        self.assertIn(theme.BORDER_STRONG, dialog.styleSheet())
        # The palette is the shared dark one, not a second local variant.
        self.assertEqual(
            theme.BG_APP,
            dialog.palette().color(QPalette.ColorRole.Window).name().upper(),
        )


class TestPresentationLayerIsPresentationOnly(unittest.TestCase):
    """The new UI-only modules must stay free of orchestration."""

    FORBIDDEN = (
        "subprocess",
        "socket",
        "ctypes",
        "requests",
        "windows_tasks",
        "config_manager",
        "yeelight_devices",
        "yeelight_pc_companion",
        "PowerRegisterSuspendResumeNotification",
        "schtasks",
        "taskkill",
        "open(",
    )

    def module_source(self, module):
        with open(module.__file__, encoding="utf-8") as handle:
            return handle.read()

    def test_the_theme_module_contains_no_orchestration(self):
        if SKIP:
            self.skipTest(SKIP_REASON)
        source = self.module_source(theme)
        for token in self.FORBIDDEN:
            self.assertNotIn(token, source, f"ui_theme.py must not reference {token!r}")

    def test_the_components_module_contains_no_orchestration(self):
        if SKIP:
            self.skipTest(SKIP_REASON)
        source = self.module_source(components)
        for token in self.FORBIDDEN:
            self.assertNotIn(token, source, f"ui_components.py must not reference {token!r}")

    def test_the_theme_and_components_modules_are_importable_alone(self):
        if SKIP:
            self.skipTest(SKIP_REASON)
        # `python -c "import ui_theme, ui_components"` must not drag in the
        # application modules: the presentation layer has no circular imports.
        completed = __import__("subprocess").run(
            [sys.executable, "-c", "import ui_theme, ui_components"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))


@unittest.skipIf(SKIP, SKIP_REASON)
class TestThemeSystem(unittest.TestCase):
    """The centralized visual system behaves like a system."""

    @classmethod
    def setUpClass(cls):
        qt_application()

    def test_every_tone_has_a_foreground_and_a_background(self):
        for tone in (theme.TONE_NEUTRAL, theme.TONE_OK, theme.TONE_WARN,
                     theme.TONE_DANGER, theme.TONE_ACCENT):
            self.assertTrue(theme.tone_foreground(tone).startswith("#"))
            self.assertTrue(theme.tone_background(tone).startswith("#"))
            self.assertIn(theme.tone_foreground(tone), theme.pill_qss(tone))

    def test_an_unknown_tone_falls_back_to_neutral(self):
        self.assertEqual(theme.tone_foreground(theme.TONE_NEUTRAL), theme.tone_foreground("nope"))

    def test_the_application_stylesheet_has_no_placeholders_left(self):
        self.assertNotIn("%(", theme.APP_QSS)
        self.assertNotIn("%(", theme.DIALOG_QSS)
        self.assertNotIn("%(", theme.WIZARD_QSS)

    def test_the_stylesheet_no_longer_looks_glassmorphic(self):
        for token in ("qlineargradient", "rgba("):
            self.assertNotIn(token, theme.APP_QSS.lower())

    def test_the_status_pill_reports_its_text(self):
        pill = components.StatusPill("Running", theme.TONE_OK)
        self.assertEqual("Running", pill.text())
        pill.set_status("Stopped", theme.TONE_NEUTRAL)
        self.assertEqual("Stopped", pill.text())

    def test_the_scrollable_helper_wraps_a_page_transparently(self):
        widget, layout = components.page_body()
        self.addCleanup(widget.deleteLater)
        self.assertIsNotNone(layout)
        scroller = components.scrollable(widget)
        self.addCleanup(scroller.deleteLater)
        self.assertTrue(scroller.widgetResizable())
        self.assertIs(widget, scroller.widget())


if __name__ == "__main__":
    unittest.main()
