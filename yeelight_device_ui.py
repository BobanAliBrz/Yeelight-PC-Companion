"""Device management UI shared by the Settings tab and the first-run wizard.

Three pieces:

* :class:`DeviceDiscoveryThread` — a ``QThread`` worker that runs one
  user-triggered LAN discovery. Discovery blocks for its whole (bounded)
  timeout, so it must never run on the GUI thread, and it is never started
  automatically, never repeated in the background and never started from the
  suspend path.
* :class:`DiscoveryResultsDialog` — the result list with the New / Already
  added / IP changed distinction and per-device selection.
* :class:`DeviceListWidget` — the editable list of configured devices (name,
  address, enable checkbox, edit, remove) plus the **Discover** and
  **Add Manually** buttons.

All counting, matching and list editing logic lives in :mod:`yeelight_devices`
and :mod:`config_manager`; this module only drives it from Qt.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QEventLoop, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config_manager import validate_device_entry
from ui_theme import (
    SPACE_M,
    SPACE_S,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
    apply_dialog_style,
)
from yeelight_devices import (
    DISCOVERY_TIMEOUT_SECONDS,
    MATCH_ID_AVAILABLE,
    MATCH_IP_CHANGED,
    MATCH_LABELS,
    MATCH_NEW,
    DeviceError,
    DiscoveryReport,
    add_device,
    apply_discovery_plan,
    clamped_discovery_timeout,
    default_device_name,
    device_summary,
    discover_devices,
    duplicate_ip_error,
    plan_discovery,
    remove_device,
    selectable_match,
    update_device,
)

# ---------------------------------------------------------
# User-facing texts
# ---------------------------------------------------------
DISCOVERY_PROGRESS_TEXT = "Searching your local network for Yeelight devices..."
DISCOVERY_EMPTY_TEXT = (
    "No Yeelight devices were found.\n\n"
    "Make sure LAN Control is enabled in the Yeelight app and the devices are on "
    "the same network."
)
DISCOVERY_ALL_KNOWN_TEXT = (
    "Every Yeelight device that answered is already in your list. Nothing to add."
)
DISCOVERY_DIALOG_TITLE = "Discover Yeelight Devices"
DISCOVERY_TIMEOUT_TEXT = "The network search did not finish in time."

# The application stylesheet is set on the main window, so a dialog needs the
# shared theme applied explicitly. `ui_theme` owns those colours; this module
# only applies them (the wizard does the same).
def apply_device_dialog_style(dialog):
    """Give a device dialog the same dark look as the application window."""
    apply_dialog_style(dialog)

# The search itself is bounded; this is only a guard so a stuck worker can never
# leave the (modal) progress dialog on screen forever.
DISCOVERY_UI_GRACE_SECONDS = 5

# Workers that have been started and have not reported `finished` yet. A QThread
# that is still running must never be destroyed — not even when the UI has
# already given up on it — so every worker stays referenced here until its own
# `finished` signal says it is over. Only explicit user actions create one, and a
# search always ends by itself (it is bounded), so this stays a very short list.
LIVE_DISCOVERY_WORKERS = []


def discovery_guard_milliseconds(timeout=DISCOVERY_TIMEOUT_SECONDS):
    """How long the UI waits for one discovery before giving up on it.

    Deliberately derived from the *clamped* timeout the search itself uses (plus
    `DISCOVERY_UI_GRACE_SECONDS` for the worker to report back), never from the
    raw request: a caller passing ``999`` must not produce a 1004-second modal
    guard while the discovery behind it stops after 30 seconds. An unusable value
    falls back to the default instead of raising.
    """
    return int((clamped_discovery_timeout(timeout) + DISCOVERY_UI_GRACE_SECONDS) * 1000)


def _release_discovery_worker(worker):
    """`finished` handler: the worker is over, so it can be deleted.

    Safe from whichever thread delivers it: the bookkeeping is plain Python and
    ``deleteLater()`` always posts the deletion to the thread the object lives in
    (the GUI thread), instead of destroying a QThread inside its own `finished`
    emission.
    """
    try:
        LIVE_DISCOVERY_WORKERS.remove(worker)
    except ValueError:  # pragma: no cover - defensive
        pass
    try:
        worker.deleteLater()
    except RuntimeError:  # pragma: no cover - already gone
        pass


def _forget_discovery_result(worker, slot):
    """Detach a UI result handler so a late result can never reach it.

    Used when the guard has already returned to the caller: the stale result of
    that abandoned search must not be able to touch anything the user sees, not
    even while a *later* discovery is running.
    """
    try:
        worker.result_ready.disconnect(slot)
    except (TypeError, RuntimeError):
        # Already disconnected, or the worker is already gone: nothing to do.
        pass


class DeviceDiscoveryThread(QThread):
    """Runs exactly one LAN discovery off the GUI thread.

    Emits :class:`DiscoveryReport` — including the failure case, because a
    blocked firewall must never leave the caller waiting for a signal that never
    arrives. Nothing else happens in this thread; it never touches the
    configuration or the UI.
    """

    result_ready = pyqtSignal(object)

    def __init__(self, timeout=DISCOVERY_TIMEOUT_SECONDS, parent=None):
        super().__init__(parent)
        self.timeout = timeout

    def collect(self):
        """The discovery call itself (no Qt): returns a DiscoveryReport."""
        try:
            return discover_devices(timeout=self.timeout)
        except Exception as exc:  # defensive: a worker must always report back
            logging.exception("[DISCOVERY] The network search failed unexpectedly.")
            return DiscoveryReport([], f"The network search failed ({exc}).")

    def run(self):
        report = self.collect()
        self.result_ready.emit(report)


def run_discovery(parent=None, timeout=DISCOVERY_TIMEOUT_SECONDS):
    """Run one bounded discovery with a modal progress dialog.

    The search runs in :class:`DeviceDiscoveryThread`; this function only waits
    for its result using a nested event loop, so the progress dialog stays
    responsive. A missed/failed worker can never hang the caller, and the wait is
    bounded by the *same* clamped timeout the search itself uses.

    When the guard expires, this returns to the caller immediately: the search is
    simply abandoned (never waited for again, never terminated), the progress
    dialog is closed, and the still-running worker is left to end by itself while
    staying safely referenced. Its late result is detached, so it cannot reach
    the UI after the fact.
    """
    seconds = clamped_discovery_timeout(timeout)
    progress = QProgressDialog(DISCOVERY_PROGRESS_TEXT, "", 0, 0, parent)
    progress.setWindowTitle(DISCOVERY_DIALOG_TITLE)
    apply_device_dialog_style(progress)
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setCancelButton(None)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)

    worker = DeviceDiscoveryThread(timeout=seconds)
    loop = QEventLoop()
    outcome = {}

    def on_result(report):
        outcome["report"] = report
        loop.quit()

    worker.result_ready.connect(on_result)
    # The worker owns its lifetime: it is retained (and deleted) only through its
    # own `finished` signal. That is what makes it safe not to wait for it below.
    LIVE_DISCOVERY_WORKERS.append(worker)
    worker.finished.connect(lambda: _release_discovery_worker(worker))
    worker.finished.connect(worker.deleteLater)

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)

    progress.show()
    try:
        worker.start()
        guard.start(discovery_guard_milliseconds(seconds))
        loop.exec()
    finally:
        guard.stop()
        progress.close()
        _forget_discovery_result(worker, on_result)

    report = outcome.get("report")
    if report is None:
        # The guard fired (or the signal could not be delivered): report it
        # instead of pretending zero devices answered.
        logging.warning("[DISCOVERY] The network search did not report back in time.")
        return DiscoveryReport([], DISCOVERY_TIMEOUT_TEXT)
    return report


def _discovery_details(item):
    """Compact, non-sensitive detail text for one discovery result.

    Model and reported power state are the two pieces of discovery metadata that
    help a user tell two devices apart; firmware is stored in the configuration
    but is not worth a column here.
    """
    device = item.get("discovered") or {}
    bits = []
    model = device.get("model")
    if model:
        bits.append(f"model {model}")
    power = device.get("power")
    if power:
        bits.append(f"reported {power}")
    return "  |  ".join(bits)


def _discovery_status(item):
    """The user-facing status text of one discovery result."""
    state = item.get("state")
    if state == MATCH_IP_CHANGED:
        previous = item.get("previous_ip") or "(unknown)"
        return f"IP changed: {previous} -> {item['discovered']['ip']}"
    if state == MATCH_ID_AVAILABLE:
        return "Already added - its Yeelight device ID can be linked"
    return MATCH_LABELS.get(state, "New")


def _display_name(item):
    """The name a new device would start with (or the existing one's name)."""
    existing = item.get("configured")
    if isinstance(existing, dict):
        return str(existing.get("name") or "")
    device = item.get("discovered") or {}
    return default_device_name(device.get("name"), device.get("model"))


def _device_detail(device):
    """Address plus the discovery metadata already stored for one device.

    Only facts the configuration already holds are shown. There is deliberately
    no online/offline badge: that state simply does not exist in this
    application, and nothing is probed here to invent it.
    """
    bits = []
    address = str(device.get("ip") or "")
    if address:
        bits.append(address)
    model = device.get("model")
    if isinstance(model, str) and model.strip():
        bits.append(f"model {model.strip()}")
    return "  \u00b7  ".join(bits)


class DiscoveryResultsDialog(QDialog):
    """Shows discovery results and lets the user pick what to apply."""

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.setWindowTitle(DISCOVERY_DIALOG_TITLE)
        self.plan = list(plan or [])
        apply_device_dialog_style(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel("Devices that answered your search")
        heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(heading)

        selectable = [item for item in self.plan if selectable_match(item)]

        self.table = QTableWidget(len(self.plan), 4)
        self.table.setHorizontalHeaderLabels(["Add", "Name", "Address", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        for row, item in enumerate(self.plan):
            check = QTableWidgetItem()
            if selectable_match(item):
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                check.setCheckState(Qt.CheckState.Checked)
            else:
                check.setFlags(Qt.ItemFlag.NoItemFlags)
            self.table.setItem(row, 0, check)

            name = QTableWidgetItem(_display_name(item))
            if selectable_match(item):
                name.setFlags(
                    Qt.ItemFlag.ItemIsEditable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
            else:
                name.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, 1, name)

            details = _discovery_details(item)
            status = _discovery_status(item)
            address = QTableWidgetItem(str(item["discovered"].get("ip") or ""))
            address.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, 2, address)

            status_text = f"{status}  |  {details}" if details else status
            status_item = QTableWidgetItem(status_text)
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            # The full text stays readable even when the column is narrow.
            status_item.setToolTip(status_text)
            self.table.setItem(row, 3, status_item)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 220)
        self.table.setMinimumHeight(min(len(self.plan), 8) * 30 + 46)
        layout.addWidget(self.table)

        hint = QLabel(
            "Tick the devices to add. An entry whose address changed updates the "
            "existing device instead of creating a second one. Names can be edited here."
        )
        hint.setStyleSheet("color: #94A3B8; font-size: 12px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add Selected")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        if not selectable:
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setToolTip(
                DISCOVERY_ALL_KNOWN_TEXT
            )

        self.resize(860, 400)

    def selection(self):
        """``(selected_keys, names)`` for the rows the user accepted."""
        selected = []
        names = {}
        for row, item in enumerate(self.plan):
            if not selectable_match(item):
                continue
            check = self.table.item(row, 0)
            if check is None or check.checkState() != Qt.CheckState.Checked:
                continue
            key = item["key"]
            selected.append(key)
            name_item = self.table.item(row, 1)
            if name_item is not None:
                name = name_item.text().strip()
                if name:
                    names[key] = name
        return selected, names


class DeviceEditorDialog(QDialog):
    """Add one device by hand, or edit an existing one.

    Editing never touches the stable id: only the friendly name, the address and
    the enable flag can change.
    """

    def __init__(self, parent=None, device=None, devices=None):
        super().__init__(parent)
        self._device = dict(device) if isinstance(device, dict) else None
        self._devices = list(devices or [])
        self.is_edit = self._device is not None

        self.setWindowTitle("Edit Yeelight Device" if self.is_edit else "Add Yeelight Device")
        apply_device_dialog_style(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        grid = QGridLayout()
        grid.setSpacing(8)

        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("e.g. Desk Lamp")
        self.txt_ip = QLineEdit()
        self.txt_ip.setPlaceholderText("e.g. 192.168.1.50")
        self.chk_enabled = QCheckBox("Use this device in sleep/wake automation")
        self.chk_enabled.setChecked(True)

        grid.addWidget(QLabel("Name:"), 0, 0)
        grid.addWidget(self.txt_name, 0, 1)
        grid.addWidget(QLabel("IP address:"), 1, 0)
        grid.addWidget(self.txt_ip, 1, 1)
        grid.addWidget(self.chk_enabled, 2, 1)
        layout.addLayout(grid)

        hint = QLabel(
            "The name is only a label: renaming a device never changes its identity, so "
            "automation keeps working. LAN Control must be enabled in the Yeelight app."
        )
        hint.setStyleSheet("color: #94A3B8; font-size: 12px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        if self._device is not None:
            self.txt_name.setText(str(self._device.get("name") or ""))
            self.txt_ip.setText(str(self._device.get("ip") or ""))
            self.chk_enabled.setChecked(bool(self._device.get("enabled")))

        self.resize(460, 220)

    def values(self):
        return {
            "name": self.txt_name.text().strip(),
            "ip": self.txt_ip.text().strip(),
            "enabled": self.chk_enabled.isChecked(),
        }

    def problems(self):
        """Validation errors for the current input (friendly strings)."""
        values = self.values()
        if self.is_edit:
            candidate = dict(self._device)
            candidate["name"] = values["name"]
            candidate["ip"] = values["ip"]
            candidate["enabled"] = values["enabled"]
            problems = validate_device_entry(candidate)
        else:
            candidate = {
                "id": "manual:placeholder",
                "name": values["name"],
                "ip": values["ip"],
                "enabled": values["enabled"],
            }
            problems = validate_device_entry(candidate)

        duplicate = duplicate_ip_error(
            self._devices,
            values["ip"],
            ignore_device_id=self._device.get("id") if self._device else None,
        )
        if duplicate:
            problems.append(duplicate)
        return problems

    def accept(self):
        """Refuse to close while the entry would be invalid."""
        problems = self.problems()
        if problems:
            QMessageBox.warning(self, "Check the device", "\n\n".join(problems))
            return
        super().accept()


def discover_devices_interactively(parent, configured):
    """Discover, show the results, and return the updated device list.

    Returns ``None`` when the user cancelled or when nothing was found, so the
    caller can leave its device list untouched.
    """
    report = run_discovery(parent)
    logging.info("[DISCOVERY] Network search finished: %d device(s) answered.", len(report.devices))

    if not report.devices:
        text = DISCOVERY_EMPTY_TEXT
        if report.error:
            text += f"\n\n{report.error}"
        QMessageBox.information(parent, DISCOVERY_DIALOG_TITLE, text)
        return None

    plan = plan_discovery(report.devices, configured)
    selectable = [item for item in plan if selectable_match(item)]
    if not selectable:
        QMessageBox.information(parent, DISCOVERY_DIALOG_TITLE, DISCOVERY_ALL_KNOWN_TEXT)
        return None

    dialog = DiscoveryResultsDialog(plan, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None

    selected, names = dialog.selection()
    if not selected:
        return None

    try:
        updated = apply_discovery_plan(configured, plan, selected, names)
    except DeviceError as exc:
        QMessageBox.warning(parent, "Devices not updated", str(exc))
        return None

    added = sum(
        1
        for item in plan
        if item.get("state") == MATCH_NEW and item.get("key") in selected
    )
    updated_existing = len(selected) - added
    logging.info(
        "[DISCOVERY] Applied the selection: %d device(s) added, %d updated.",
        added,
        updated_existing,
    )
    return updated


class DeviceListWidget(QWidget):
    """The editable list of configured Yeelight devices."""

    changed = pyqtSignal()

    def __init__(self, parent=None, show_actions=True):
        super().__init__(parent)
        self._devices = []
        self._row_labels = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        if show_actions:
            actions = QHBoxLayout()
            self.btn_discover = QPushButton("Discover")
            self.btn_discover.setToolTip(
                "Search the local network for Yeelight devices (LAN Control must be enabled on them)."
            )
            self.btn_discover.clicked.connect(self.on_discover)
            self.btn_add = QPushButton("Add Manually")
            self.btn_add.setToolTip("Add a device by name and address.")
            self.btn_add.clicked.connect(self.on_add_manually)
            actions.addWidget(self.btn_discover)
            actions.addWidget(self.btn_add)
            actions.addStretch(1)
            layout.addLayout(actions)

        # The count is the first thing a user looks for on this page, so it sits
        # above the list; the (hidden) empty state stays below it.
        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("hint")
        layout.addWidget(self.lbl_summary)

        self._rows = QWidget()
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(SPACE_S)
        layout.addWidget(self._rows)

        self.lbl_empty = QLabel(
            "No Yeelight devices configured yet. Use Discover to find them on your "
            "local network, or Add Manually if you already know the address."
        )
        self.lbl_empty.setObjectName("hint")
        self.lbl_empty.setWordWrap(True)
        layout.addWidget(self.lbl_empty)

        self._rebuild()

    # --- state ----------------------------------------------------------
    def devices(self):
        """A copy of the current device list."""
        return [dict(entry) for entry in self._devices]

    def set_devices(self, devices):
        self._devices = [dict(entry) for entry in (devices or []) if isinstance(entry, dict)]
        self._rebuild()

    # --- actions --------------------------------------------------------
    def on_discover(self):
        updated = discover_devices_interactively(self, self.devices())
        if updated is not None:
            self.set_devices(updated)
            self.changed.emit()

    def on_add_manually(self):
        dialog = DeviceEditorDialog(self, devices=self.devices())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self._devices = add_device(
                self._devices, values["name"], values["ip"], values["enabled"]
            )
        except DeviceError as exc:
            QMessageBox.warning(self, "Device not added", str(exc))
            return
        self._rebuild()
        self.changed.emit()

    def on_edit(self, device_id):
        device = next((entry for entry in self._devices if entry.get("id") == device_id), None)
        if device is None:
            return
        dialog = DeviceEditorDialog(self, device=device, devices=self._devices)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self._devices = update_device(
                self._devices,
                device_id,
                name=values["name"],
                ip=values["ip"],
                enabled=values["enabled"],
            )
        except DeviceError as exc:
            QMessageBox.warning(self, "Device not updated", str(exc))
            return
        self._rebuild()
        self.changed.emit()

    def on_remove(self, device_id):
        device = next((entry for entry in self._devices if entry.get("id") == device_id), None)
        if device is None:
            return
        name = str(device.get("name") or "this device")
        answer = QMessageBox.question(
            self,
            "Remove Yeelight device",
            f"Remove '{name}' from the device list?\n\n"
            "Sleep/wake automation stops controlling this device. Its automation settings "
            "and the other devices are not affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._devices = remove_device(self._devices, device_id)
        self._rebuild()
        self.changed.emit()

    def on_toggle(self, device_id, enabled):
        for entry in self._devices:
            if entry.get("id") == device_id:
                entry["enabled"] = bool(enabled)
                break
        self._style_row(device_id)
        self._update_summary()
        self.changed.emit()

    # --- rendering ------------------------------------------------------
    def _clear_rows(self):
        self._row_labels = {}
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild(self):
        self._clear_rows()
        for device in self._devices:
            self._rows_layout.addWidget(self._build_row(device))
        self._update_summary()

    def _build_row(self, device):
        device_id = str(device.get("id") or "")
        row = QFrame()
        row.setObjectName("device_row")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(SPACE_M, SPACE_S, SPACE_M, SPACE_S)
        layout.setSpacing(SPACE_M)

        check = QCheckBox()
        check.setChecked(bool(device.get("enabled")))
        check.setToolTip("Include this device in sleep/wake automation")
        check.toggled.connect(lambda checked, target=device_id: self.on_toggle(target, checked))

        name = QLabel(str(device.get("name") or ""))
        detail = QLabel(_device_detail(device))

        btn_edit = QPushButton("Edit")
        btn_edit.clicked.connect(lambda _checked=False, target=device_id: self.on_edit(target))
        btn_remove = QPushButton("Remove")
        btn_remove.clicked.connect(lambda _checked=False, target=device_id: self.on_remove(target))

        layout.addWidget(check)
        layout.addWidget(name)
        layout.addWidget(detail)
        layout.addStretch(1)
        layout.addWidget(btn_edit)
        layout.addWidget(btn_remove)

        self._row_labels[device_id] = (name, detail)
        self._style_row(device_id)
        return row

    def _style_row(self, device_id):
        """Dim a disabled device's labels; the checkbox stays operable."""
        labels = self._row_labels.get(device_id)
        if not labels:
            return
        name, detail = labels
        device = next((entry for entry in self._devices if entry.get("id") == device_id), None)
        enabled = bool(device and device.get("enabled"))
        name.setStyleSheet(
            f"font-weight: 600; color: {TEXT};" if enabled
            else f"font-weight: 600; color: {TEXT_DIM};"
        )
        detail.setStyleSheet(f"color: {TEXT_MUTED};" if enabled else f"color: {TEXT_DIM};")

    def _update_summary(self):
        self.lbl_empty.setVisible(not self._devices)
        self.lbl_summary.setText(f"Yeelight devices: {device_summary(self._devices)}")
