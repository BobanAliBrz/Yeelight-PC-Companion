"""First-run setup wizard for Yeelight PC Companion.

A normal user must never have to hand-edit ``config.json``. When no valid
configuration exists, this wizard collects the minimum information the
application needs (location, Yeelight devices, integration enable flags,
automation preferences) and writes the configuration atomically.

If the user cancels, nothing is written: the caller must not start the
application with placeholder values.

The module depends on PyQt6 only (plus :mod:`config_manager` for all validation)
so the configuration logic itself stays GUI-free and unit-testable.
"""

from __future__ import annotations

import copy
import logging
import os

from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from ui_theme import APP_QSS, WIZARD_QSS, apply_top_level_theme

from config_manager import (
    CONFIG_VERSION,
    INTEGRATION_KEYS,
    INTEGRATIONS,
    ConfigError,
    ConfigValidationError,
    LEGACY_UNCONSUMED_AUTOMATION_KEYS,
    integration_enabled,
    integration_path,
    storage_mode,
    validate_config,
    validate_device_entry,
)

from yeelight_device_ui import DeviceListWidget
from yeelight_devices import device_summary

from windows_tasks import (
    ACTION_PROVISION,
    STATUS_UNSAFE_TARGET,
    apply_openrgb_task_action,
    openrgb_elevation_status,
)

# Styling now comes from the shared presentation layer (`ui_theme`); the wizard
# adds only the QWizard-specific rules on top of the application stylesheet.


def _parse_number(text, label, minimum, maximum, unit=""):
    """Return ``(value, error_message)`` for a user-entered number."""
    raw = (text or "").strip()
    if not raw:
        return None, f"{label} is required."
    try:
        value = float(raw)
    except ValueError:
        return None, f"{label} must be a number{unit}."
    if value < minimum or value > maximum:
        return None, f"{label} must be between {minimum} and {maximum}{unit}."
    return value, None


def _elevation_warning(detail=""):
    """The wizard's warning when the seamless elevated launch is not available.

    The specific reason (for example "this location is user-writable") is kept
    when there is one, because it tells the user exactly what to change.
    """
    text = "OpenRGB was configured, but seamless elevated launch could not be enabled."
    if detail:
        text += f"\n\n{detail}"
    return f"{text}\n\nYou can repair this later from the Integrations page."


class FirstRunWizard(QWizard):
    """Multi-page setup wizard that writes a valid configuration on Finish."""

    def __init__(self, config_manager, stylesheet=None, initial_error=None, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.working = config_manager.default_config()
        self.config_saved = False
        self._imported = False

        self.setWindowTitle("Yeelight PC Companion - Setup")
        # ClassicStyle honours the (dark) palette for the page title/subtitle,
        # unlike ModernStyle which paints the header in fixed light-theme colours.
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.NoCancelButtonOnLastPage, False)
        # The shared theme, plus the QWizard-specific rules.
        apply_top_level_theme(self, (stylesheet or APP_QSS) + WIZARD_QSS)
        self.setMinimumSize(820, 580)
        self.setButtonText(QWizard.WizardButton.FinishButton, "Finish and Start")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Cancel setup")

        self.page_welcome = WelcomePage(self, initial_error)
        self.page_location = LocationPage(self)
        self.page_devices = DevicesPage(self)
        self.page_integrations = IntegrationsPage(self)
        self.page_automation = AutomationPage(self)
        self.page_review = ReviewPage(self)

        self.ids = {
            "welcome": self.addPage(self.page_welcome),
            "location": self.addPage(self.page_location),
            "devices": self.addPage(self.page_devices),
            "integrations": self.addPage(self.page_integrations),
            "automation": self.addPage(self.page_automation),
            "review": self.addPage(self.page_review),
        }

    # --- navigation ------------------------------------------------------
    def nextId(self):
        """After a successful import, jump straight to the review page."""
        if self.currentId() == self.ids["welcome"] and self._imported:
            return self.ids["review"]
        return super().nextId()

    # --- shared state ----------------------------------------------------
    def apply_imported_config(self, config):
        """Adopt a validated, migrated configuration imported by the user."""
        self.working = copy.deepcopy(config)
        self.working["config_version"] = CONFIG_VERSION
        self._imported = True

    def storage_description(self):
        mode = storage_mode(self.config_manager.app_dir)
        if mode == "portable":
            return "the application folder (portable mode)"
        if mode == "source":
            return "the application folder (development run)"
        return "your Windows user data folder (%LOCALAPPDATA%)"

    # --- finish ----------------------------------------------------------
    def accept(self):
        """Finish: validate everything, then save atomically.

        A Python exception escaping this Qt slot would abort the process, so any
        unexpected failure is reported instead of propagating.
        """
        try:
            self._finish_setup()
        except Exception:
            logging.exception("[SETUP] Unexpected error while saving the configuration.")
            QMessageBox.critical(
                self,
                "Setup could not be saved",
                "An unexpected error occurred while writing the configuration.\n\n"
                "Nothing was written. See the debug log for details.",
            )

    def _finish_setup(self):
        config = copy.deepcopy(self.working)
        config["config_version"] = CONFIG_VERSION

        result = validate_config(config)
        if result.errors:
            QMessageBox.critical(
                self,
                "Setup cannot be completed",
                "Please correct the following before finishing:\n\n- " + "\n- ".join(result.errors),
            )
            return

        try:
            self.config_manager.save(config, create_backup=True)
        except (ConfigError, OSError) as exc:
            QMessageBox.critical(
                self,
                "Setup could not be saved",
                f"The configuration could not be written:\n\n{exc}",
            )
            return

        for warning in result.warnings:
            logging.warning("[SETUP] %s", warning)

        self.working = config
        self.config_saved = True

        # The configuration is already saved, so a declined or failed one-time
        # administrator approval can never discard it.
        elevation_warning = self._set_up_openrgb_elevation(config)

        notes = list(result.warnings)
        if elevation_warning:
            notes.append(elevation_warning)
        if notes:
            QMessageBox.information(
                self,
                "Setup complete",
                "Yeelight PC Companion is configured.\n\nPlease note:\n\n- "
                + "\n- ".join(notes),
            )
        super().accept()

    def _set_up_openrgb_elevation(self, config):
        """Offer the one-time administrator approval for the seamless launch.

        Returns a warning message when the seamless launch could not be enabled,
        or None when there is nothing to report. Only OpenRGB needs this: it is
        the one application that must run with administrator rights.

        An OpenRGB that the user's own account can modify cannot be given to a
        privileged task at all, so that case is explained instead of asking for
        an approval that would be refused.
        """
        if not integration_enabled(config, "openrgb"):
            return None
        openrgb_path = integration_path(config, "openrgb")
        if not openrgb_path:
            return None

        status = None
        try:
            status = openrgb_elevation_status(openrgb_path)
            if status.is_ready:
                return None
        except Exception:
            logging.exception("[SETUP] Could not inspect the OpenRGB launch task.")

        if status is not None and status.state == STATUS_UNSAFE_TARGET:
            return f"{status.detail}\n\nYou can repair this later from the Integrations page."

        answer = QMessageBox.question(
            self,
            "One-time administrator approval",
            "Yeelight PC Companion needs one-time administrator approval so it can "
            "start OpenRGB automatically after wake without future UAC prompts.\n\n"
            "Windows will show a permission prompt now. After you approve it once, "
            "waking the PC will start OpenRGB silently.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return _elevation_warning()

        try:
            ok, message = apply_openrgb_task_action(ACTION_PROVISION, openrgb_path)
        except Exception:
            logging.exception("[SETUP] Unexpected error while setting up the OpenRGB launch task.")
            ok, message = False, ""

        if ok:
            return None
        return _elevation_warning(message)


class WelcomePage(QWizardPage):
    def __init__(self, wizard, initial_error=None):
        super().__init__()
        self.wizard = wizard
        self.setTitle("Welcome")
        self.setSubTitle(
            "Yeelight PC Companion keeps your Yeelight lights and RGB software in step "
            "with your PC's sleep/wake cycle and the local day/night cycle."
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        explanation = QLabel(
            "On sleep it turns your lights off and stops the RGB applications. On wake it "
            "restores them, and at night it switches your Yeelight devices back on.\n\n"
            "Setup takes about a minute and only asks for your location (used locally to "
            "calculate sunrise and sunset), your Yeelight devices, and which RGB "
            "applications you use."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        if initial_error:
            problem = QLabel(f"Existing configuration problem: {initial_error}")
            problem.setObjectName("warning")
            problem.setWordWrap(True)
            layout.addWidget(problem)

        group = QGroupBox("How would you like to start?")
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(10)

        self.radio_fresh = QRadioButton("Set up Yeelight PC Companion step by step")
        self.radio_fresh.setChecked(True)
        self.radio_import = QRadioButton("Import an existing Yeelight PC Companion configuration")
        self.radio_fresh.toggled.connect(self._update_import_state)

        self.btn_import = QPushButton("Choose configuration file...")
        self.btn_import.setObjectName("btn_primary")
        self.btn_import.clicked.connect(self._choose_import_file)

        self.lbl_import_status = QLabel("")
        self.lbl_import_status.setObjectName("hint")
        self.lbl_import_status.setWordWrap(True)

        group_layout.addWidget(self.radio_fresh)
        group_layout.addWidget(self.radio_import)
        import_row = QHBoxLayout()
        import_row.addWidget(self.btn_import)
        import_row.addWidget(self.lbl_import_status, 1)
        group_layout.addLayout(import_row)

        layout.addWidget(group)
        layout.addStretch(1)
        self._update_import_state()

    def _update_import_state(self):
        importing = self.radio_import.isChecked()
        self.btn_import.setEnabled(importing)

    def _choose_import_file(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Import configuration", "", "JSON configuration (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            config, from_version = self.wizard.config_manager.load_external(path)
        except ConfigValidationError as exc:
            QMessageBox.critical(
                self,
                "That configuration cannot be used",
                "Nothing was changed. Please fix the following and try again:\n\n- "
                + "\n- ".join(exc.errors),
            )
            return
        except ConfigError as exc:
            QMessageBox.critical(self, "That configuration cannot be used", f"Nothing was changed.\n\n{exc}")
            return

        self.radio_import.setChecked(True)
        self.wizard.apply_imported_config(config)
        note = ""
        if from_version != CONFIG_VERSION:
            note = f" (upgraded from version {from_version} to {CONFIG_VERSION})"
        self.lbl_import_status.setText(
            f"Imported {os.path.basename(path)}{note}. Press Next to review, then Finish."
        )
        self.completeChanged.emit()

    def validatePage(self):
        if self.radio_import.isChecked() and not self.wizard._imported:
            QMessageBox.warning(
                self,
                "Choose a configuration file",
                "Select 'Set up Yeelight PC Companion step by step', or choose a configuration "
                "file to import.",
            )
            return False
        return True


class LocationPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        self.setTitle("Location")
        self.setSubTitle(
            "Your coordinates are used only on this computer to calculate sunrise and sunset."
        )

        layout = QVBoxLayout(self)
        group = QGroupBox("Sun position")
        form = QFormLayout(group)
        form.setContentsMargins(16, 20, 16, 16)
        form.setSpacing(10)

        self.txt_lat = QLineEdit()
        self.txt_lon = QLineEdit()
        self.txt_elev = QLineEdit()
        self.txt_buffer = QLineEdit()

        form.addRow(QLabel("Latitude (-90 to 90):"), self.txt_lat)
        form.addRow(QLabel("Longitude (-180 to 180):"), self.txt_lon)
        form.addRow(QLabel("Elevation (meters, 0 is fine):"), self.txt_elev)
        form.addRow(QLabel("Treat the sky as dark this many hours around sunset/sunrise:"), self.txt_buffer)

        hint = QLabel(
            "Nothing is transmitted anywhere: the coordinates never leave this computer. "
            "A buffer of 1-2 hours turns the lights on slightly before sunset and keeps them "
            "on slightly after sunrise."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(hint)
        layout.addStretch(1)

    def initializePage(self):
        location = self.wizard.working["location"]
        self.txt_lat.setText(str(location.get("latitude", "")))
        self.txt_lon.setText(str(location.get("longitude", "")))
        self.txt_elev.setText(str(location.get("elevation", "0")))
        self.txt_buffer.setText(str(location.get("light_buffer_hours", "2")))

    def validatePage(self):
        latitude, error = _parse_number(self.txt_lat.text(), "Latitude", -90, 90)
        if error:
            return self._reject(error)
        longitude, error = _parse_number(self.txt_lon.text(), "Longitude", -180, 180)
        if error:
            return self._reject(error)
        elevation, error = _parse_number(self.txt_elev.text(), "Elevation", -500, 12000, unit=" meters")
        if error:
            return self._reject(error)
        buffer_hours, error = _parse_number(
            self.txt_buffer.text(), "Sunrise/sunset buffer", 0, 24, unit=" hours"
        )
        if error:
            return self._reject(error)

        self.wizard.working["location"].update(
            {
                "latitude": self.txt_lat.text().strip(),
                "longitude": self.txt_lon.text().strip(),
                "elevation": elevation,
                "light_buffer_hours": buffer_hours,
            }
        )
        return True

    def _reject(self, message):
        QMessageBox.warning(self, "Check your location", message)
        return False


class DevicesPage(QWizardPage):
    """Yeelight devices: discover them on the LAN, or add one by hand.

    Any number of devices is allowed, including none at all — a user who only
    wants the RGB-application automation can finish setup without a single
    Yeelight device and add one later on the Devices page.
    """

    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        self.setTitle("Yeelight devices")
        self.setSubTitle(
            "Optional: find the Yeelight devices on your network, or add one by address. "
            "You can finish setup with no devices and add them later."
        )

        layout = QVBoxLayout(self)
        group = QGroupBox("Devices")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(16, 20, 16, 16)
        group_layout.setSpacing(8)

        self.device_list = DeviceListWidget()
        self.device_list.changed.connect(self.completeChanged.emit)
        group_layout.addWidget(self.device_list)

        hint = QLabel(
            "Discover searches your local network for Yeelight devices (LAN Control "
            "must be enabled in the Yeelight app). Add Manually asks for a name and an IP "
            "address instead. The app talks to these devices directly on your local network, "
            "and every list entry can be edited or removed later on the Devices page."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(hint)
        layout.addStretch(1)

    def initializePage(self):
        self.device_list.set_devices(self.wizard.working["lights"].get("devices") or [])

    def validatePage(self):
        """Store the device list. Zero devices is a valid answer."""
        devices = self.device_list.devices()
        errors = []
        for position, entry in enumerate(devices, start=1):
            errors.extend(validate_device_entry(entry, position))
        if errors:
            QMessageBox.warning(
                self,
                "Check the Yeelight devices",
                "Please correct the following:\n\n- " + "\n- ".join(errors),
            )
            return False

        self.wizard.working["lights"]["devices"] = devices
        return True


class IntegrationsPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        self.setTitle("Applications to manage")
        self.setSubTitle("Each application is optional. Disabled applications are skipped completely.")

        layout = QVBoxLayout(self)
        group = QGroupBox("Integrations")
        grid = QGridLayout(group)
        grid.setContentsMargins(16, 20, 16, 16)
        grid.setSpacing(10)

        self.rows = {}
        for row, key in enumerate(INTEGRATION_KEYS):
            meta = INTEGRATIONS[key]
            checkbox = QCheckBox(meta["label"])
            path_edit = QLineEdit()
            path_edit.setPlaceholderText(f"Path to {meta['hint']}")
            browse = QPushButton("Browse...")
            browse.setMaximumWidth(110)

            checkbox.toggled.connect(path_edit.setEnabled)
            checkbox.toggled.connect(browse.setEnabled)
            path_edit.setEnabled(False)
            browse.setEnabled(False)
            browse.clicked.connect(lambda _checked=False, edit=path_edit: self._browse(edit))

            grid.addWidget(checkbox, row, 0)
            grid.addWidget(path_edit, row, 1)
            grid.addWidget(browse, row, 2)
            self.rows[key] = (checkbox, path_edit, browse)

        hint = QLabel(
            "A disabled integration is not launched, stopped or waited for, and its status is "
            "ignored. If an enabled application is not installed yet you will see a warning when "
            "you finish, and it is skipped at runtime until it appears."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(hint)
        layout.addStretch(1)

    def _browse(self, path_edit):
        current = path_edit.text().strip()
        start_dir = os.path.dirname(current) if current and os.path.isdir(os.path.dirname(current)) else ""
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Select executable", start_dir, "Programs (*.exe);;All files (*)"
        )
        if path:
            path_edit.setText(path)

    def initializePage(self):
        for key, (checkbox, path_edit, _browse_button) in self.rows.items():
            checkbox.setChecked(bool(self.wizard.working["integrations"][key]["enabled"]))
            path_edit.setText(self.wizard.working["paths"].get(key, "") or "")

    def validatePage(self):
        for key, (checkbox, path_edit, _browse_button) in self.rows.items():
            meta = INTEGRATIONS[key]
            path = path_edit.text().strip()
            if checkbox.isChecked():
                if not path:
                    QMessageBox.warning(
                        self,
                        "Missing executable path",
                        f"{meta['label']} is enabled but no executable path is set.\n\n"
                        "Choose the executable, or clear the checkbox to disable it.",
                    )
                    return False
                if not os.path.isfile(path):
                    answer = QMessageBox.question(
                        self,
                        "Executable not found",
                        f"{meta['hint']} was not found at:\n{path}\n\n"
                        "Use this path anyway? It is skipped at runtime until the file exists.",
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return False

        for key, (checkbox, path_edit, _browse_button) in self.rows.items():
            self.wizard.working["integrations"][key]["enabled"] = checkbox.isChecked()
            self.wizard.working["paths"][key] = path_edit.text().strip()
        return True


class AutomationPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        self.setTitle("Automation")
        self.setSubTitle("Choose what should happen when the PC sleeps and wakes.")

        layout = QVBoxLayout(self)
        group = QGroupBox("Behaviour")
        form = QFormLayout(group)
        form.setContentsMargins(16, 20, 16, 16)
        form.setSpacing(10)

        self.chk_close_apps = QCheckBox("Close the light-control applications when the PC sleeps")
        self.chk_turn_off_lights = QCheckBox("Turn the Yeelight devices off when the PC sleeps")
        self.chk_restore_apps = QCheckBox("Restore everything when the PC wakes")
        self.chk_launch_synapse = QCheckBox("Also launch Razer Synapse on wake (it takes screen focus)")
        self.chk_wait_synapse = QCheckBox("Wait for Razer Synapse when it starts by itself")
        self.txt_synapse_timeout = QLineEdit()

        form.addRow(self.chk_close_apps)
        form.addRow(self.chk_turn_off_lights)
        form.addRow(self.chk_restore_apps)
        form.addRow(self.chk_launch_synapse)
        form.addRow(self.chk_wait_synapse)
        form.addRow(QLabel("Razer Synapse detection timeout (seconds):"), self.txt_synapse_timeout)

        hint = QLabel(
            "Razer Synapse options only apply when the Razer Synapse integration is enabled.\n"
            "Compatibility settings kept for existing setups but not currently acted on by the "
            "runtime: " + ", ".join(LEGACY_UNCONSUMED_AUTOMATION_KEYS) + "."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(hint)
        layout.addStretch(1)

    def initializePage(self):
        automation = self.wizard.working["automation"]
        self.chk_close_apps.setChecked(bool(automation.get("close_apps_on_sleep", True)))
        self.chk_turn_off_lights.setChecked(bool(automation.get("turn_off_yeelight_on_sleep", True)))
        self.chk_restore_apps.setChecked(bool(automation.get("restore_apps_on_wake", True)))
        self.chk_launch_synapse.setChecked(bool(automation.get("launch_razer_synapse", False)))
        self.chk_wait_synapse.setChecked(bool(automation.get("wait_for_razer_synapse", True)))
        self.txt_synapse_timeout.setText(
            str(int(float(automation.get("razer_synapse_timeout_seconds", 45))))
        )

    def validatePage(self):
        timeout, error = _parse_number(
            self.txt_synapse_timeout.text(), "Razer Synapse detection timeout", 0, 600, unit=" seconds"
        )
        if error:
            QMessageBox.warning(self, "Check the automation settings", error)
            return False

        self.wizard.working["automation"].update(
            {
                "close_apps_on_sleep": self.chk_close_apps.isChecked(),
                "turn_off_yeelight_on_sleep": self.chk_turn_off_lights.isChecked(),
                "restore_apps_on_wake": self.chk_restore_apps.isChecked(),
                "launch_razer_synapse": self.chk_launch_synapse.isChecked(),
                "wait_for_razer_synapse": self.chk_wait_synapse.isChecked(),
                "razer_synapse_timeout_seconds": timeout,
            }
        )
        return True


class ReviewPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        self.setTitle("Review and finish")
        self.setSubTitle("Check the summary, then finish to save the configuration.")

        layout = QVBoxLayout(self)
        group = QGroupBox("Summary")
        self.summary_layout = QFormLayout(group)
        self.summary_layout.setContentsMargins(16, 20, 16, 16)
        self.summary_layout.setSpacing(8)
        layout.addWidget(group)

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("warning")
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        self.note = QLabel("")
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        layout.addStretch(1)

        self._summary_rows = []
        for label in ("Location", "Yeelight devices", "Enabled applications", "Automation"):
            value = QLabel("-")
            value.setWordWrap(True)
            self.summary_layout.addRow(QLabel(f"{label}:"), value)
            self._summary_rows.append(value)

    def initializePage(self):
        config = self.wizard.working
        location = config["location"]
        latitude = str(location.get("latitude", "")).strip()
        longitude = str(location.get("longitude", "")).strip()
        if latitude and longitude:
            location_text = (
                f"{latitude}, {longitude} (buffer {location.get('light_buffer_hours')} h)"
            )
        else:
            location_text = "Not configured"

        devices = config["lights"].get("devices") or []
        devices_text = device_summary(devices)
        if not devices:
            devices_text = "None yet (light control stays inactive until you add one)"

        enabled = [
            INTEGRATIONS[key]["label"]
            for key in INTEGRATION_KEYS
            if config["integrations"][key]["enabled"]
        ]
        integrations_text = ", ".join(enabled) if enabled else "None"

        automation = config["automation"]
        automation_text = (
            f"Lights off on sleep: {'on' if automation.get('turn_off_yeelight_on_sleep') else 'off'}"
            f" | Restore on wake: {'on' if automation.get('restore_apps_on_wake') else 'off'}"
            f" | Close apps on sleep: {'on' if automation.get('close_apps_on_sleep') else 'off'}"
        )

        for value, text in zip(
            self._summary_rows, (location_text, devices_text, integrations_text, automation_text)
        ):
            value.setText(text)

        self.note.setText(
            "Finishing writes config.json to "
            + self.wizard.storage_description()
            + " (created automatically if needed). An existing configuration is backed up once as "
            "config.json.bak. Coordinates and local addresses are stored locally only."
        )

        result = validate_config(config)
        self.warning_label.setText(
            "\n".join(f"Note: {warning}" for warning in result.warnings)
        )
        self.completeChanged.emit()

    def validatePage(self):
        return True


def run_first_run_wizard(config_manager, stylesheet=None, parent=None, initial_error=None):
    """Show the wizard. Returns True only if a valid configuration was saved."""
    wizard = FirstRunWizard(config_manager, stylesheet=stylesheet, initial_error=initial_error, parent=parent)
    accepted = wizard.exec() == QWizard.DialogCode.Accepted
    if not (accepted and wizard.config_saved):
        logging.info("[SETUP] First-run setup was cancelled; no configuration was written.")
        return False
    logging.info("[SETUP] First-run setup completed; configuration saved.")
    return True
