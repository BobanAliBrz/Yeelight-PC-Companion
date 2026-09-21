import sys
import os
import subprocess
import threading
import time
import logging
import ctypes
import ctypes.wintypes
import ipaddress
import logging.handlers
import select
import socket
import struct
import copy
from datetime import datetime, timezone, timedelta

# PyQt6 Imports
from PyQt6.QtCore import (
    QAbstractNativeEventFilter, QCoreApplication, QByteArray,
    QThread, pyqtSignal, QObject, QTimer, Qt
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSystemTrayIcon, QMenu, QTextEdit,
    QLineEdit, QCheckBox, QStackedWidget, QMessageBox, QGridLayout,
    QFrame, QFileDialog
)
from PyQt6.QtGui import QIcon, QAction, QTextCursor

# Presentation layer (Stage 5 redesign). `ui_theme` owns the palette,
# typography, spacing and stylesheets; `ui_components` provides the small set
# of reusable presentational widgets. Neither module contains configuration,
# power, process or network logic.
from ui_theme import (
    APP_QSS,
    SPACE_L,
    SPACE_M,
    SPACE_S,
    SPACE_XL,
    SPACE_XS,
    TONE_ACCENT,
    TONE_NEUTRAL,
    TONE_OK,
    TONE_WARN,
    apply_app_theme,
    text_qss,
)
from ui_components import (
    IntegrationCard,
    SectionCard,
    SidebarButton,
    StatusPill,
    StatusRow,
    field_label,
    hint_label,
    muted_label,
    page_body,
    scrollable,
)

# Configuration layer (single source of truth for defaults, validation,
# migration, storage locations, import/export and atomic writes).
from config_manager import (
    CONFIG_VERSION,
    CRASH_LOG_FILENAME,
    DEBUG_LOG_FILENAME,
    INTEGRATION_KEYS,
    INTEGRATIONS,
    ConfigError,
    ConfigManager,
    ConfigValidationError,
    configured_devices,
    device_count_summary,
    enabled_device_ips,
    get_app_dir,
    integration_enabled,
    integration_path,
    storage_mode,
    validate_config,
)
from first_run_wizard import run_first_run_wizard

# Yeelight device management: the editable device list and the user-triggered
# LAN discovery, both shared with the first-run wizard. Discovery runs in its
# own worker thread and is never started automatically.
from yeelight_device_ui import DeviceListWidget

# Windows Scheduled Task elevation broker for OpenRGB. OpenRGB needs
# administrator rights on some systems; the dedicated scheduled task lets wake
# restoration start it silently, without a UAC prompt. See project_memory.md
# ("OpenRGB elevation model").
from windows_tasks import (
    ACTION_NONE,
    ACTION_PROVISION,
    ACTION_REMOVE,
    END_TASK_STOP_BUDGET_SECONDS,
    OPENRGB_TASK_ARGS,
    OPENRGB_TASK_NAME,
    REPAIR_ACTION_LABEL,
    SET_UP_ACTION_LABEL,
    STATUS_DISABLED,
    STATUS_UNSAFE_TARGET,
    WindowsTaskError,
    apply_openrgb_task_action,
    end_openrgb_task,
    is_process_elevated,
    openrgb_elevation_status,
    openrgb_process_running,
    openrgb_task_action,
    run_openrgb_task,
    run_provisioning_cli,
)

# ---------------------------------------------------------
# Windows Native Power/Shutdown Event Constants
# ---------------------------------------------------------
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
APP_USER_MODEL_ID = "YeelightPCCompanion.App"

def set_windows_app_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

# ---------------------------------------------------------
# Windows Native Toolhelp32 API (Process Listing)
# ---------------------------------------------------------
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.wintypes.ULONG)),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260)
    ]

def get_running_processes_win32():
    """
    Query all running process names natively using the Windows Toolhelp32 API.
    This is extremely fast (<8ms), requires zero process handles to be opened (AccessDenied-safe),
    and is 100% leak-free.
    """
    running_processes = set()
    hSnapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if hSnapshot == -1 or hSnapshot == 0:
        return running_processes
        
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        
        retval = ctypes.windll.kernel32.Process32First(hSnapshot, ctypes.byref(pe))
        while retval:
            try:
                exe_name = pe.szExeFile.decode('utf-8', errors='ignore').strip()
                if exe_name:
                    running_processes.add(exe_name.lower())
            except Exception:
                pass
            retval = ctypes.windll.kernel32.Process32Next(hSnapshot, ctypes.byref(pe))
    except Exception:
        pass
    finally:
        ctypes.windll.kernel32.CloseHandle(hSnapshot)
        
    return running_processes

def terminate_processes_win32(*names):
    """
    Terminate matching processes directly through Win32.
    This avoids spawning taskkill during suspend, where every extra second matters.
    """
    target_names = {name.lower() for name in names if name}
    if not target_names:
        return 0

    killed = 0
    hSnapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if hSnapshot == -1 or hSnapshot == 0:
        return killed

    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)

        retval = ctypes.windll.kernel32.Process32First(hSnapshot, ctypes.byref(pe))
        while retval:
            try:
                exe_name = pe.szExeFile.decode('utf-8', errors='ignore').strip().lower()
                if exe_name in target_names:
                    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pe.th32ProcessID)
                    if handle:
                        try:
                            if ctypes.windll.kernel32.TerminateProcess(handle, 0):
                                killed += 1
                        finally:
                            ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
            retval = ctypes.windll.kernel32.Process32Next(hSnapshot, ctypes.byref(pe))
    except Exception:
        pass
    finally:
        ctypes.windll.kernel32.CloseHandle(hSnapshot)

    return killed

# ---------------------------------------------------------
# Win32 Direct Power Callback (Primary Sleep/Wake Detector)
# ---------------------------------------------------------
# Uses PowerRegisterSuspendResumeNotification from PowrProf.dll.
# This is a direct OS callback — no message loop dependency,
# no risk of PyQt GC, and the OS explicitly waits for it to return.
DEVICE_NOTIFY_CALLBACK = 2

# Callback type: ULONG CALLBACK(PVOID Context, ULONG Type, PVOID Setting)
POWER_CALLBACK_FUNC = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)

class DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS(ctypes.Structure):
    _fields_ = [
        ("Callback", POWER_CALLBACK_FUNC),
        ("Context", ctypes.c_void_p),
    ]

class Win32PowerCallback(QObject):
    """
    Direct OS-level power callback using PowerRegisterSuspendResumeNotification.
    This is invoked by the Windows kernel directly on a system thread pool thread,
    bypassing Qt's message loop entirely. Much more reliable than WM_POWERBROADCAST.

    For SUSPEND: Executes the suspend_action callable directly on the callback thread
                 (synchronous — the OS waits for it to return before sleeping).
    For RESUME:  Emits a Qt signal that is queued to the GUI thread.
    """
    resume_detected = pyqtSignal()

    def __init__(self, suspend_action=None):
        super().__init__()
        self._suspend_action = suspend_action  # callable, invoked directly on callback thread
        self._registration_handle = ctypes.c_void_p()
        # Store callback references as instance attributes to prevent GC
        self._callback_func = POWER_CALLBACK_FUNC(self._on_power_event)
        self._params = DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS()
        self._params.Callback = self._callback_func
        self._params.Context = None
        self._registered = False

    def register(self):
        """Register for direct OS power suspend/resume callbacks."""
        try:
            powrprof = ctypes.windll.LoadLibrary("PowrProf.dll")
            result = powrprof.PowerRegisterSuspendResumeNotification(
                DEVICE_NOTIFY_CALLBACK,
                ctypes.byref(self._params),
                ctypes.byref(self._registration_handle)
            )
            if result == 0:  # ERROR_SUCCESS
                self._registered = True
                logging.info("[POWER CALLBACK] Successfully registered PowerRegisterSuspendResumeNotification (primary detector)")
            else:
                logging.error(f"[POWER CALLBACK] PowerRegisterSuspendResumeNotification failed with error code {result}")
        except Exception as e:
            logging.error(f"[POWER CALLBACK] Failed to register: {e}")

    def is_registered(self):
        return self._registered

    def _on_power_event(self, context, event_type, setting):
        """Called directly by the Windows kernel on a system thread pool thread."""
        try:
            if event_type == PBT_APMSUSPEND:
                logging.info("[POWER CALLBACK] >>> SUSPEND detected via direct OS callback <<<")
                if self._suspend_action:
                    try:
                        self._suspend_action()
                    except Exception as e:
                        logging.error(f"[POWER CALLBACK] Suspend action error: {e}")
            elif event_type in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                logging.info(f"[POWER CALLBACK] >>> RESUME detected via direct OS callback (type={event_type}) <<<")
                self.resume_detected.emit()
        except Exception as e:
            logging.error(f"[POWER CALLBACK] Error in power event handler: {e}")
        return 0

    def unregister(self):
        if self._registered and self._registration_handle:
            try:
                powrprof = ctypes.windll.LoadLibrary("PowrProf.dll")
                powrprof.PowerUnregisterSuspendResumeNotification(self._registration_handle)
                self._registered = False
                logging.info("[POWER CALLBACK] Unregistered power callback")
            except Exception:
                pass

# ---------------------------------------------------------
# Presentation
# ---------------------------------------------------------
# The palette, typography, spacing and every stylesheet now live in
# `ui_theme.py`, and the reusable presentational widgets live in
# `ui_components.py`. `APP_QSS` is the application stylesheet and is the same
# one the first-run wizard is styled with.

# ---------------------------------------------------------
# Custom Logging Handler to emit QSignals
# ---------------------------------------------------------
class QtLogHandler(logging.Handler, QObject):
    log_signal = pyqtSignal(str, int)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%H:%M:%S'))

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg, record.levelno)

# ---------------------------------------------------------
# State and Solar Calculation Engine (Background Thread)
# ---------------------------------------------------------
class SolarEngineThread(QThread):
    solar_update = pyqtSignal(bool, str) # (is_dark, status_text)
    api_error = pyqtSignal(str)

    def __init__(self, config_manager):
        super().__init__()
        self.config_manager = config_manager
        self.running = True
        self.last_is_dark = None

    def run(self):
        logging.info("Solar Engine thread started.")
        while self.running:
            try:
                # Reload configuration through the ConfigManager (single source of
                # truth for defaults/validation/migration).
                config = self.config_manager.load()

                lat = config["location"]["latitude"]
                lon = config["location"]["longitude"]
                elev = config["location"]["elevation"]
                buffer_hours = config["location"]["light_buffer_hours"]
                
                import ephem
                obs = ephem.Observer()
                obs.lat = str(lat)
                obs.lon = str(lon)
                obs.elevation = float(elev)
                
                # ephem dates are UTC based
                obs.date = datetime.now(timezone.utc)
                now = obs.date.datetime()
                
                next_sunrise = obs.next_rising(ephem.Sun()).datetime()
                next_sunset = obs.next_setting(ephem.Sun()).datetime()
                
                buffer_delta = timedelta(hours=float(buffer_hours))
                
                # Check dark state
                if next_sunrise < next_sunset:
                    is_dark = True
                else:
                    prev_sunrise = obs.previous_rising(ephem.Sun()).datetime()
                    time_to_sunset = next_sunset - now
                    time_since_sunrise = now - prev_sunrise
                    is_dark = (time_to_sunset <= buffer_delta) or (time_since_sunrise <= buffer_delta)
                
                status_text = "Nighttime" if is_dark else "Daytime"

                # Emit update
                self.solar_update.emit(is_dark, status_text)

                # Update Artemis (skipped entirely when the integration is disabled)
                if integration_enabled(config, "artemis"):
                    self.update_artemis(is_dark, status_text)
                else:
                    logging.debug("Artemis integration is disabled; solar state not published to Artemis.")

                self.last_is_dark = is_dark
                
            except Exception as e:
                logging.error(f"Error in Solar Engine: {e}")
                
            # Wait 60 seconds
            for _ in range(60):
                if not self.running:
                    break
                self.msleep(1000)

    def update_artemis(self, is_dark, status_text):
        schema_url = "http://localhost:9696/json-modules/SunsetInfo/schema"
        data_url = "http://localhost:9696/json-modules/SunsetInfo/data"
        
        schema = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "title": "SunsetInfo",
            "type": "object",
            "properties": {
                "isDark": { "type": "boolean" },
                "statusText": { "type": "string" }
            }
        }
        
        import requests
        try:
            # Try posting schema (Artemis only needs it once, but safe to post)
            requests.post(schema_url, json=schema, timeout=1.5)
            # Post actual state
            data = {"isDark": is_dark, "statusText": status_text}
            resp = requests.post(data_url, json=data, timeout=1.5)
            if resp.status_code not in [200, 201, 204]:
                self.api_error.emit(f"Artemis API rejected data: HTTP {resp.status_code}")
        except requests.exceptions.RequestException:
            # Artemis is likely not running, which is normal when we boot/suspend
            pass

    def stop(self):
        self.running = False
        self.wait()

# ---------------------------------------------------------
# External process spawning — frozen-build DLL search hygiene
# ---------------------------------------------------------
# A PyInstaller build adds its own bundle directory (and the Qt/pywin32
# sub-directories it loads from) to the launcher's PATH, and PyInstaller's
# runtime hooks make the bundle directory the process-wide DLL search
# directory. Both are inherited by any GUI application we start, so an
# installed third-party program that resolves its own runtime DLLs by name can
# end up loading *our* copies — a running Yeelight Chroma Connector loaded
# ``dist\YeelightPCCompanion\_internal\VCRUNTIME140.dll`` that way and kept the
# build output locked, which made the next PyInstaller COLLECT fail with
# ``PermissionError: [WinError 5]``.
#
# Two narrow measures fix this for every external program we launch, without
# touching the bundles or the application's own environment:
#
# * ``SetDllDirectoryW(NULL)`` around the spawn, restored in ``finally`` (the
#   documented PyInstaller guidance). The previous value is read back with
#   ``GetDllDirectoryW`` instead of assuming ``sys._MEIPASS``.
# * the child's PATH with the bundle directories removed.
#
# ``SetDllDirectoryW`` is process-global, so the whole reset/spawn/restore
# section is serialised by one lock.
_LAUNCH_LOCK = threading.RLock()


def _frozen_bundle_dir():
    """The PyInstaller bundle directory, or None when running from source."""
    return os.path.normcase(os.path.abspath(sys._MEIPASS)) if getattr(sys, "_MEIPASS", None) else None


def _normalised_separators(path):
    """Return *path* with every separator expressed as the local one."""
    text = os.fspath(path)
    for separator in ("\\", "/"):
        if separator != os.sep:
            text = text.replace(separator, os.sep)
    return text


def _is_inside(path, directory):
    """True when *path* is *directory* itself or located underneath it.

    Component-wise comparison, never a substring test: a sibling directory
    whose name merely starts with the bundle name must not match. Both
    separator styles are understood, so a `PATH` entry containing forward
    slashes is classified the same way as one using backslashes.
    """
    try:
        candidate = os.path.normcase(os.path.abspath(_normalised_separators(path)))
        parent = os.path.normcase(os.path.abspath(_normalised_separators(directory)))
    except (TypeError, ValueError):
        return False
    if not candidate or not parent:
        return False
    try:
        return os.path.commonpath([candidate, parent]) == parent
    except ValueError:
        # Different drives (or a mix of absolute and relative): not inside.
        return False


def external_process_environment(base=None):
    """A copy of the environment safe to hand to an installed external program.

    ``os.environ`` itself is never mutated. Only ``PATH`` is touched, and only
    to drop entries belonging to the frozen bundle (the bundle directory and
    the descendants PyInstaller's runtime hooks add); every unrelated entry is
    preserved, including entries that merely look similar. Running from source
    changes nothing and the original mapping is returned unchanged.
    """
    environment = os.environ.copy() if base is None else dict(base)
    bundle = _frozen_bundle_dir()
    if bundle is None:
        return environment
    path = environment.get("PATH")
    if not path:
        return environment
    kept = [entry for entry in path.split(os.pathsep) if not _is_inside(entry, bundle)]
    environment["PATH"] = os.pathsep.join(kept)
    return environment


def _set_dll_directory(directory):
    """Set the process DLL search directory (None restores the default)."""
    try:
        return ctypes.windll.kernel32.SetDllDirectoryW(directory)
    except Exception:
        return False


def _get_dll_directory():
    """The current process DLL search directory, or None when unset."""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetDllDirectoryW(len(buffer), buffer)
        if length <= 0:
            return None
        return buffer.value
    except Exception:
        return None


class external_process_environment_scope:
    """Clear this process's injected DLL directory for one spawn, then restore.

    Used as a context manager around ``subprocess.Popen`` of an installed
    external application. Nested use is safe (the lock is reentrant) and the
    previous directory is restored on every path, including when the spawn
    raises.
    """

    def __enter__(self):
        _LAUNCH_LOCK.acquire()
        self._previous = _get_dll_directory()
        _set_dll_directory(None)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            _set_dll_directory(self._previous)
        finally:
            _LAUNCH_LOCK.release()
        return False


# ---------------------------------------------------------
# Suspend-time Yeelight OFF fan-out
# ---------------------------------------------------------
# The suspend sequence must finish well inside Windows' ~2 s freeze deadline
# (see project_memory.md, "Suspend timing contract"). Sending the OFF command to
# the configured devices therefore may not cost one socket timeout per device:
# with an arbitrary number of configured devices an `O(N x timeout)` loop tears
# straight through the budget (8 unreachable devices would already spend ~2 s
# before the Connector wait and the process termination are even considered).
# Every address is attempted concurrently instead — non-blocking sockets, one
# `select()` fan-out, one global deadline for the whole batch.
SUSPEND_YEELIGHT_PORT = 55443
SUSPEND_YEELIGHT_OFF_PAYLOAD = b'{"id":1,"method":"set_power","params":["off","sudden",0]}\r\n'

# The entire networking phase of the suspend sequence — every TCP connect and
# every send, for every device together — gets this one budget. It is
# deliberately small: `kill Connector -> 0.35 s music-mode release wait -> this
# -> kill OpenRGB/Artemis` has to stay comfortably below the ~1.5 s target, so
# the budget is a fraction of the target rather than a per-device timeout.
SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS = 0.35

# How long the Connector is given to release Yeelight music mode before the OFF
# fan-out. Measured behaviour, unchanged by any later stage.
SUSPEND_CONNECTOR_RELEASE_SECONDS = 0.35

# Budgeted worst case of the native same-integrity controller terminate
# (`terminate_processes_win32`), which is a Toolhelp32 enumeration plus
# `OpenProcess`/`TerminateProcess` calls — measured in the tens of milliseconds
# and bounded here so the sequence total below stays honest. It is the one phase
# that is deliberately a little below its possible worst case, which is what
# leaves the sequence its headroom against the target.
SUSPEND_NATIVE_TERMINATE_SECONDS = 0.35

#: The hard target for the whole `_execute_suspend_actions()` sequence.
SUSPEND_SEQUENCE_TARGET_SECONDS = 1.5


#: The phases that are *budgeted* (rather than measured-and-small) inside
#: `_execute_suspend_actions()`. The OpenRGB task-aware stop is one of them and
#: must stay globally bounded: it used to be a 30 s query plus a 5 s `/end` plus a
#: 2 s poll, which is why this exists as an explicit, testable contract.
def suspend_budgeted_seconds():
    """Sum of the suspend phases that carry an explicit worst-case budget."""
    return (
        SUSPEND_CONNECTOR_RELEASE_SECONDS
        + SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS
        + END_TASK_STOP_BUDGET_SECONDS
        + SUSPEND_NATIVE_TERMINATE_SECONDS
    )


# Windows' `select()` accepts only a limited number of sockets per call. Fanning
# out in chunks keeps an absurdly long device list from raising instead of
# connecting; because every chunk shares the one deadline above, more devices can
# only ever cost *less* time per socket, never more total time.
SUSPEND_YEELIGHT_SELECT_CHUNK = 256

# Per-device outcomes reported by `fire_and_forget_off_devices()`.
SUSPEND_FANOUT_SENT = "sent"
SUSPEND_FANOUT_TIMED_OUT = "timed_out"
SUSPEND_FANOUT_FAILED = "failed"

# Internal per-socket states of the fan-out.
SUSPEND_FANOUT_CONNECTING = "connecting"
SUSPEND_FANOUT_SENDING = "sending"


def _suspend_off_targets(device_ips):
    """The fan-out address list: trimmed, de-duplicated **IP literals**.

    Each entry is returned as ``(address, address_family)`` so the family comes
    from the literal itself. Nothing on this path resolves a name: no DNS query,
    no ``getaddrinfo()``, no lookup that could outlive the budget. Configuration
    validation only ever stores IP literals, so an entry that is not one cannot
    be a configured Yeelight address and is skipped instead of being handed to
    the resolver.
    """
    targets = []
    seen = set()
    ignored = []
    for address in device_ips or []:
        text = str(address or "").strip()
        try:
            parsed = ipaddress.ip_address(text)
        except ValueError:
            if text:
                ignored.append(text)
            continue
        # Deduplicate on the canonical form, so two spellings of one address are
        # one device (the same rule the configuration layer uses).
        canonical = str(parsed)
        if canonical in seen:
            continue
        seen.add(canonical)
        targets.append((canonical, socket.AF_INET if parsed.version == 4 else socket.AF_INET6))
    if ignored:
        logging.warning(
            "[SUSPEND] Ignoring device address(es) that are not IP literals: %s",
            ", ".join(ignored),
        )
    return targets


def _suspend_connect_socket(address, family):
    """A socket whose non-blocking ``connect()`` has already been started.

    ``connect_ex()`` returns a platform error code instead of raising, which is
    exactly what a non-blocking connect needs: a non-zero result means "in
    progress" (or already failed) and the real outcome is read from ``SO_ERROR``
    once ``select()`` reports the socket writable. Returns None when the socket
    could not even be created; every socket this function returns is closed by
    the caller.
    """
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        sock.setblocking(False)
        sock.connect_ex((address, SUSPEND_YEELIGHT_PORT))
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        return None
    return sock


def _suspend_close(sock, connections):
    """Close one socket and forget everything tracked about it."""
    connections.pop(sock, None)
    try:
        sock.close()
    except Exception:
        pass


def _suspend_chunks(sockets):
    """Split the sockets into what a single ``select()`` call can take."""
    return [
        sockets[start:start + SUSPEND_YEELIGHT_SELECT_CHUNK]
        for start in range(0, len(sockets), SUSPEND_YEELIGHT_SELECT_CHUNK)
    ]


def _suspend_advance(sock, connections, outcomes):
    """Advance one socket ``select()`` reported writable.

    Either its connection attempt now has an outcome, or it is connected and can
    take payload bytes. Returns True when anything about the socket changed, so
    the caller knows another wait is worthwhile.
    """
    connection = connections[sock]
    address = connection["address"]
    changed = False
    if connection["state"] == SUSPEND_FANOUT_CONNECTING:
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
            outcomes[address] = SUSPEND_FANOUT_FAILED
            _suspend_close(sock, connections)
            return True
        connection["state"] = SUSPEND_FANOUT_SENDING
        changed = True
    try:
        sent = sock.send(connection["unsent"])
    except (BlockingIOError, InterruptedError):
        return changed
    except OSError:
        outcomes[address] = SUSPEND_FANOUT_FAILED
        _suspend_close(sock, connections)
        return True
    if sent <= 0:
        return changed
    if sent < len(connection["unsent"]):
        connection["unsent"] = connection["unsent"][sent:]
        return True
    outcomes[address] = SUSPEND_FANOUT_SENT
    _suspend_close(sock, connections)
    return True


def _log_suspend_fanout(outcomes, elapsed, budget):
    """Report the whole batch in at most two log records — never one per device.

    Logging must not add delays of its own to a path that is measured in
    fractions of a second, so the per-device detail is summarised instead of
    emitted device by device.
    """
    sent = [address for address, outcome in outcomes.items() if outcome == SUSPEND_FANOUT_SENT]
    missed = sorted(
        (address, outcome) for address, outcome in outcomes.items() if outcome != SUSPEND_FANOUT_SENT
    )
    logging.info(
        "[SUSPEND] OFF fan-out: %d/%d addressed in %.3fs (sent: %s)",
        len(sent),
        len(outcomes),
        elapsed,
        ", ".join(sent) if sent else "-",
    )
    if missed:
        logging.warning(
            "[SUSPEND] OFF not delivered within the %.2fs network budget: %s",
            budget,
            ", ".join(f"{address} ({outcome})" for address, outcome in missed),
        )


def fire_and_forget_off_devices(device_ips, budget=SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS):
    """Send the raw ``set_power off sudden`` command to every address at once.

    This is the suspend-time batch sender. It deliberately does none of the
    things a normal Yeelight operation does: **no discovery, no
    ``yeelight.Bulb``, no DNS** (the address family comes from the validated IP
    literal itself), **no retries, no response verification, no filesystem
    work**. Every device stays independently best-effort — one dead bulb can
    neither delay nor break the others.

    The whole batch runs under **one** global deadline, so the networking phase
    does not grow with the number of devices: 1 device and 20 unreachable devices
    both finish inside the same small budget instead of waiting a socket timeout
    each. A bulb needs no reply: once connected, the payload is sent and the
    socket is closed.

    Returns ``{address: outcome}`` (one of `SUSPEND_FANOUT_SENT`,
    `SUSPEND_FANOUT_TIMED_OUT`, `SUSPEND_FANOUT_FAILED`). Never raises — the
    suspend sequence must not be interrupted by a socket failure — and every
    socket it opens is closed on every path.
    """
    outcomes = {}
    targets = _suspend_off_targets(device_ips)
    if not targets:
        # Zero devices is a fully supported state: nothing is opened at all.
        return outcomes

    try:
        seconds = max(0.0, float(budget))
    except (TypeError, ValueError):
        seconds = SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS

    started = time.monotonic()
    deadline = started + seconds
    connections = {}  # socket -> {"address", "state", "unsent"}
    try:
        for address, family in targets:
            sock = _suspend_connect_socket(address, family)
            if sock is None:
                outcomes[address] = SUSPEND_FANOUT_FAILED
                continue
            connections[sock] = {
                "address": address,
                "state": SUSPEND_FANOUT_CONNECTING,
                "unsent": SUSPEND_YEELIGHT_OFF_PAYLOAD,
            }

        while connections:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunks = _suspend_chunks(list(connections))
            progressed = False
            for position, chunk in enumerate(chunks):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Every chunk gets an equal share of what is left of the single
                # budget, so a very long device list degrades to less time per
                # socket instead of a longer total wait.
                share = remaining / (len(chunks) - position)
                try:
                    _, writable, _ = select.select([], chunk, [], share)
                except (OSError, ValueError):
                    # The platform refused the wait (or it was interrupted):
                    # stop here, every remaining socket is still closed below.
                    break
                for sock in writable:
                    if _suspend_advance(sock, connections, outcomes):
                        progressed = True
            if not progressed:
                break
    except Exception:  # pragma: no cover - defensive: suspend must never raise
        logging.exception("[SUSPEND] The Yeelight OFF fan-out failed unexpectedly.")
    finally:
        # Anything still open never got its command in time: report it as a
        # timeout and close it. Every socket is closed on every path.
        for sock, connection in list(connections.items()):
            outcomes.setdefault(connection["address"], SUSPEND_FANOUT_TIMED_OUT)
            _suspend_close(sock, connections)

    _log_suspend_fanout(outcomes, time.monotonic() - started, seconds)
    return outcomes


# ---------------------------------------------------------
# OpenRGB SDK readiness gate (restore sequence)
# ---------------------------------------------------------
# Starting OpenRGB is not the same thing as OpenRGB being usable by another SDK
# client. OpenRGB binds its SDK server ~31 ms into its startup and then spends
# nearly all of the rest of it enumerating controllers, so a consumer that
# connects during that window sees an empty or incomplete device list and only
# recovers by being restarted. Measured cold start, maintainer's PC, OpenRGB
# v1.0 with 4 controllers, taken from OpenRGB's own log:
#
#       31 ms  the SDK server accepts connections
#     1 136 ms  controller #1 registered
#    11 684 ms  controllers #2-#4 registered (11 684 / 11 755 / 11 883)
#    12 337 ms  "Detection completed"
#
# "The controller count has not changed for a while" is therefore **not** a
# valid readiness signal: the count above sits unchanged at one for 10.5 of
# those 12.3 seconds, so a short stability rule declares readiness while three
# of the four controllers do not exist yet. Neither is any fixed delay. The only
# honest signal is the one OpenRGB emits for it, and SDK protocol 6 defines it:
#
#   NET_PACKET_ID_DETECTION_STARTED            101
#   NET_PACKET_ID_DETECTION_PROGRESS_CHANGED   102
#   NET_PACKET_ID_DETECTION_COMPLETE           103
#
# Verified in the source of the installed build (the `release_1.0` tag, commit
# 81bbe18a - the commit the installed `OpenRGB.exe` names in its own log):
#
#   NetworkProtocol.h:28         OPENRGB_SDK_PROTOCOL_VERSION is 6, and the
#   NetworkProtocol.h:125-127    three detection events were introduced with it
#   NetworkServer.cpp:873        SignalDetectionCompleted() calls
#                                SendRequest_DetectionCompleted() for *every*
#                                entry of ServerClients
#   NetworkServer.cpp:3945       ...which sends 103 only to clients whose
#                                client_protocol_version >= 6. No client flag
#                                and no subscription is involved (the
#                                ProfileManager, by contrast, requires one).
#   NetworkServer.cpp:1668       NET_PACKET_ID_REQUEST_PROTOCOL_VERSION (40)
#                                stores min(requested, server max) on the
#                                connection
#   NetworkServer.cpp:3704       ...and its reply carries the *server's* max
#   DetectionManager.cpp:867     detection's last act is to signal
#                                DETECTIONMANAGER_UPDATE_REASON_DETECTION_COMPLETE
#   Documentation/OpenRGBSDK.md  "103 ... Indicate to clients that detection
#                                completed."
#
# Two consequences shape this gate:
#
#   1. A client that is connected when detection completes receives 103 no
#      matter how early during detection it connected: the server broadcasts
#      the event to the client list as it exists at that moment.
#   2. Protocol 6 cannot be *asked* for the current detection state - there is
#      no such packet in the protocol's table - so a client that connects after
#      103 was sent never learns on that connection that detection finished.
#
# The gate is therefore explicit about the two situations it can be in, which is
# the only honest option:
#
#   * OpenRGB started by this restore (`launched=True`) -> detection is still
#     ahead of us, so readiness is *only* DETECTION_COMPLETE. A server too old
#     to have the events (protocol < 6) cannot confirm completion at all: the
#     budget runs out and the restore continues with a warning.
#
#   * OpenRGB found already running (`launched=False`) -> detection may have
#     finished before we connected. 103 is still awaited, but a positive
#     controller count that does not change and no detection event for
#     OPENRGB_READINESS_ALREADY_RUNNING_SECONDS is accepted instead. A process
#     that predates the restore has almost certainly settled, and the quiet
#     window is what separates the two possibilities: it is longer than the
#     widest silence between two detection events in the measured cold start
#     (10.6 s, the HID enumeration stage). Without protocol 6 there is no
#     evidence to weigh at all, so this path cannot accept either.
#
# Everything here is read-only. The only requests this client ever sends are a
# protocol-version request and controller-count requests; the detection events
# arrive because the server pushes them. No RGB data, no rescan, no profile or
# configuration command, one OpenRGB process, no elevation.
#
#   request:  4s "ORGB" | 4s pkt_dev_id=0 | 4s pkt_id | 4s pkt_size | payload
#   reply:    4s "ORGB" | 4s pkt_dev_id   | 4s pkt_id | 4s pkt_size | payload
OPENRGB_SDK_HOST = "127.0.0.1"
OPENRGB_SDK_PORT = 6742
OPENRGB_SDK_MAGIC = b"ORGB"
OPENRGB_SDK_HEADER_SIZE = 16
# OpenRGB's own limit for one packet body (NetworkProtocol.h:
# OPENRGB_SDK_MAX_PACKET_SIZE). A larger packet cannot come from a working
# server, so the stream is treated as broken instead of allocating for it.
# Packets this client did not ask for are still consumed in full, so a large
# push (an RGBController update, for example) cannot desynchronise the stream.
OPENRGB_SDK_MAX_PAYLOAD_SIZE = 8 * 1024 * 1024

OPENRGB_SDK_PACKET_REQUEST_CONTROLLER_COUNT = 0
OPENRGB_SDK_PACKET_ACK = 10
OPENRGB_SDK_PACKET_REQUEST_PROTOCOL_VERSION = 40
OPENRGB_SDK_PACKET_SET_SERVER_NAME = 51
OPENRGB_SDK_PACKET_DEVICE_LIST_UPDATED = 100
OPENRGB_SDK_PACKET_DETECTION_STARTED = 101
OPENRGB_SDK_PACKET_DETECTION_PROGRESS_CHANGED = 102
OPENRGB_SDK_PACKET_DETECTION_COMPLETE = 103

# The highest SDK protocol this client speaks, and the lowest one that has the
# detection events above. Negotiation is what turns "detection events are
# available" from an assumption into a fact.
OPENRGB_SDK_CLIENT_PROTOCOL_VERSION = 6
OPENRGB_SDK_DETECTION_PROTOCOL_VERSION = 6

# Connect and read timeout of one SDK conversation.
OPENRGB_SDK_PROBE_TIMEOUT_SECONDS = 0.6
# How long a protocol-version request may take to be answered before the server
# is treated as protocol 0 (OpenRGB's own client waits one second as well).
OPENRGB_SDK_NEGOTIATION_TIMEOUT_SECONDS = 1.0

# Readiness gate. The timeout bounds the whole gate (~2x the measured cold
# detection time) and is never fatal: the restore continues without it.
OPENRGB_READINESS_TIMEOUT_SECONDS = 25.0
OPENRGB_READINESS_POLL_SECONDS = 0.5
OPENRGB_READINESS_SETTLE_SECONDS = 1.5
# See the block comment above: longer than the widest silence between two
# detection events in the measured cold start (10.6 s, the HID stage).
OPENRGB_READINESS_ALREADY_RUNNING_SECONDS = 12.0
# A detection progress event is logged when the percentage changes, or when this
# many seconds have passed since the last progress line - whichever comes first.
# OpenRGB emits one event per detector it walks through, so logging every one of
# them wrote 991 lines into a single real restore log (measured, 2026-09-19);
# this turns the progress line into a bounded heartbeat instead.
OPENRGB_READINESS_PROGRESS_LOG_SECONDS = 2.0

OPENRGB_READINESS_DETECTING = "detecting"
OPENRGB_READINESS_PROGRESS = "progress"
OPENRGB_READINESS_DETECTED = "detected"
OPENRGB_READINESS_COUNT_CHANGED = "count-changed"
OPENRGB_READINESS_COMPLETE = "complete"
OPENRGB_READINESS_UNSUPPORTED = "unsupported"
OPENRGB_READINESS_RECONNECTING = "reconnecting"
OPENRGB_READINESS_READY = "ready"
OPENRGB_READINESS_TIMEOUT = "timeout"
OPENRGB_READINESS_CANCELLED = "cancelled"


def _openrgb_send_packet(sock, packet_id, payload=b""):
    """Send one correctly framed, device-less OpenRGB SDK packet over `sock`."""
    header = struct.pack("<4sIII", OPENRGB_SDK_MAGIC, 0, int(packet_id), len(payload))
    sock.sendall(header + payload)


def decode_openrgb_controller_count(payload, protocol_version):
    """Split a NET_PACKET_ID_REQUEST_CONTROLLER_COUNT body into (count, ids).

    The body depends on the protocol that was negotiated for the connection, so
    it is parsed with that version instead of a guess (Documentation/
    OpenRGBSDK.md, "Device IDs"): protocol 0-5 answer with the little-endian
    32-bit count alone, protocol 6 and above append one little-endian 32-bit
    unique controller id per controller. A body that does not match the
    negotiated shape exactly is not a count and returns None.
    """
    if len(payload) < 4:
        return None
    count = int(struct.unpack_from("<I", payload, 0)[0])
    if protocol_version >= OPENRGB_SDK_DETECTION_PROTOCOL_VERSION:
        if len(payload) != 4 + 4 * count:
            return None
        return count, struct.unpack_from("<%dI" % count, payload, 4)
    if len(payload) != 4:
        return None
    return count, ()


def decode_openrgb_detection_progress(payload):
    """Best-effort (percent, text) of a detection-progress event body.

    Layout (protocol 6): u32 data_size, u32 detection_percent, u16
    string_length, then the null-terminated string. Only the diagnostic text of
    the restore log depends on it, so a truncated or oversized body loses the
    text and never the event itself.
    """
    if len(payload) < 10:
        return None, ""
    percent = int(struct.unpack_from("<I", payload, 4)[0])
    string_length = int(struct.unpack_from("<H", payload, 8)[0])
    raw = payload[10 : 10 + string_length].split(b"\x00", 1)[0]
    return percent, raw.decode("utf-8", "replace")


class OpenRgbSdkConnection:
    """One read-only OpenRGB SDK connection with the protocol negotiated.

    The connection records what the server has told it so far - the negotiated
    protocol version, the controller count and ids of the last count reply, the
    progress of the last detection event and how many detection events of each
    kind arrived - so the gate above is a policy over facts instead of packet
    plumbing. The only two requests it can send are:

        NET_PACKET_ID_REQUEST_PROTOCOL_VERSION  (40)  once, to negotiate
        NET_PACKET_ID_REQUEST_CONTROLLER_COUNT  (0)   the device list

    Every packet the server pushes is consumed in full, including the ones this
    client ignores (acknowledgements, device-list updates, controller updates),
    so the stream can never desynchronise. Nothing here reads or writes RGB data
    and nothing here ever asks for a rescan.
    """

    def __init__(self, sock, timeout=OPENRGB_SDK_PROBE_TIMEOUT_SECONDS):
        self.sock = sock
        self.timeout = timeout
        self.closed = False
        self.protocol_version = 0
        self.server_protocol_version = None
        self.server_name = None
        self.controller_count = None
        self.controller_ids = ()
        # Requests and replies are matched by index (the server answers a
        # connection in order), which is what lets the gate tell a count reply
        # that was already in flight from the one it asked for *after*
        # detection completed.
        self.count_requests_sent = 0
        self.count_replies_received = 0
        self.detection_started_count = 0
        self.detection_progress_count = 0
        self.detection_complete_count = 0
        self.detection_percent = None
        self.detection_string = ""
        self.device_list_update_count = 0

    @property
    def alive(self):
        """False once the stream is broken, the peer is gone or we closed it."""
        return not self.closed

    # --- sending -------------------------------------------------------
    def send_request(self, packet_id, payload=b""):
        """Send one request. False when the connection is no longer usable."""
        if self.closed or self.sock is None:
            return False
        try:
            _openrgb_send_packet(self.sock, packet_id, payload)
        except Exception:
            self.closed = True
            return False
        return True

    def request_protocol_version(self, version=None):
        """Announce this client's protocol and ask for the server's."""
        if version is None:
            version = OPENRGB_SDK_CLIENT_PROTOCOL_VERSION
        return self.send_request(
            OPENRGB_SDK_PACKET_REQUEST_PROTOCOL_VERSION, struct.pack("<I", int(version))
        )

    def request_controller_count(self):
        """Ask for the device list. The count request has no body."""
        if not self.send_request(OPENRGB_SDK_PACKET_REQUEST_CONTROLLER_COUNT):
            return False
        self.count_requests_sent += 1
        return True

    # --- receiving -----------------------------------------------------
    def negotiate(self, timeout=None):
        """Negotiate the protocol version and store the result.

        The request carries the highest version this client speaks; the server
        stores ``min(requested, its own)`` for the connection and answers with
        its own maximum. A server without protocol versioning sends no version
        reply at all (an older server may still acknowledge the request with an
        "unsupported" status), which is the documented meaning of "the server's
        highest version is 0" - so silence is protocol 0, not an error.
        Returns the negotiated version (also stored in `protocol_version`).
        """
        if not self.request_protocol_version():
            self.protocol_version = 0
            return 0
        remaining = OPENRGB_SDK_NEGOTIATION_TIMEOUT_SECONDS if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, float(remaining))
        while self.alive:
            now = time.monotonic()
            if now >= deadline:
                break
            packet_id = self.read(deadline - now)
            if packet_id == OPENRGB_SDK_PACKET_REQUEST_PROTOCOL_VERSION:
                break
        server_version = self.server_protocol_version
        if server_version is None:
            self.protocol_version = 0
        else:
            self.protocol_version = min(
                OPENRGB_SDK_CLIENT_PROTOCOL_VERSION, int(server_version)
            )
        return self.protocol_version

    def read(self, timeout):
        """Read and dispatch one packet. Returns its id, or None.

        None means "nothing arrived within `timeout`" - not an error. A
        disconnect, a short read, a bad magic, an impossible payload size or a
        timeout in the middle of a packet marks the connection closed, because
        the stream cannot be resynchronised after any of those.
        """
        if self.closed or self.sock is None:
            return None
        try:
            # Never 0: a zero timeout would switch the socket into non-blocking
            # mode and every read would fail instead of waiting.
            self.sock.settimeout(max(0.01, float(timeout)))
        except Exception:
            self.closed = True
            return None
        try:
            header = self._read_exactly(OPENRGB_SDK_HEADER_SIZE)
            if header is None:
                self.closed = True
                return None
            magic, device_id, packet_id, payload_size = struct.unpack("<4sIII", header)
            if magic != OPENRGB_SDK_MAGIC:
                self.closed = True
                return None
            if payload_size > OPENRGB_SDK_MAX_PAYLOAD_SIZE:
                self.closed = True
                return None
            payload = b""
            if payload_size:
                payload = self._read_exactly(payload_size)
                if payload is None:
                    self.closed = True
                    return None
            self._dispatch(int(device_id), int(packet_id), payload)
            return int(packet_id)
        except (socket.timeout, TimeoutError):
            return None
        except Exception:
            self.closed = True
            return None

    def _read_exactly(self, size):
        """Exactly `size` bytes, or None (disconnect / short read).

        A `socket.timeout` propagates - the caller turns it into "nothing
        arrived" - but one raised after the first byte marks the connection
        closed, because half a packet cannot be resynchronised.
        """
        chunks = []
        remaining = int(size)
        while remaining > 0:
            try:
                chunk = self.sock.recv(remaining)
            except (socket.timeout, TimeoutError):
                if chunks:
                    self.closed = True
                raise
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _dispatch(self, device_id, packet_id, payload):
        """Record one packet. Unknown packets are simply dropped."""
        if packet_id == OPENRGB_SDK_PACKET_REQUEST_CONTROLLER_COUNT:
            if device_id != 0:
                return
            decoded = decode_openrgb_controller_count(payload, self.protocol_version)
            if decoded is not None:
                self.controller_count, self.controller_ids = decoded
                self.count_replies_received += 1
        elif packet_id == OPENRGB_SDK_PACKET_REQUEST_PROTOCOL_VERSION:
            if device_id != 0 or len(payload) < 4:
                return
            self.server_protocol_version = int(struct.unpack_from("<I", payload, 0)[0])
        elif packet_id == OPENRGB_SDK_PACKET_SET_SERVER_NAME:
            if device_id != 0:
                return
            self.server_name = payload.split(b"\x00", 1)[0].decode("utf-8", "replace")
        elif packet_id == OPENRGB_SDK_PACKET_DETECTION_STARTED:
            if device_id == 0:
                self.detection_started_count += 1
        elif packet_id == OPENRGB_SDK_PACKET_DETECTION_PROGRESS_CHANGED:
            if device_id != 0:
                return
            self.detection_progress_count += 1
            percent, text = decode_openrgb_detection_progress(payload)
            if percent is not None:
                self.detection_percent = percent
            if text:
                self.detection_string = text
        elif packet_id == OPENRGB_SDK_PACKET_DETECTION_COMPLETE:
            if device_id == 0:
                self.detection_complete_count += 1
        elif packet_id == OPENRGB_SDK_PACKET_DEVICE_LIST_UPDATED:
            if device_id == 0:
                self.device_list_update_count += 1

    def close(self):
        """Close the socket. Idempotent and never raises."""
        self.closed = True
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def open_openrgb_sdk_connection(timeout=OPENRGB_SDK_PROBE_TIMEOUT_SECONDS):
    """Connect to the OpenRGB SDK server and negotiate the protocol.

    Returns a connected `OpenRgbSdkConnection` whose `protocol_version` is the
    version negotiated for that connection, or None when OpenRGB's SDK server
    cannot be reached at all. Never raises: a refusal, a timeout and a disconnect
    all mean "not reachable yet", and a server that does not answer the
    negotiation is protocol 0, which the connection stays usable for.

    Read-only, loopback, no privileges: an ordinary SDK client, exactly like the
    one Artemis' plugin opens.
    """
    timeout = max(0.05, float(timeout))
    try:
        sock = socket.create_connection(
            (OPENRGB_SDK_HOST, OPENRGB_SDK_PORT), timeout=timeout
        )
    except Exception:
        return None
    connection = OpenRgbSdkConnection(sock, timeout=timeout)
    try:
        sock.settimeout(timeout)
        connection.negotiate()
    except Exception:
        connection.close()
        return None
    return connection


def _openrgb_readiness_message(state, **values):
    """The restore-log line for one readiness state, or None for poll noise."""
    elapsed = values.get("elapsed", 0.0)
    count = values.get("count")
    if state == OPENRGB_READINESS_PROGRESS:
        percent = values.get("percent")
        text = values.get("text") or ""
        shown = "?" if percent is None else percent
        return f"OpenRGB detection progress: {shown}%{(' - ' + text) if text else ''}"
    if state == OPENRGB_READINESS_DETECTING:
        return "OpenRGB is detecting controllers..."
    if state == OPENRGB_READINESS_DETECTED:
        return f"OpenRGB SDK detected {count} controller(s)."
    if state == OPENRGB_READINESS_COUNT_CHANGED:
        return (
            f"OpenRGB SDK controller count changed: {values.get('previous')} -> {count}."
        )
    if state == OPENRGB_READINESS_COMPLETE:
        return f"OpenRGB detection completed ({elapsed:.1f}s)."
    if state == OPENRGB_READINESS_UNSUPPORTED:
        return (
            f"OpenRGB SDK protocol {values.get('protocol')} does not report "
            "detection completion. Waiting for the readiness budget..."
        )
    if state == OPENRGB_READINESS_RECONNECTING:
        return "OpenRGB SDK connection lost. Reconnecting..."
    if state == OPENRGB_READINESS_READY:
        if values.get("confirmed"):
            if count is None:
                return f"OpenRGB SDK ready: detection completed ({elapsed:.1f}s)."
            return (
                f"OpenRGB SDK ready: {count} controller(s), detection completed "
                f"({elapsed:.1f}s)."
            )
        return (
            f"OpenRGB SDK ready: {count} controller(s), unchanged for "
            f"{values.get('window', 0):.0f}s ({elapsed:.1f}s)."
        )
    if state == OPENRGB_READINESS_TIMEOUT:
        return (
            "WARNING: OpenRGB detection readiness could not be confirmed within "
            f"{values.get('timeout', OPENRGB_READINESS_TIMEOUT_SECONDS):.0f}s. "
            "Continuing."
        )
    if state == OPENRGB_READINESS_CANCELLED:
        return "OpenRGB readiness wait cancelled."
    return None


def wait_for_openrgb_ready(
    connect=None,
    sleep=None,
    elapsed=None,
    cancelled=None,
    publish=None,
    launched=False,
    timeout=OPENRGB_READINESS_TIMEOUT_SECONDS,
    poll_interval=OPENRGB_READINESS_POLL_SECONDS,
    settle_seconds=OPENRGB_READINESS_SETTLE_SECONDS,
    already_running_seconds=OPENRGB_READINESS_ALREADY_RUNNING_SECONDS,
    progress_log_seconds=OPENRGB_READINESS_PROGRESS_LOG_SECONDS,
):
    """Wait until OpenRGB's controller detection is confirmed complete.

    The restore sequence used to sleep a fixed 8 seconds after requesting the
    OpenRGB launch and then start Artemis. That is a race - OpenRGB binds its
    SDK server while it is still detecting - and so was the count-stability rule
    that replaced it: the controller count stays unchanged at one for 10.5 of
    the measured 12.3 second cold start, so "the count has not moved" declares
    readiness while three of four controllers are still missing. See the block
    comment above for the measurements and for the OpenRGB source that defines
    the real signal.

    `launched` is what makes the two situations distinguishable, and it is the
    caller's knowledge, not a guess:

    * `launched=True` - this restore started that OpenRGB process, so its
      detection is still ahead of us. Readiness is DETECTION_COMPLETE (103) and
      nothing else: no count, and no amount of waiting without the event, is
      accepted. A server that cannot report it runs out the budget.

    * `launched=False` - OpenRGB was already running when the restore began.
      Its detection may have finished before we connected, in which case 103
      never arrives on our connection. 103 is still awaited, but readiness is
      also accepted once a positive controller count has been unchanged - and no
      detection event has been received - for `already_running_seconds`, which
      is longer than OpenRGB's widest silent detection stage.

    Returns True when readiness was confirmed (the settle delay has then run),
    False on timeout or cancellation. It never raises, and a timeout is not a
    failure: the caller continues with the rest of the restore, so a broken or
    old OpenRGB can never hold the wake path hostage.

    `connect`, `sleep`, `elapsed`, `cancelled` and `publish` are injectable so
    the gate can be tested without a real OpenRGB, a real clock or real
    sleeping. The defaults are the real SDK connection factory, `time.sleep`,
    `time.monotonic`, "never cancelled" and "discard the progress line"; an
    injected `connect(timeout=...)` must return an `OpenRgbSdkConnection` (or
    something with the same interface), or None when OpenRGB cannot be reached.
    `publish(text)` is called only for real transitions - never once per poll,
    and never once per detection-progress event either: OpenRGB emits one of
    those per detector it walks through (991 in one measured cold start), so the
    progress line is rate-limited to a percentage change or one line per
    `progress_log_seconds`.
    """
    connector = open_openrgb_sdk_connection if connect is None else connect
    sleeper = time.sleep if sleep is None else sleep
    now_at = time.monotonic if elapsed is None else elapsed
    is_cancelled = (lambda: False) if cancelled is None else cancelled
    # A floor as well as a ceiling: a zero interval would turn the wait loops
    # into a busy spin and could never let a fake or real clock advance.
    poll = max(0.05, float(poll_interval))
    window = max(0.0, float(already_running_seconds))
    progress_log_interval = max(0.0, float(progress_log_seconds))

    def publish_state(state, **values):
        if publish is None:
            return
        line = _openrgb_readiness_message(state, **values)
        if line is not None:
            publish(line)

    started = now_at()
    deadline = started + max(0.0, float(timeout))
    connection = None

    def connect_until_available():
        """Connect (bounded by the budget). None means the caller must give up."""
        while True:
            if is_cancelled():
                publish_state(OPENRGB_READINESS_CANCELLED)
                return None
            now = now_at()
            if now >= deadline:
                publish_state(OPENRGB_READINESS_TIMEOUT, timeout=timeout)
                return None
            remaining = min(OPENRGB_SDK_PROBE_TIMEOUT_SECONDS, max(0.0, deadline - now))
            found = connector(timeout=remaining)
            if found is not None:
                return found
            sleeper(poll)

    try:
        connection = connect_until_available()
        if connection is None:
            return False

        protocol = int(getattr(connection, "protocol_version", 0) or 0)
        supports_events = protocol >= OPENRGB_SDK_DETECTION_PROTOCOL_VERSION
        if not supports_events:
            publish_state(OPENRGB_READINESS_UNSUPPORTED, protocol=protocol)

        confirmed = False  # DETECTION_COMPLETE was observed
        confirmed_at = None
        final_count_request = 0  # count requests sent when completion arrived
        count = None
        quiet_since = now_at()
        seen_started = seen_progress = seen_complete = 0
        last_request = None
        accepted = False
        progress_percent = None
        progress_logged_at = None

        while True:
            now = now_at()

            # --- what the server has said since the last look ---------------
            if connection.detection_started_count != seen_started:
                seen_started = connection.detection_started_count
                quiet_since = now
                publish_state(OPENRGB_READINESS_DETECTING)
            if connection.detection_progress_count != seen_progress:
                seen_progress = connection.detection_progress_count
                quiet_since = now
                # One line per detector would be ~1 000 lines per restore, so a
                # percentage change or the logging interval is what publishes.
                if (
                    progress_logged_at is None
                    or connection.detection_percent != progress_percent
                    or now - progress_logged_at >= progress_log_interval
                ):
                    progress_percent = connection.detection_percent
                    progress_logged_at = now
                    publish_state(
                        OPENRGB_READINESS_PROGRESS,
                        percent=connection.detection_percent,
                        text=connection.detection_string,
                    )
            if connection.detection_complete_count != seen_complete:
                seen_complete = connection.detection_complete_count
                if supports_events:
                    confirmed = True
                    confirmed_at = now
                    publish_state(OPENRGB_READINESS_COMPLETE, elapsed=now - started)
                    # The controller count is the *result* of the detection that
                    # just finished, so the device list is asked for again here.
                    # A reply already in flight answers a request that was sent
                    # before completion and may still be one controller short,
                    # so readiness waits for a reply to one of the requests sent
                    # from this point on: the server answers in order, so a reply
                    # whose index reaches `count_requests_sent` is fresh.
                    connection.request_controller_count()
                    final_count_request = connection.count_requests_sent
                    last_request = now
            if connection.controller_count != count:
                previous, count = count, connection.controller_count
                quiet_since = now
                if count and count > 0:
                    publish_state(
                        OPENRGB_READINESS_COUNT_CHANGED
                        if previous
                        else OPENRGB_READINESS_DETECTED,
                        count=count,
                        previous=previous,
                        elapsed=now - started,
                    )

            # --- is it ready? ----------------------------------------------
            if confirmed:
                if connection.count_replies_received >= final_count_request:
                    accepted = True
                elif now - confirmed_at >= max(poll, OPENRGB_SDK_PROBE_TIMEOUT_SECONDS):
                    # Detection is confirmed; a count reply that never arrives
                    # must not undo that. The readiness line then simply says so
                    # without naming a number.
                    count = None
                    accepted = True
            elif (
                not launched
                and supports_events
                and count is not None
                and count > 0
                and now - quiet_since >= window
            ):
                accepted = True
            if accepted:
                break

            if is_cancelled():
                publish_state(OPENRGB_READINESS_CANCELLED)
                return False
            if now >= deadline:
                publish_state(OPENRGB_READINESS_TIMEOUT, timeout=timeout)
                return False

            # --- keep the controller count fresh ----------------------------
            if last_request is None or now - last_request >= poll:
                connection.request_controller_count()
                last_request = now

            # --- let the server talk ---------------------------------------
            slice_seconds = min(poll, max(0.0, deadline - now)) if poll > 0 else 0.0
            if slice_seconds > 0:
                connection.read(slice_seconds)

            if not connection.alive:
                connection.close()
                connection = None
                publish_state(OPENRGB_READINESS_RECONNECTING)
                connection = connect_until_available()
                if connection is None:
                    return False
                # A fresh connection has seen nothing, so nothing it reports may
                # be trusted as "unchanged since we started watching" - and a
                # completion event that arrived before the drop was for the old
                # connection, so it is not remembered either.
                protocol = int(getattr(connection, "protocol_version", 0) or 0)
                supports_events = protocol >= OPENRGB_SDK_DETECTION_PROTOCOL_VERSION
                confirmed = False
                confirmed_at = None
                final_count_request = 0
                count = None
                quiet_since = now_at()
                seen_started = seen_progress = seen_complete = 0
                last_request = None
                progress_percent = None
                progress_logged_at = None

        sleeper(settle_seconds)
        if is_cancelled():
            publish_state(OPENRGB_READINESS_CANCELLED)
            return False
        publish_state(
            OPENRGB_READINESS_READY,
            count=count,
            elapsed=now_at() - started,
            confirmed=confirmed,
            window=window,
        )
        return True
    except Exception:
        # Defensive: readiness must never disturb the restore sequence.
        logging.exception("[RESTORE] The OpenRGB readiness gate failed unexpectedly.")
        return False
    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------
# Launch / Restore Sequencer (Background Thread)
# ---------------------------------------------------------
class RestoreEngineThread(QThread):
    progress_update = pyqtSignal(str)
    finished_sequence = pyqtSignal()

    def __init__(self, config_manager):
        super().__init__()
        self.config_manager = config_manager
        self.running = True

    def stop(self):
        self.running = False
        self.wait(3000)

    def run(self):
        self.progress_update.emit("Restoration sequence started. Waiting 5s for system initialization...")
        self.sleep(5) # Allow Windows system clock and network stack to fully stabilize/synchronize after wake/boot
        if not self.running:
            self.progress_update.emit("Restoration sequence cancelled.")
            return
        try:
            config = self.config_manager.load()

            # Integration enable flags. A disabled integration is neither
            # launched, stopped nor waited for; its absence is not an error.
            # For enabled integrations the sequence and timing below are
            # unchanged from the previous behaviour.
            use_openrgb = integration_enabled(config, "openrgb")
            use_connector = integration_enabled(config, "yeelight_connector")
            use_synapse = integration_enabled(config, "razer_synapse")
            use_artemis = integration_enabled(config, "artemis")

            # Yeelight device targets: every enabled configured device, read from
            # the configuration alone. No discovery, no network probing — one dead
            # device can never stop the others (each call is isolated).
            device_ips = enabled_device_ips(config)

            # --- 0. Clean up stale processes to guarantee fresh hardware detection on wake ---
            self.progress_update.emit("Terminating any stale background light controller processes...")
            if use_connector:
                self.kill_process(INTEGRATIONS["yeelight_connector"]["process"])
            if use_artemis:
                self.kill_process(INTEGRATIONS["artemis"]["process"])
            if use_openrgb:
                self.kill_process(INTEGRATIONS["openrgb"]["process"])
            self.sleep(3)  # Allow music mode TCP connections to fully close and bulbs to exit music mode
            if not self.running:
                self.progress_update.emit("Restoration sequence cancelled.")
                return
            
            # --- 1. Launch OpenRGB Server (needs Admin for RAM RGB) ---
            if use_openrgb:
                openrgb_path = integration_path(config, "openrgb")
                self.progress_update.emit("Checking OpenRGB status...")
                # OpenRGB launched (or already running) is *not* the same as
                # OpenRGB usable by another SDK client: the server is already
                # listening while it is still enumerating controllers, so a
                # consumer connecting early sees an empty or incomplete device
                # list. Every path below therefore ends in the readiness gate,
                # which the caller's next steps wait on. A skipped OpenRGB
                # (disabled, missing executable, no elevation task) never opens
                # the gate, exactly as it never waited before.
                #
                # The gate is told which of the two situations this is: an
                # OpenRGB we just started is waiting for its own detection to
                # complete (the DETECTION_COMPLETE event, and nothing else, will
                # do), while a process that was already there may have finished
                # long ago. See the readiness section above.
                #
                # The gate returning "not ready" is *not* a cancellation: a
                # timeout degrades gracefully and the restore continues, exactly
                # like the fixed wait before it. Only `self.running` decides
                # whether this is a cancelled sequence.
                if not self.is_process_running(INTEGRATIONS["openrgb"]["process"]):
                    if self.integration_path_available("openrgb", openrgb_path):
                        started = self.launch_openrgb(openrgb_path)
                        if started:
                            self.progress_update.emit(
                                "OpenRGB launched. Waiting for OpenRGB to finish detecting controllers..."
                            )
                            self.wait_for_openrgb_ready(launched=True)
                            if not self.running:
                                self.progress_update.emit("Restoration sequence cancelled.")
                                return
                else:
                    self.progress_update.emit("OpenRGB is already running.")
                    self.progress_update.emit(
                        "Waiting for the running OpenRGB to confirm its controller list..."
                    )
                    self.wait_for_openrgb_ready(launched=False)
                    if not self.running:
                        self.progress_update.emit("Restoration sequence cancelled.")
                        return
            else:
                self.progress_update.emit("OpenRGB integration is disabled. Skipping.")

            # --- 2. Evaluate Sun State to Decide Yeelight Connection ---
            self.progress_update.emit("Checking Solar State before lighting up...")
            is_dark = self.evaluate_solar_state(config)
            
            if is_dark:
                self.progress_update.emit(
                    f"Nighttime confirmed. Turning ON {len(device_ips)} configured Yeelight device(s)..."
                )
                self.power_devices(device_ips, turn_on=True)
                if not self.running:
                    self.progress_update.emit("Restoration sequence cancelled.")
                    return

                if use_connector:
                    connector_path = integration_path(config, "yeelight_connector")
                    if not self.is_process_running(INTEGRATIONS["yeelight_connector"]["process"]):
                        if self.integration_path_available("yeelight_connector", connector_path):
                            self.progress_update.emit("Launching Yeelight Chroma Connector (Minimized)...")
                            # The connector must start in the folder containing its own
                            # executable. Launched with the inherited working directory it
                            # resolved its runtime files (VCRUNTIME140.dll, ...) from this
                            # application's own packaged folder (dist\...\_internal),
                            # which kept build output locked for the next build.
                            self.launch_process(
                                connector_path,
                                hidden=False,
                                cwd=os.path.dirname(connector_path),
                            )
                    else:
                        self.progress_update.emit("Yeelight Chroma Connector already running.")
                else:
                    self.progress_update.emit("Yeelight Chroma Connector integration is disabled. Skipping.")
            else:
                self.progress_update.emit("Daytime detected. Keeping/turning Yeelight bulbs OFF...")
                if use_connector:
                    self.progress_update.emit("Stopping Yeelight Chroma Connector process...")
                    self.kill_process(INTEGRATIONS["yeelight_connector"]["process"])
                    self.sleep(1) # wait a moment for the process to exit and connection to close
                self.power_devices(device_ips, turn_on=False)
                if not self.running:
                    self.progress_update.emit("Restoration sequence cancelled.")
                    return

            # --- 3. Verify/Wait for Razer Synapse ---
            automation = config.get("automation", {})
            launch_synapse = automation.get("launch_razer_synapse", False)
            wait_for_synapse = automation.get("wait_for_razer_synapse", True)
            synapse_timeout = int(float(automation.get("razer_synapse_timeout_seconds", 45)))

            if not use_synapse:
                self.progress_update.emit("Razer Synapse integration is disabled. Skipping Razer Synapse handling.")
            else:
                self.progress_update.emit("Checking Razer Synapse status...")
                synapse_proc = INTEGRATIONS["razer_synapse"]["process"]
                if launch_synapse:
                    if not self.is_process_running(synapse_proc):
                        synapse_path = integration_path(config, "razer_synapse")
                        if self.integration_path_available("razer_synapse", synapse_path):
                            self.progress_update.emit("Starting Razer Synapse (Minimized)...")
                            # Razer is stubborn; starts background services. Launched minimized natively.
                            self.launch_process(synapse_path, hidden=False)
                            self.progress_update.emit("Waiting 12s for Razer SDK...")
                            self.sleep(12)
                    else:
                        self.progress_update.emit("Razer Synapse is already running.")
                elif wait_for_synapse:
                    if not self.is_process_running(synapse_proc):
                        self.progress_update.emit("Waiting for Razer Synapse to start...")
                        synapse_found = False
                        for i in range(synapse_timeout):
                            if not self.running:
                                break
                            if self.is_process_running(synapse_proc):
                                self.progress_update.emit(f"Razer Synapse detected after {i+1}s. Waiting 5s for initialization...")
                                self.sleep(5)
                                synapse_found = True
                                break
                            self.sleep(1)
                        if not synapse_found:
                            self.progress_update.emit("WARNING: Razer Synapse not running after timeout. Proceeding.")
                    else:
                        self.progress_update.emit("Razer Synapse is already running.")
                else:
                    self.progress_update.emit("Razer Synapse check skipped per config.")

            # --- 4. Launch Artemis 2 ---
            if not self.running:
                self.progress_update.emit("Restoration sequence cancelled.")
                return
            if not use_artemis:
                self.progress_update.emit("Artemis integration is disabled. Skipping.")
            else:
                self.progress_update.emit("Checking Artemis status...")
                artemis_proc = INTEGRATIONS["artemis"]["process"]
                if not self.is_process_running(artemis_proc):
                    artemis_path = integration_path(config, "artemis")
                    if self.integration_path_available("artemis", artemis_path):
                        self.progress_update.emit("Launching Artemis 2 (Minimized)...")
                        self.launch_process(artemis_path, ["--minimized"], hidden=False)
                        self.sleep(3)
                        if not self.is_process_running(artemis_proc):
                            self.progress_update.emit("WARNING: Artemis did not stay running after launch. Retrying once...")
                            self.launch_process(artemis_path, ["--minimized"], hidden=False)
                            self.sleep(3)
                            if not self.is_process_running(artemis_proc):
                                self.progress_update.emit("WARNING: Artemis still not detected after retry.")
                else:
                    self.progress_update.emit("Artemis is already running.")

            self.progress_update.emit("Restoration sequence successfully completed!")

        except Exception as e:
            self.progress_update.emit(f"ERROR: Sequence aborted: {e}")
            logging.error(f"Restoration error: {e}")
            
        self.finished_sequence.emit()

    def is_process_running(self, name):
        try:
            return name.lower() in get_running_processes_win32()
        except Exception:
            return False

    def integration_path_available(self, key, path):
        """True when an enabled integration actually has a usable executable.

        A blank or missing executable is not an error: the integration is simply
        skipped for this pass and the rest of the sequence continues. The path
        must point at a *file* — an existing directory is not a usable
        executable. Only the executable's file name is logged, never the full
        local path.
        """
        label = INTEGRATIONS[key]["label"]
        if not path:
            self.progress_update.emit(
                f"WARNING: {label} is enabled but has no executable path configured. Skipping it."
            )
            return False
        if not os.path.isfile(path):
            self.progress_update.emit(
                f"WARNING: {label} executable ({os.path.basename(path)}) was not found. Skipping it until it is installed."
            )
            logging.warning("[RESTORE] %s executable is missing (%s); skipping.", label, os.path.basename(path))
            return False
        return True

    def kill_process(self, name):
        try:
            killed = terminate_processes_win32(name)
            if killed == 0:
                subprocess.run(["taskkill", "/f", "/im", name], creationflags=0x08000000, capture_output=True)
        except Exception:
            pass

    def launch_process(self, path, args=None, hidden=True, cwd=None):
        if not self.running:
            self.progress_update.emit("Launch skipped because restoration was cancelled.")
            return
        # An executable path must be a file: an existing directory would only
        # fail later, inside Popen.
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f"Executable not found: {path}")
            
        cmd = [path]
        if args:
            cmd.extend(args)
            
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0 if hidden else 7  # 0 = SW_HIDE, 7 = SW_SHOWMINNOACTIVE
        
        # We do NOT use CREATE_NO_WINDOW (0x08000000) for GUI processes, as this flag is intended
        # exclusively for console applications and causes WPF/WinForms (like Yeelight Connector)
        # to silently crash or hang during startup. We use creationflags=0.
        # `cwd` stays opt-in: most integrations are launched with the inherited
        # working directory. The two that must not inherit ours pass their own
        # executable's folder explicitly — OpenRGB (elevated child) and the
        # Yeelight Chroma Connector (resolves its runtime DLLs from the CWD).
        #
        # Every launch through here starts an *installed third-party* program, so
        # the frozen-build contamination is cleared for all of them: this
        # process's injected DLL directory is reset for the duration of the spawn
        # and the child's PATH no longer lists our bundle. Subprocesses that
        # belong to this application (the elevation helper, `taskkill`) do not go
        # through this method and are deliberately left alone, and the OpenRGB
        # scheduled task is already isolated by Task Scheduler.
        with external_process_environment_scope():
            proc = subprocess.Popen(
                cmd,
                startupinfo=startupinfo,
                creationflags=0,
                cwd=cwd,
                env=external_process_environment(),
            )
        logging.info(f"Successfully spawned '{os.path.basename(path)}' with PID {proc.pid}")

    def wait_for_openrgb_ready(self, launched=False):
        """Hold the restore sequence until OpenRGB's detection is confirmed.

        The module-level `wait_for_openrgb_ready()` is the gate itself; this
        wrapper binds it to the running thread so that

        * `self.running` is honoured (a suspend during the wait returns
          immediately and lets the caller take its normal cancellation path),
        * `launched` tells the gate whether *this* restore started the OpenRGB
          process - the fact that decides between "wait for the completion
          event" and "a settled controller count is credible",
        * the thread's own sleep is used for every wait of the gate, and
        * every state change reaches the restore log through `progress_update`.

        The thread's sleep is `QThread.msleep`, not `QThread.sleep`: the gate
        polls every 0.5 s and settles for 1.5 s, and `QThread.sleep` takes whole
        seconds. `time.sleep` would work for the fractions but would take the
        wait out of the Qt thread and out of the thread's own millisecond
        granularity, so the gate never falls back to it (verified by a test that
        makes `time.sleep` raise).

        The OpenRGB SDK client is an ordinary unprivileged localhost client and
        changes nothing about the elevation model (§4b).
        """
        return wait_for_openrgb_ready(
            cancelled=lambda: not self.running,
            publish=self.progress_update.emit,
            sleep=lambda seconds: self.msleep(max(0, int(round(float(seconds) * 1000)))),
            launched=launched,
        )

    def launch_openrgb(self, path):
        """Start OpenRGB without ever producing a UAC prompt on the wake path.

        OpenRGB needs administrator rights, but waking the PC must stay
        unattended, so the privileged launch is delegated to the pre-authorised
        scheduled task ``YeelightPCCompanion-OpenRGB`` that the user approved
        once during setup. Starting an existing highest-privilege task is silent.

        When this application is already elevated it can start OpenRGB directly
        as a child process instead, and no Task Scheduler round trip happens.
        That direct launch pins the working directory to the folder containing
        OpenRGB, so the elevated child never inherits ours (the scheduled task
        does the same through ``<WorkingDirectory>``).

        Returns True when OpenRGB was started, False when it was skipped. This
        method never requests elevation: a missing or stale task degrades to
        "OpenRGB skipped for this restore" and the rest of the sequence runs.
        """
        if is_process_elevated():
            self.progress_update.emit(
                "Starting OpenRGB SDK Server directly (this application is elevated)..."
            )
            self.launch_process(
                path, list(OPENRGB_TASK_ARGS), hidden=True, cwd=os.path.dirname(path)
            )
            logging.info("[RESTORE] OpenRGB started directly (app is elevated).")
            return True

        try:
            status = openrgb_elevation_status(path)
        except Exception as exc:
            status = None
            logging.warning("[RESTORE] OpenRGB elevation status could not be read: %s", exc)

        if status is None or not status.is_ready:
            self.progress_update.emit(
                "WARNING: OpenRGB seamless elevation is not configured. "
                "Skipping OpenRGB for this restore. Open Settings to repair."
            )
            logging.warning(
                "[RESTORE] OpenRGB seamless elevation is not configured (%s); "
                "skipping OpenRGB for this restore. Open Settings to repair.",
                status.label if status is not None else "status unavailable",
            )
            return False

        try:
            run_openrgb_task()
        except WindowsTaskError as exc:
            self.progress_update.emit(
                "WARNING: OpenRGB could not be started through Windows Task Scheduler. "
                "Skipping OpenRGB for this restore. Open Settings to repair."
            )
            logging.warning(
                "[RESTORE] OpenRGB scheduled task could not be started: %s", exc
            )
            return False

        self.progress_update.emit(
            "Starting OpenRGB SDK Server through the elevated launch task..."
        )
        logging.info("[RESTORE] OpenRGB scheduled task %s started.", OPENRGB_TASK_NAME)
        return True

    def evaluate_solar_state(self, config):
        try:
            import ephem
            obs = ephem.Observer()
            obs.lat = str(config["location"]["latitude"])
            obs.lon = str(config["location"]["longitude"])
            obs.elevation = float(config["location"]["elevation"])
            obs.date = datetime.now(timezone.utc)
            now = obs.date.datetime()
            next_sunrise = obs.next_rising(ephem.Sun()).datetime()
            next_sunset = obs.next_setting(ephem.Sun()).datetime()
            buffer_delta = timedelta(hours=float(config["location"]["light_buffer_hours"]))
            
            if next_sunrise < next_sunset:
                return True
            prev_sunrise = obs.previous_rising(ephem.Sun()).datetime()
            time_to_sunset = next_sunset - now
            time_since_sunrise = now - prev_sunrise
            return (time_to_sunset <= buffer_delta) or (time_since_sunrise <= buffer_delta)
        except Exception as e:
            logging.error(f"Failed to evaluate solar state in sequencer: {e}")
            return False

    def power_devices(self, device_ips, turn_on):
        """Send one power command to every device in the given list.

        Fault isolation is the point: Yeelight devices drop off the network
        independently, so a device that cannot be reached must never stop the
        remaining devices from being switched. Each device is reported on its
        own and the loop always continues.
        """
        for ip in device_ips or []:
            try:
                if turn_on:
                    self.safe_turn_on(ip)
                else:
                    self.safe_turn_off(ip)
            except Exception as exc:
                logging.warning(
                    "[RESTORE] Device %s could not be switched %s; continuing with the "
                    "remaining devices: %s",
                    ip,
                    "on" if turn_on else "off",
                    exc,
                )

    def safe_turn_on(self, ip):
        if not ip or not str(ip).strip():
            # No address for this device: nothing to send, no error.
            return
        ip = str(ip).strip()
        bulb = None
        try:
            from yeelight import Bulb
            bulb = Bulb(ip)
            bulb.turn_on()
        except Exception as e:
            logging.warning(f"Could not connect to turn ON bulb at {ip}: {e}")
        finally:
            if bulb:
                try:
                    if bulb._Bulb__socket is not None:
                        bulb._Bulb__socket.close()
                except Exception:
                    pass

    def safe_turn_off(self, ip):
        if not ip or not str(ip).strip():
            # No address for this device: nothing to send, no error.
            return
        ip = str(ip).strip()
        bulb = None
        try:
            from yeelight import Bulb
            bulb = Bulb(ip)
            try:
                bulb.stop_music()
            except Exception:
                pass
            bulb.turn_off()
        except Exception as e:
            logging.warning(f"Could not connect to turn OFF bulb at {ip}: {e}")
        finally:
            if bulb:
                try:
                    if bulb._Bulb__socket is not None:
                        bulb._Bulb__socket.close()
                except Exception:
                    pass

# ---------------------------------------------------------
# Native Windows Event Filter
# ---------------------------------------------------------
class WinPowerEventFilter(QAbstractNativeEventFilter):
    def __init__(self, suspend_callback, resume_callback):
        super().__init__()
        self.suspend_callback = suspend_callback
        self.resume_callback = resume_callback
        self.last_suspend_time = 0.0
        self.last_resume_time = 0.0

    def nativeEventFilter(self, eventType: QByteArray, message: int):
        try:
            if eventType == b"windows_generic_MSG" or eventType == b"windows_dispatcher_MSG":
                msg_addr = int(message)
                if msg_addr != 0:
                    msg = ctypes.wintypes.MSG.from_address(msg_addr)
                    if msg.message == WM_POWERBROADCAST:
                        now = time.time()
                        if msg.wParam == PBT_APMSUSPEND:
                            if now - self.last_suspend_time > 8.0:
                                self.last_suspend_time = now
                                logging.info("--- NATIVE EVENT: WM_POWERBROADCAST -> PBT_APMSUSPEND (Sleep) ---")
                                self.suspend_callback()
                            else:
                                logging.info("--- NATIVE EVENT: WM_POWERBROADCAST -> PBT_APMSUSPEND (Ignored duplicate) ---")
                        elif msg.wParam in [PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC]:
                            if now - self.last_resume_time > 8.0:
                                self.last_resume_time = now
                                logging.info(f"--- NATIVE EVENT: WM_POWERBROADCAST -> PBT_APMRESUME... (Wake, wParam={msg.wParam}) ---")
                                self.resume_callback()
                            else:
                                logging.info(f"--- NATIVE EVENT: WM_POWERBROADCAST -> PBT_APMRESUME... (Ignored duplicate, wParam={msg.wParam}) ---")
                    elif msg.message in [WM_QUERYENDSESSION, WM_ENDSESSION]:
                        now = time.time()
                        if now - self.last_suspend_time > 8.0:
                            self.last_suspend_time = now
                            logging.info("--- NATIVE EVENT: WM_QUERYENDSESSION/WM_ENDSESSION (Shutdown) ---")
                            self.suspend_callback()
        except Exception as e:
            logging.error(f"[EVENT FILTER] Error processing native event: {e}")
        return (False, 0)

LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
    ]

class Win32ShutdownWindow:
    """
    Hidden native Win32 window dedicated to shutdown/logoff broadcasts.
    Qt's native event filter can miss WM_QUERYENDSESSION while the app is tray-hidden;
    this window gives Windows a direct top-level HWND to notify.
    """
    CLASS_ALREADY_EXISTS = 1410

    def __init__(self, shutdown_action=None):
        self._shutdown_action = shutdown_action
        self._hwnd = None
        self._class_name = "YeelightPCCompanionShutdownReceiverWindow"
        self._wndproc = WNDPROC(self._window_proc)

    def create(self):
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetLastError.argtypes = []
            kernel32.GetLastError.restype = ctypes.wintypes.DWORD
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
            user32.RegisterClassW.restype = ctypes.wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                ctypes.wintypes.DWORD,
                ctypes.wintypes.LPCWSTR,
                ctypes.wintypes.LPCWSTR,
                ctypes.wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.wintypes.HWND,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            user32.CreateWindowExW.restype = ctypes.wintypes.HWND
            user32.DestroyWindow.argtypes = [ctypes.wintypes.HWND]
            user32.DestroyWindow.restype = ctypes.wintypes.BOOL
            user32.DefWindowProcW.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.wintypes.UINT,
                ctypes.wintypes.WPARAM,
                ctypes.wintypes.LPARAM,
            ]
            user32.DefWindowProcW.restype = LRESULT

            hinstance = kernel32.GetModuleHandleW(None)

            wc = WNDCLASS()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinstance
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self._class_name

            atom = user32.RegisterClassW(ctypes.byref(wc))
            if atom == 0:
                err = kernel32.GetLastError()
                if err != self.CLASS_ALREADY_EXISTS:
                    logging.error(f"[SHUTDOWN WINDOW] RegisterClassW failed with error {err}")
                    return False

            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                "Yeelight PC Companion Shutdown Receiver",
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                logging.error(f"[SHUTDOWN WINDOW] CreateWindowExW failed with error {kernel32.GetLastError()}")
                return False

            self._hwnd = hwnd
            logging.info("[SHUTDOWN WINDOW] Hidden Win32 shutdown receiver created.")
            return True
        except Exception as e:
            logging.error(f"[SHUTDOWN WINDOW] Failed to create shutdown receiver: {e}")
            return False

    def destroy(self):
        if self._hwnd:
            try:
                ctypes.windll.user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None

    def _window_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_QUERYENDSESSION:
                logging.info("[SHUTDOWN WINDOW] WM_QUERYENDSESSION detected. Running shutdown actions.")
                if self._shutdown_action:
                    self._shutdown_action()
                return 1
            if msg == WM_ENDSESSION:
                if wparam:
                    logging.info("[SHUTDOWN WINDOW] WM_ENDSESSION confirmed. Running shutdown actions.")
                    if self._shutdown_action:
                        self._shutdown_action()
                return 0
        except Exception as e:
            logging.error(f"[SHUTDOWN WINDOW] Error handling shutdown message: {e}")
            if msg == WM_QUERYENDSESSION:
                return 1
            return 0

        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

# ---------------------------------------------------------
# Window-level presentation constants
# ---------------------------------------------------------
PAGE_OVERVIEW = "Overview"
PAGE_DEVICES = "Devices"
PAGE_INTEGRATIONS = "Integrations"
PAGE_AUTOMATION = "Automation"
PAGE_LOGS = "Logs"

# Sidebar navigation, in display order.
NAV_PAGES = (PAGE_OVERVIEW, PAGE_DEVICES, PAGE_INTEGRATIONS, PAGE_AUTOMATION, PAGE_LOGS)

# Pages that edit the configuration, so the shared action area (Save / Import /
# Export) is shown while they are open.
PAGES_WITH_ACTION_BAR = (PAGE_DEVICES, PAGE_INTEGRATIONS, PAGE_AUTOMATION)

DEFAULT_PAGE = PAGE_OVERVIEW
SYSTEM_ACTIVE_TEXT = "System active"

# One short, factual description per integration (shown on its card).
INTEGRATION_DESCRIPTIONS = {
    "openrgb": (
        "RGB controller server. Some systems need it to run with administrator rights; the "
        "one-time seamless launch setup below keeps waking the PC unattended."
    ),
    "yeelight_connector": (
        "Bridges the Yeelight devices to Chroma-aware games and is kept in step with the "
        "day/night cycle."
    ),
    "razer_synapse": (
        "Launched or waited for on wake; its timing options live on the Automation page."
    ),
    "artemis": "Started on wake once OpenRGB has confirmed its controller list.",
}

# Content-header subtitles (restrained, no marketing wording).
PAGE_SUBTITLES = {
    PAGE_OVERVIEW: "Current day/night state, service health and manual actions.",
    PAGE_DEVICES: "The Yeelight devices this PC controls on your local network.",
    PAGE_INTEGRATIONS: "Which applications are started, stopped and restored with the PC.",
    PAGE_AUTOMATION: "What happens on sleep and wake, and the location used for sunrise and sunset.",
    PAGE_LOGS: "Log output of the current session.",
}

# The four services shown on the Overview page. Labels and process names come
# from the canonical integration definitions so the overview, the integrations
# page and the status check can never disagree. The detail line is operational
# fact only - nothing here is measured or invented at runtime.
SERVICE_ROWS = (
    (
        INTEGRATIONS["openrgb"]["label"],
        INTEGRATIONS["openrgb"]["process"],
        "RGB controller server; Artemis is started only after its device detection completes.",
    ),
    (
        INTEGRATIONS["yeelight_connector"]["label"],
        INTEGRATIONS["yeelight_connector"]["process"],
        "Bridges the Yeelight devices to Chroma-aware games; its state drives the self-healing check.",
    ),
    (
        INTEGRATIONS["razer_synapse"]["label"],
        INTEGRATIONS["razer_synapse"]["process"],
        "Launched or waited for on wake according to the Automation settings.",
    ),
    (
        INTEGRATIONS["artemis"]["label"],
        INTEGRATIONS["artemis"]["process"],
        "Started on wake once OpenRGB has confirmed its controller list.",
    ),
)

# ---------------------------------------------------------
# Process-status polling cadence
# ---------------------------------------------------------
# The Overview's service rows are refreshed from a native Toolhelp32 process
# listing. That listing is cheap per call but it is by far the largest
# recurring cost of an idle session (measured at 13-15 ms for ~176 processes on
# the development machine, i.e. ~0.5% of one CPU core at the three-second
# cadence). While the window is only in the tray those rows cannot be seen by
# anyone, so the same check runs at a much slower cadence; showing the window
# refreshes immediately, so a result that is stale from the hidden period is
# never displayed. Nothing else about the check changes - the same native call
# is still the only source of process state.
STATUS_POLL_INTERVAL_VISIBLE_MS = 3000
STATUS_POLL_INTERVAL_HIDDEN_MS = 15000

# The in-app log view keeps a bounded number of recent lines. A tray session
# runs for days and the rich-text document behind `QTextEdit` grows by roughly
# 8 KB per appended line (measured), so leaving it unbounded means memory that
# only ever grows for the lifetime of the process.
#
# Qt's own `QTextDocument.maximumBlockCount` was measured first and rejected:
# it drops one block per append, and every drop invalidates the whole document
# layout, which cost 2.8-4.6 ms *per appended line* once the limit was reached
# (against 0.19-0.27 ms while the document was still growing under this scheme,
# and 0.17-0.25 ms with no bound at all). At the observed real-world log rate of
# ~2 lines/minute the limit is reached within a day of uptime, so that cost would
# apply for the rest of the session - including to the logging inside the sleep
# path. Letting the document grow to the cap plus one batch and then removing the
# whole batch rebuilds the layout once per batch instead, which keeps the same
# bound at roughly a seventeenth of the cost.
UI_LOG_MAX_BLOCKS = 3000
UI_LOG_TRIM_BATCH = 500

# The application theme (dark palette + Fusion style + stylesheet) is global Qt
# state, so it is applied once per process instead of on every window
# construction.
_APP_THEME_APPLIED = False


def _apply_process_theme():
    """Apply the shared visual theme to the QApplication. Idempotent."""
    global _APP_THEME_APPLIED
    app = QApplication.instance()
    if app is None or _APP_THEME_APPLIED:
        return
    apply_app_theme(app)
    _APP_THEME_APPLIED = True


# ---------------------------------------------------------
# Main MainWindow UI
# ---------------------------------------------------------
class YeelightPCCompanionWindow(QMainWindow):
    def __init__(self, config_path, auto_restore=True, automation_paused=False, config_manager=None):
        super().__init__()
        # The ConfigManager owns the canonical defaults, validation, migration,
        # atomic writes and the runtime storage location.
        self.config_manager = config_manager or ConfigManager(config_path)
        self.config_path = self.config_manager.config_path
        self._automation_paused = automation_paused
        # The Stage 5 redesign: a restrained dark utility window instead of the
        # earlier glassmorphic dashboard. The theme is application-wide so every
        # dialog, message box and the first-run wizard match.
        self.setWindowTitle("Yeelight PC Companion")
        self.setMinimumSize(820, 540)
        _apply_process_theme()
        self.setStyleSheet(APP_QSS)
        
        # Load configuration
        self.load_config()

        # Set up custom logging
        self.log_handler = QtLogHandler()
        self.log_handler.log_signal.connect(self.append_log)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.INFO)

        # UI Setup
        self.init_ui()

        # System Tray Icon Setup
        self.init_tray()

        # Start Solar Calculations thread
        self.solar_thread = SolarEngineThread(self.config_manager)
        self.solar_thread.solar_update.connect(self.on_solar_update)
        self.solar_thread.api_error.connect(lambda err: logging.warning(err))
        self.solar_thread.start()

        # Sequencer Thread Init
        self.restore_thread = None

        # Periodically check process statuses. The cadence follows the window's
        # visibility: every STATUS_POLL_INTERVAL_VISIBLE_MS while the dashboard
        # is on screen, and much less often while the app is only in the tray
        # (see showEvent/hideEvent). A window that starts straight into the tray
        # therefore never pays the visible cadence.
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.check_system_statuses)
        self._apply_status_polling(visible=False)

        # Immediate check at launch
        self.check_system_statuses()

        # Suspend deduplication timestamp (shared between WinPowerEventFilter and Win32PowerCallback)
        self._last_suspend_exec_time = 0.0
        self._sleep_transition_active = False
        self._system_sleeping = False

        # Watchdog timer — checks power detection health every 5 minutes
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.timeout.connect(self._watchdog_check)
        self._watchdog_timer.start(300000)  # 5 minutes

        logging.info("Yeelight PC Companion initialized. System running silently in tray.")

        # Report (never repair) the OpenRGB elevation state: waking the PC must
        # stay unattended, so a missing or stale launch task is surfaced in
        # Settings and in the log instead of raising a UAC prompt from the tray.
        self._log_openrgb_elevation_state()
        
        # Trigger automatic restoration sequence on startup/boot
        if auto_restore and not self._automation_paused:
            self.trigger_resume()
        else:
            logging.info("Startup restoration skipped by command-line flag.")

    def load_config(self):
        """Load (and migrate in memory) the active configuration.

        A failed *reload* keeps the last known-good configuration already in
        memory: a transient read/JSON/validation error must never silently
        disable integrations or blank device addresses. The canonical defaults
        are only used when no valid configuration has ever been loaded, so the
        UI can still render instead of crashing while the user fixes the file
        through the wizard or Settings UI.

        Returns True when the configuration was reloaded successfully.
        """
        try:
            loaded = self.config_manager.load()
        except ConfigError as exc:
            logging.error(f"[CONFIG] Could not load configuration: {exc}")
            if hasattr(self, "config"):
                logging.warning(
                    "[CONFIG] Keeping the last known-good configuration that is already in memory."
                )
            else:
                logging.warning(
                    "[CONFIG] No configuration has ever been loaded; using defaults in memory."
                )
                self.config = self.config_manager.default_config()
            return False
        self.config = loaded
        return True

    def save_config_file(self):
        """Validate and atomically write the in-memory configuration."""
        self.config_manager.save(self.config)
        logging.info("Configuration saved successfully.")

    def init_ui(self):
        """Build the window: a navigation sidebar plus one stacked page each.

        Presentation only. Every attribute the automation engine, the tray and
        the save/import paths touch keeps its name and meaning (`lbl_system_status`,
        `service_badges`, `device_list`, `integration_widgets`, the location and
        automation fields, `log_display`); only their placement changed.
        """
        _apply_process_theme()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        root = QHBoxLayout(central_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(SPACE_XL, SPACE_L, SPACE_XL, SPACE_L)
        content_layout.setSpacing(SPACE_L)
        content_layout.addLayout(self._build_page_header())

        self.page_index = {}
        self.pages = QStackedWidget()
        self.setup_overview_page()
        self.setup_devices_page()
        self.setup_integrations_page()
        self.setup_automation_page()
        self.setup_logs_page()
        content_layout.addWidget(self.pages, 1)

        self.action_bar = self._build_action_bar()
        content_layout.addWidget(self.action_bar)

        root.addWidget(content, 1)

        # Fill every configuration control from the loaded configuration, and
        # refresh the reported OpenRGB elevation state (read-only inspection).
        self._populate_settings_widgets(self.config)
        self._refresh_openrgb_elevation_status()
        self._refresh_dashboard_labels()
        self.select_page(DEFAULT_PAGE)
        self.resize(1120, 720)

    # ---------------------------------------------------------
    # Window chrome: sidebar, page header, shared action area
    # ---------------------------------------------------------
    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(212)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(SPACE_M, SPACE_L, SPACE_M, SPACE_L)
        layout.setSpacing(SPACE_XS)

        app_name = QLabel("Yeelight PC Companion")
        app_name.setObjectName("app_name")
        app_name.setWordWrap(True)
        layout.addWidget(app_name)

        app_section = QLabel("LIGHT ORCHESTRATOR")
        app_section.setObjectName("app_section")
        layout.addWidget(app_section)
        layout.addSpacing(SPACE_L)

        self.nav_buttons = {}
        for name in NAV_PAGES:
            button = SidebarButton(name)
            button.clicked.connect(lambda _checked=False, page=name: self.select_page(page))
            self.nav_buttons[name] = button
            layout.addWidget(button)

        layout.addStretch(1)
        layout.addWidget(hint_label("Closing this window keeps the app running in the tray."))
        return sidebar

    def _build_page_header(self):
        """Page title/subtitle on the left, the live system status on the right."""
        header = QHBoxLayout()
        header.setSpacing(SPACE_M)

        title_column = QVBoxLayout()
        title_column.setSpacing(2)
        self.lbl_page_title = QLabel(DEFAULT_PAGE)
        self.lbl_page_title.setObjectName("page_title")
        self.lbl_page_subtitle = QLabel(PAGE_SUBTITLES.get(DEFAULT_PAGE, ""))
        self.lbl_page_subtitle.setObjectName("page_subtitle")
        self.lbl_page_subtitle.setWordWrap(True)
        title_column.addWidget(self.lbl_page_title)
        title_column.addWidget(self.lbl_page_subtitle)

        # Compatibility hook: `trigger_suspend()`/`trigger_resume()`/
        # `on_resume_completed()` keep updating this label through
        # `_set_system_status()`, exactly as they always did.
        self.lbl_system_status = StatusPill(SYSTEM_ACTIVE_TEXT, TONE_OK)
        self.lbl_system_status.setToolTip(
            "Reported by the sleep/wake handlers: 'Suspending' and 'Restoring' appear during a transition."
        )

        header.addLayout(title_column, 1)
        header.addWidget(self.lbl_system_status, 0, Qt.AlignmentFlag.AlignTop)
        return header

    def _build_action_bar(self):
        """The single action area for configuration changes.

        One primary action instead of a large button per page; Import and Export
        stay visually secondary to it.
        """
        bar = QFrame()
        bar.setObjectName("card")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(SPACE_L, SPACE_M, SPACE_L, SPACE_M)
        layout.setSpacing(SPACE_S)
        layout.addStretch(1)

        self.btn_import = QPushButton("Import Configuration")
        self.btn_import.clicked.connect(self.import_config_from_file)
        self.btn_export = QPushButton("Export Configuration")
        self.btn_export.clicked.connect(self.export_config_to_file)
        self.btn_save = QPushButton("Save Changes")
        self.btn_save.setObjectName("btn_primary")
        self.btn_save.setToolTip("Validate every setting and save the configuration file.")
        self.btn_save.clicked.connect(self.save_settings_from_gui)

        layout.addWidget(self.btn_import, 0)
        layout.addWidget(self.btn_export, 0)
        layout.addWidget(self.btn_save, 0)
        return bar

    def _add_page(self, name, widget):
        """Register a page in the stack and remember its index."""
        self.page_index[name] = self.pages.addWidget(widget)
        return widget

    def select_page(self, name):
        """Show a page and keep the sidebar, header and action area in step.

        Returns False for an unknown page so a caller cannot leave the window in
        a half-selected state.
        """
        index = self.page_index.get(name)
        button = self.nav_buttons.get(name)
        if index is None or button is None:
            return False
        button.setChecked(True)
        self.pages.setCurrentIndex(index)
        self.lbl_page_title.setText(name)
        self.lbl_page_subtitle.setText(PAGE_SUBTITLES.get(name, ""))
        self.action_bar.setVisible(name in PAGES_WITH_ACTION_BAR)
        if name == PAGE_LOGS:
            self.log_display.moveCursor(QTextCursor.MoveOperation.End)
        return True

    # ---------------------------------------------------------
    # Page: Overview
    # ---------------------------------------------------------
    def setup_overview_page(self):
        """Is everything okay, is it day or night, what is controlled, can I resync?"""
        body, layout = page_body()

        # --- environment / automation summary ---------------------------
        card_env = SectionCard(
            "Environment & automation",
            "Calculated locally from the Automation coordinates; nothing is uploaded.",
        )

        solar_row = QHBoxLayout()
        solar_row.setSpacing(SPACE_M)
        self.lbl_sun_state = StatusPill("Calculating...", TONE_NEUTRAL)
        solar_row.addWidget(self.lbl_sun_state, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_action_req = QLabel("None")
        self.lbl_action_req.setStyleSheet(text_qss(TONE_NEUTRAL, weight=600, size=14))
        solar_row.addWidget(self.lbl_action_req, 1)
        card_env.add_layout(solar_row)

        facts = QGridLayout()
        facts.setHorizontalSpacing(SPACE_L)
        facts.setVerticalSpacing(SPACE_S)
        facts.setColumnStretch(1, 1)

        facts.addWidget(field_label("Yeelight devices"), 0, 0)
        self.lbl_device_summary = QLabel("Calculating...")
        self.lbl_device_summary.setObjectName("value_strong")
        self.lbl_device_summary.setWordWrap(True)
        facts.addWidget(self.lbl_device_summary, 0, 1)

        # Coordinates stay secondary detail: present, muted, never the focus.
        facts.addWidget(field_label("Coordinates"), 1, 0)
        coordinates = QHBoxLayout()
        coordinates.setSpacing(SPACE_XS)
        self.lbl_lat = muted_label("")
        self.lbl_lon = muted_label("")
        coordinates.addWidget(muted_label("Latitude"))
        coordinates.addWidget(self.lbl_lat)
        coordinates.addSpacing(SPACE_M)
        coordinates.addWidget(muted_label("Longitude"))
        coordinates.addWidget(self.lbl_lon)
        coordinates.addStretch(1)
        facts.addLayout(coordinates, 1, 1)

        card_env.add_layout(facts)

        # --- service health ---------------------------------------------
        card_services = SectionCard("Service health")
        self.service_badges = {}
        for label, process_name, detail in SERVICE_ROWS:
            row = StatusRow(label, detail)
            row.pill.setToolTip(f"{process_name} - reported only as running or stopped.")
            self.service_badges[process_name] = row.pill
            card_services.add_widget(row)
        card_services.add_hint(
            "Checked every three seconds while this window is open, and much less often while "
            "the app is only in the tray. Disabled integrations are skipped, so 'Stopped' is "
            "normal for them."
        )

        # --- manual actions ---------------------------------------------
        card_actions = SectionCard(
            "Manual actions",
            "These run this app's sleep or wake steps immediately instead of waiting for a power event.",
        )
        action_row = QHBoxLayout()
        action_row.setSpacing(SPACE_M)

        self.btn_force_sync = QPushButton("Force System Sync")
        self.btn_force_sync.setObjectName("btn_primary")
        self.btn_force_sync.setToolTip(
            "Runs the wake/restore sequence now: stops stale processes, starts the enabled "
            "integrations and applies the current day/night state."
        )
        self.btn_force_sync.clicked.connect(self.trigger_resume)

        self.btn_sleep_actions = QPushButton("Run Sleep Actions")
        self.btn_sleep_actions.setObjectName("btn_warning")
        self.btn_sleep_actions.setToolTip(
            "Runs this app's sleep steps now: closes the light-control applications and switches the "
            "Yeelight devices off.\n\nIt does NOT put Windows to sleep."
        )
        self.btn_sleep_actions.clicked.connect(self.trigger_suspend)

        action_row.addWidget(self.btn_force_sync, 0)
        action_row.addWidget(self.btn_sleep_actions, 0)
        action_row.addStretch(1)
        card_actions.add_layout(action_row)
        card_actions.add_hint(
            "Run Sleep Actions performs this app's sleep steps only - Windows itself is not suspended."
        )

        layout.addWidget(card_env)
        layout.addWidget(card_services)
        layout.addWidget(card_actions)
        layout.addStretch(1)

        self._add_page(PAGE_OVERVIEW, scrollable(body))

    # ---------------------------------------------------------
    # Page: Devices
    # ---------------------------------------------------------
    def setup_devices_page(self):
        """Device management, moved out of the old single settings form."""
        body, layout = page_body()

        card = SectionCard(
            "Yeelight Devices",
            "Every device listed here is controlled on your local network.",
        )
        self.btn_discover = QPushButton("Discover")
        self.btn_discover.setToolTip("Search the local network for Yeelight devices (LAN Control must be enabled on them).")
        self.btn_add_device = QPushButton("Add Manually")
        self.btn_add_device.setToolTip("Add a device by name and address.")
        card.add_action(self.btn_discover)
        card.add_action(self.btn_add_device)

        # The list widget keeps its own device logic and its own dialogs; this
        # page only hosts it and forwards the two entry points.
        self.device_list = DeviceListWidget(show_actions=False)
        self.btn_discover.clicked.connect(self.device_list.on_discover)
        self.btn_add_device.clicked.connect(self.device_list.on_add_manually)
        card.add_widget(self.device_list)
        card.add_hint(
            "Disable a device to leave it alone, or remove it to stop controlling it entirely. "
            "Enabling or disabling takes effect when you save."
        )

        layout.addWidget(card)
        layout.addStretch(1)

        self._add_page(PAGE_DEVICES, scrollable(body))

    # ---------------------------------------------------------
    # Page: Integrations
    # ---------------------------------------------------------
    def setup_integrations_page(self):
        """One card per integration: enable flag, executable path, Browse."""
        body, layout = page_body()

        layout.addWidget(
            hint_label(
                "Each application is optional. A disabled integration is never launched, stopped, "
                "waited for or used for self-healing - disable or uninstall anything you do not use."
            )
        )

        self.integration_widgets = {}
        self.integration_cards = {}

        for key in INTEGRATION_KEYS:
            meta = INTEGRATIONS[key]
            card = IntegrationCard(
                meta["label"],
                INTEGRATION_DESCRIPTIONS.get(key, ""),
                f"Path to {meta['hint']}",
                with_action=(key == "openrgb"),
            )
            card.btn_browse.clicked.connect(
                lambda _checked=False, target=card.txt_path: self.browse_for_executable(target)
            )
            if key == "openrgb":
                # OpenRGB is the one application that needs administrator
                # rights; its one-time seamless launch setup is presented here.
                self.lbl_openrgb_elevation = card.lbl_status
                self.btn_openrgb_elevation = card.btn_action
                self.lbl_openrgb_elevation_hint = card.lbl_hint
                self.btn_openrgb_elevation.clicked.connect(self.repair_openrgb_elevation)
            self.integration_cards[key] = card
            self.integration_widgets[key] = (card.chk_enabled, card.txt_path, card.btn_browse)
            layout.addWidget(card)

        layout.addWidget(
            hint_label(
                "An enabled application that is not installed is reported as a note when you save and "
                "is skipped at runtime until it appears; the remaining steps still run."
            )
        )
        layout.addStretch(1)

        self._add_page(PAGE_INTEGRATIONS, scrollable(body))

    # ---------------------------------------------------------
    # Page: Automation
    # ---------------------------------------------------------
    def setup_automation_page(self):
        """Sleep/wake behaviour, solar location, Razer handling, compatibility."""
        body, layout = page_body()

        # --- Sleep & wake ------------------------------------------------
        card_sleep = SectionCard(
            "Sleep & wake",
            "Runs in the background; the sleep steps are kept short so they finish before the PC freezes.",
        )
        self.chk_close_apps = QCheckBox("Close the light-control applications when the PC sleeps")
        self.chk_close_apps.setToolTip(
            "Stops the Yeelight Chroma Connector first (it holds the devices), then OpenRGB and "
            "Artemis - each one only while its integration is enabled."
        )
        self.chk_turn_off_yeelight = QCheckBox("Turn the Yeelight devices off when the PC sleeps")
        self.chk_turn_off_yeelight.setToolTip(
            "Sent as one bounded batch to every enabled device, so an unreachable device cannot "
            "delay the others."
        )
        self.chk_restore_apps = QCheckBox("Restore the applications when the PC wakes")
        self.chk_restore_apps.setToolTip(
            "Runs the wake sequence in the background: starts the enabled integrations and applies "
            "the current day/night state."
        )
        card_sleep.add_widget(self.chk_close_apps)
        card_sleep.add_widget(self.chk_turn_off_yeelight)
        card_sleep.add_widget(self.chk_restore_apps)

        # --- Solar / location -------------------------------------------
        card_solar = SectionCard(
            "Solar & location",
            "Used locally to calculate sunrise and sunset. Nothing is uploaded.",
        )
        solar_form = QGridLayout()
        solar_form.setHorizontalSpacing(SPACE_L)
        solar_form.setVerticalSpacing(SPACE_S)
        # An empty trailing column absorbs the window width, so the fields stay
        # next to their labels instead of drifting to the right edge.
        solar_form.setColumnStretch(2, 1)

        self.txt_lat = QLineEdit()
        self.txt_lon = QLineEdit()
        self.txt_elev = QLineEdit()
        self.txt_buf = QLineEdit()
        # Short numeric values: cap the fields so a wide window does not turn
        # them into long empty boxes.
        for field in (self.txt_lat, self.txt_lon, self.txt_elev, self.txt_buf):
            field.setMaximumWidth(200)

        solar_form.addWidget(field_label("Latitude (-90 to 90)"), 0, 0)
        solar_form.addWidget(self.txt_lat, 0, 1)
        solar_form.addWidget(field_label("Longitude (-180 to 180)"), 1, 0)
        solar_form.addWidget(self.txt_lon, 1, 1)
        solar_form.addWidget(field_label("Elevation in metres"), 2, 0)
        solar_form.addWidget(self.txt_elev, 2, 1)
        solar_form.addWidget(field_label("Sunrise/sunset buffer in hours"), 3, 0)
        solar_form.addWidget(self.txt_buf, 3, 1)
        card_solar.add_layout(solar_form)
        card_solar.add_hint(
            "A buffer of 1-2 hours switches the devices on slightly before sunset and keeps them on "
            "slightly after sunrise. The coordinates never leave this computer."
        )

        # --- Razer Synapse behaviour ------------------------------------
        card_synapse = SectionCard(
            "Razer Synapse behaviour",
            "Only applies while the Razer Synapse integration is enabled.",
        )
        self.chk_launch_synapse = QCheckBox("Launch Razer Synapse directly on wake (it takes screen focus)")
        self.chk_wait_synapse = QCheckBox("Alternatively wait/poll for Synapse started by the system")
        self.txt_synapse_timeout = QLineEdit()
        self.txt_synapse_timeout.setMaximumWidth(120)

        synapse_form = QGridLayout()
        synapse_form.setHorizontalSpacing(SPACE_L)
        synapse_form.setVerticalSpacing(SPACE_S)
        synapse_form.addWidget(self.chk_launch_synapse, 0, 0, 1, 2)
        synapse_form.addWidget(self.chk_wait_synapse, 1, 0, 1, 2)

        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(SPACE_S)
        timeout_row.addWidget(field_label("Detection timeout (seconds)"))
        timeout_row.addWidget(self.txt_synapse_timeout)
        timeout_row.addStretch(1)
        synapse_form.addLayout(timeout_row, 2, 0, 1, 2)
        card_synapse.add_layout(synapse_form)
        self.lbl_synapse_dependency = hint_label("")
        card_synapse.add_widget(self.lbl_synapse_dependency)

        # --- Compatibility ----------------------------------------------
        card_compat = SectionCard(
            "Compatibility",
            "Settings kept for existing configurations.",
        )
        self.chk_turn_on_yeelight = QCheckBox(
            "Turn the Yeelight devices on when the PC wakes (retained setting)"
        )
        self.chk_turn_on_yeelight.setToolTip(
            "Stored, validated and saved exactly as before, but no runtime code path currently reads it."
        )
        card_compat.add_widget(self.chk_turn_on_yeelight)
        card_compat.add_hint(
            "This setting is retained and saved, but it is not currently acted upon. The same applies "
            "to 'force_silent_launch', which is preserved in the configuration without a control here."
        )

        layout.addWidget(card_sleep)
        layout.addWidget(card_solar)
        layout.addWidget(card_synapse)
        layout.addWidget(card_compat)
        layout.addStretch(1)

        self._add_page(PAGE_AUTOMATION, scrollable(body))

    # ---------------------------------------------------------
    # Seamless elevated OpenRGB launch (Windows scheduled task)
    # ---------------------------------------------------------
    def _refresh_openrgb_elevation_status(self):
        """Update the Integrations card indicator for the seamless elevated launch.

        Returns the status object, or None when it could not be determined.
        The indicator always describes the *saved* configuration, because the
        launch task is derived from it.
        """
        try:
            enabled = integration_enabled(self.config, "openrgb")
            status = openrgb_elevation_status(
                integration_path(self.config, "openrgb"),
                integration_is_enabled=enabled,
            )
        except Exception:
            logging.exception("[OPENRGB] Could not determine the elevated launch status.")
            self._openrgb_elevation_status = None
            self.lbl_openrgb_elevation.set_status("Unavailable", TONE_WARN)
            self.lbl_openrgb_elevation_hint.setText(
                "The Windows launch task could not be inspected. See the debug log for details."
            )
            self.btn_openrgb_elevation.setText(SET_UP_ACTION_LABEL)
            self.btn_openrgb_elevation.setEnabled(False)
            return None

        self._openrgb_elevation_status = status

        # Ready / Needs setup / Unsafe / Unavailable / Not used: the status text
        # itself says which one it is, the tone only reinforces it.
        if status.is_ready:
            tone = TONE_OK
        elif status.state == STATUS_DISABLED:
            tone = TONE_NEUTRAL
        else:
            tone = TONE_WARN
        self.lbl_openrgb_elevation.set_status(status.label, tone)

        if status.action_label:
            self.btn_openrgb_elevation.setText(status.action_label)
            self.btn_openrgb_elevation.setEnabled(enabled)
        else:
            self.btn_openrgb_elevation.setText(SET_UP_ACTION_LABEL)
            self.btn_openrgb_elevation.setEnabled(False)

        hint = status.detail
        if status.needs_action:
            hint += (
                " This applies to the saved OpenRGB path, so save your changes first "
                "if you just edited it."
            )
        self.lbl_openrgb_elevation_hint.setText(hint)
        return status

    def _log_openrgb_elevation_state(self):
        """Report (never repair) the elevation state at startup.

        Waking the PC must stay unattended, so a missing or stale launch task is
        surfaced in Settings and in the log instead of raising a UAC prompt from
        the tray.
        """
        status = getattr(self, "_openrgb_elevation_status", None)
        if status is None or status.state == STATUS_DISABLED:
            return
        if status.is_ready:
            logging.info("[OPENRGB] Seamless elevated launch is ready.")
            return
        logging.warning(
            "[OPENRGB] OpenRGB seamless elevated launch is not ready (%s). OpenRGB is "
            "skipped after wake until it is set up on the Integrations page.",
            status.label,
        )

    def _confirm_openrgb_approval(self):
        """Explain and confirm the one-time administrator approval."""
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
        return answer == QMessageBox.StandardButton.Yes

    def repair_openrgb_elevation(self):
        """Explicitly set up or repair the elevated OpenRGB launch task.

        This is the only user-triggered place that may request administrator
        approval; the automatic wake path never does.
        """
        try:
            self._repair_openrgb_elevation()
        except Exception:
            logging.exception("[OPENRGB] Unexpected error while setting up the launch task.")
            QMessageBox.critical(
                self,
                "OpenRGB elevated launch",
                "An unexpected error occurred while setting up the OpenRGB launch task.\n\n"
                "See the debug log for details.",
            )
        finally:
            self._refresh_openrgb_elevation_status()

    def _repair_openrgb_elevation(self):
        if not integration_enabled(self.config, "openrgb"):
            QMessageBox.information(
                self,
                "OpenRGB elevated launch",
                "Enable the OpenRGB integration first, save the settings, then set up the "
                "seamless launch.",
            )
            return

        openrgb_path = integration_path(self.config, "openrgb")
        if not openrgb_path:
            QMessageBox.warning(
                self,
                "OpenRGB elevated launch",
                "Set the OpenRGB executable path first, save the settings, then set up the "
                "seamless launch.",
            )
            return

        # An OpenRGB that this user account can modify can never be given to a
        # privileged task. Explain that instead of asking for an approval that
        # is going to be refused anyway.
        status = self._refresh_openrgb_elevation_status()
        if status is not None and status.state == STATUS_UNSAFE_TARGET:
            QMessageBox.warning(self, "OpenRGB elevated launch", status.detail)
            return

        if not self._confirm_openrgb_approval():
            return

        ok, message = apply_openrgb_task_action(ACTION_PROVISION, openrgb_path)
        if ok:
            QMessageBox.information(
                self,
                "OpenRGB elevated launch",
                message or "OpenRGB seamless elevated launch is ready.",
            )
        else:
            QMessageBox.warning(
                self,
                "OpenRGB elevated launch",
                f"{message}\n\nYou can try again from the Integrations page at any time. OpenRGB is "
                "skipped after wake until this succeeds.",
            )

    def _sync_openrgb_elevation_task(self, previous):
        """Keep the Windows launch task in step with the saved configuration.

        Enabling OpenRGB or changing its executable path is explicit
        configuration activity, so this is the moment to create or update the
        task - including the one administrator approval it may need. The
        background and wake-time paths never request elevation.
        """
        enabled_before, path_before = previous
        enabled_after = integration_enabled(self.config, "openrgb")
        path_after = integration_path(self.config, "openrgb")

        action = openrgb_task_action(enabled_before, path_before, enabled_after, path_after)
        if action == ACTION_NONE:
            return
        if action == ACTION_PROVISION and not self._confirm_openrgb_approval():
            logging.info(
                "[OPENRGB] One-time approval for the seamless launch was not granted; "
                "OpenRGB stays configured and can be repaired from the Integrations page."
            )
            return

        ok, message = apply_openrgb_task_action(action, path_after)
        if not ok and message:
            QMessageBox.warning(self, "OpenRGB elevated launch", message)

    # ---------------------------------------------------------
    # Settings UI <-> configuration mapping
    # ---------------------------------------------------------
    def browse_for_executable(self, line_edit):
        """Native file picker for an integration executable path."""
        current = line_edit.text().strip()
        start_dir = ""
        if current:
            candidate = os.path.dirname(current)
            if os.path.isdir(candidate):
                start_dir = candidate
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Select executable", start_dir, "Programs (*.exe);;All files (*)"
        )
        if path:
            line_edit.setText(path)

    def _populate_settings_widgets(self, config):
        """Fill every Settings control from a configuration dictionary."""
        location = config["location"]
        self.txt_lat.setText(str(location.get("latitude", "")))
        self.txt_lon.setText(str(location.get("longitude", "")))
        self.txt_elev.setText(str(location.get("elevation", "")))
        self.txt_buf.setText(str(location.get("light_buffer_hours", "")))

        self.device_list.set_devices(configured_devices(config))

        for key, (chk_enabled, txt_path, btn_browse) in self.integration_widgets.items():
            enabled = bool(config["integrations"][key]["enabled"])
            chk_enabled.setChecked(enabled)
            txt_path.setText(str(config["paths"].get(key, "") or ""))
            txt_path.setEnabled(enabled)
            btn_browse.setEnabled(enabled)

        automation = config["automation"]
        self.chk_close_apps.setChecked(bool(automation.get("close_apps_on_sleep", True)))
        self.chk_turn_off_yeelight.setChecked(bool(automation.get("turn_off_yeelight_on_sleep", True)))
        self.chk_restore_apps.setChecked(bool(automation.get("restore_apps_on_wake", True)))
        self.chk_turn_on_yeelight.setChecked(bool(automation.get("turn_on_yeelight_on_wake_night", True)))
        self.chk_launch_synapse.setChecked(bool(automation.get("launch_razer_synapse", False)))
        self.chk_wait_synapse.setChecked(bool(automation.get("wait_for_razer_synapse", True)))
        self.txt_synapse_timeout.setText(
            str(int(float(automation.get("razer_synapse_timeout_seconds", 45))))
        )

        self._refresh_razer_dependency_note(config)

    def _refresh_razer_dependency_note(self, config):
        """Explain the Razer dependency of the Automation options.

        Presentation only: it reads the same enable flag the runtime reads and
        changes nothing about how the options are collected or saved.
        """
        if integration_enabled(config, "razer_synapse"):
            tone = TONE_NEUTRAL
            text = "Razer Synapse is enabled, so these options are in effect on wake."
        else:
            tone = TONE_WARN
            text = (
                "Razer Synapse is disabled on the Integrations page, so these options have no "
                "effect until you enable it there."
            )
        self.lbl_synapse_dependency.setText(text)
        self.lbl_synapse_dependency.setStyleSheet(text_qss(tone, weight=500, size=12))

    def _collect_settings_config(self):
        """Build a configuration from the Settings controls.

        Unknown/extra keys from the loaded configuration are preserved, so this
        never silently drops settings written by another version.
        """
        config = copy.deepcopy(self.config) if isinstance(self.config, dict) else self.config_manager.default_config()
        config["config_version"] = CONFIG_VERSION

        for section in ("location", "lights", "paths", "integrations", "automation"):
            if not isinstance(config.get(section), dict):
                config[section] = copy.deepcopy(self.config_manager.default_config()[section])

        config["location"].update(
            {
                "latitude": self.txt_lat.text().strip(),
                "longitude": self.txt_lon.text().strip(),
                "elevation": self.txt_elev.text().strip(),
                "light_buffer_hours": self.txt_buf.text().strip(),
            }
        )
        config["lights"]["devices"] = self.device_list.devices()
        for key, (chk_enabled, txt_path, _btn_browse) in self.integration_widgets.items():
            config["integrations"].setdefault(key, {})["enabled"] = chk_enabled.isChecked()
            config["paths"][key] = txt_path.text().strip()

        config["automation"].update(
            {
                "close_apps_on_sleep": self.chk_close_apps.isChecked(),
                "turn_off_yeelight_on_sleep": self.chk_turn_off_yeelight.isChecked(),
                "restore_apps_on_wake": self.chk_restore_apps.isChecked(),
                "turn_on_yeelight_on_wake_night": self.chk_turn_on_yeelight.isChecked(),
                "launch_razer_synapse": self.chk_launch_synapse.isChecked(),
                "wait_for_razer_synapse": self.chk_wait_synapse.isChecked(),
                "razer_synapse_timeout_seconds": self.txt_synapse_timeout.text().strip(),
            }
        )
        return config

    def _refresh_dashboard_labels(self):
        self.lbl_lat.setText(str(self.config["location"].get("latitude", "")))
        self.lbl_lon.setText(str(self.config["location"].get("longitude", "")))
        self._refresh_device_summary()

    def _refresh_device_summary(self):
        """The factual device line: an arbitrary number of devices, no fixed slots."""
        self.lbl_device_summary.setText(device_count_summary(self.config))

    def _restart_solar_thread(self):
        self.solar_thread.stop()
        self.solar_thread = SolarEngineThread(self.config_manager)
        self.solar_thread.solar_update.connect(self.on_solar_update)
        self.solar_thread.api_error.connect(lambda err: logging.warning(err))
        self.solar_thread.start()

    # ---------------------------------------------------------
    # Page: Logs
    # ---------------------------------------------------------
    def setup_logs_page(self):
        """The in-app log view; file logging is unchanged.

        The view is kept to the most recent ``UI_LOG_MAX_BLOCKS`` lines by
        `_trim_log_display`; the rotating file log is unaffected because it has
        its own size cap.
        """
        body, layout = page_body()

        card = SectionCard(
            "System Logs",
            "Output of the current session. The rotating debug log file is unchanged.",
        )
        self.btn_clear_logs = QPushButton("Clear")
        self.btn_clear_logs.setToolTip("Clears this view only; the log file on disk is not touched.")
        card.add_action(self.btn_clear_logs)

        self.log_display = QTextEdit()
        self.log_display.setObjectName("log_display")
        self.log_display.setReadOnly(True)
        self.log_display.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_display.setMinimumHeight(320)
        card.add_widget(self.log_display)

        self.btn_clear_logs.clicked.connect(self.log_display.clear)

        layout.addWidget(card, 1)
        # Deliberately not wrapped in a scroll area: the log view itself scrolls
        # and should use the full page height.
        self._add_page(PAGE_LOGS, body)

    # ---------------------------------------------------------
    # System Tray Integration
    # ---------------------------------------------------------
    def init_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        
        # Look for a custom icon in the application directory
        app_dir = get_app_dir()
        custom_icon_path = None
        for filename in ["yeelight_pc_companion.ico", "yeelight_pc_companion.png", "icon.ico", "icon.png"]:
            p = os.path.join(app_dir, filename)
            if os.path.exists(p):
                custom_icon_path = p
                break
                
        if custom_icon_path:
            self.tray_icon.setIcon(QIcon(custom_icon_path))
            # QWindow Icon too
            self.setWindowIcon(QIcon(custom_icon_path))
            logging.info(f"Loaded custom tray icon from: {os.path.basename(custom_icon_path)}")
        else:
            # Fall back to a standard system icon
            from PyQt6.QtWidgets import QStyle
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
            
        self.tray_icon.setToolTip("Yeelight PC Companion - Light Orchestrator")
        
        # Menu
        tray_menu = QMenu()
        
        action_open = QAction("Open Dashboard", self)
        action_open.triggered.connect(self.showNormal)
        
        action_sync = QAction("Force System Sync", self)
        action_sync.triggered.connect(self.trigger_resume)
        
        action_off = QAction("Simulate Sleep (Turn Off)", self)
        action_off.triggered.connect(self.trigger_suspend)
        
        action_exit = QAction("Exit Yeelight PC Companion", self)
        action_exit.triggered.connect(self.exit_app)
        
        tray_menu.addAction(action_open)
        tray_menu.addSeparator()
        tray_menu.addAction(action_sync)
        tray_menu.addAction(action_off)
        tray_menu.addSeparator()
        tray_menu.addAction(action_exit)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        
        # Double click opens dashboard
        self.tray_icon.activated.connect(self.on_tray_activated)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.showNormal()
            self.activateWindow()

    # ---------------------------------------------------------
    # Core Logic Slot Calls
    # ---------------------------------------------------------
    def append_log(self, message, level):
        self.log_display.append(message)
        # Scroll to bottom
        self.log_display.moveCursor(QTextCursor.MoveOperation.End)
        self._trim_log_display()

    def _trim_log_display(self):
        """Keep the in-app log view bounded, in batches.

        Called after every appended line, but only acts once the document has
        grown a whole batch past the cap - removing the oldest blocks in one
        edit rather than one per line. See `UI_LOG_TRIM_BATCH` for why. "Clear"
        and the file log are unaffected.
        """
        document = self.log_display.document()
        if document.blockCount() <= UI_LOG_MAX_BLOCKS + UI_LOG_TRIM_BATCH:
            return
        excess = document.blockCount() - UI_LOG_MAX_BLOCKS
        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(
            QTextCursor.MoveOperation.NextBlock,
            QTextCursor.MoveMode.KeepAnchor,
            excess,
        )
        cursor.removeSelectedText()

    def save_settings_from_gui(self):
        """Validate the Settings form and save it atomically.

        Validation errors are explained in plain language; exceptions are never
        shown to the user (and must not escape this Qt slot).
        """
        try:
            self._save_settings_from_gui()
        except Exception:
            logging.exception("[CONFIG] Unexpected error while saving settings.")
            QMessageBox.critical(
                self,
                "Settings not saved",
                "An unexpected error occurred while saving the settings.\n\n"
                "Nothing was changed. See the debug log for details.",
            )

    def _save_settings_from_gui(self):
        try:
            config = self._collect_settings_config()
        except (ValueError, TypeError):
            QMessageBox.critical(
                self,
                "Invalid value",
                "One of the values in the form is not valid. Please check the coordinates, "
                "buffer hours and the Razer Synapse timeout.",
            )
            return

        result = validate_config(config)
        if result.errors:
            QMessageBox.critical(
                self,
                "Settings not saved",
                "Nothing was saved. Please correct the following:\n\n- " + "\n- ".join(result.errors),
            )
            return

        # Remember the saved OpenRGB state so the Windows launch task can be kept
        # in step with it after a successful save.
        previous_openrgb = (
            integration_enabled(self.config, "openrgb"),
            integration_path(self.config, "openrgb"),
        )

        try:
            self.config_manager.save(config)
        except (ConfigError, OSError) as exc:
            QMessageBox.critical(
                self,
                "Settings not saved",
                f"The configuration could not be written:\n\n{exc}",
            )
            return

        self.config = config
        self._refresh_dashboard_labels()
        self._restart_solar_thread()

        # Enabling OpenRGB or changing its path is explicit configuration
        # activity: this is where the privileged launch task is (re)created or
        # removed. The wake path itself never requests elevation.
        self._sync_openrgb_elevation_task(previous_openrgb)
        self._refresh_openrgb_elevation_status()

        logging.info("Applied changes to Solar Thread and IP Engine.")
        for warning in result.warnings:
            logging.warning("[CONFIG] %s", warning)

        if result.warnings:
            QMessageBox.warning(
                self,
                "Settings saved with notes",
                "Settings were saved, but please note:\n\n- " + "\n- ".join(result.warnings),
            )
        else:
            QMessageBox.information(self, "Success", "Settings applied and saved successfully.")

    # ---------------------------------------------------------
    # Import / Export configuration
    # ---------------------------------------------------------
    def import_config_from_file(self):
        """Import a configuration file, replacing the current one only if valid.

        Nothing is replaced before the imported file has validated, and no
        exception may escape this Qt slot.
        """
        try:
            self._import_config_from_file()
        except Exception:
            logging.exception("[CONFIG] Unexpected error while importing a configuration.")
            QMessageBox.critical(
                self,
                "Import failed",
                "An unexpected error occurred while importing that configuration.\n\n"
                "Nothing was changed. See the debug log for details.",
            )

    def _import_config_from_file(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Import configuration", "", "JSON configuration (*.json);;All files (*)"
        )
        if not path:
            return

        try:
            config = self.config_manager.import_config(path)
        except ConfigValidationError as exc:
            QMessageBox.critical(
                self,
                "Import cancelled",
                "The current configuration was not changed. Please fix the following and try again:\n\n- "
                + "\n- ".join(exc.errors),
            )
            return
        except ConfigError as exc:
            QMessageBox.critical(
                self, "Import cancelled", f"The current configuration was not changed.\n\n{exc}"
            )
            return
        except OSError as exc:
            QMessageBox.critical(
                self, "Import failed", f"The configuration could not be imported:\n\n{exc}"
            )
            return

        self.config = config
        self._populate_settings_widgets(config)
        self._refresh_dashboard_labels()
        self._restart_solar_thread()

        # Importing never requests elevation; it only reports the new task state
        # so the user can repair it from Settings.
        self._refresh_openrgb_elevation_status()

        logging.info("Configuration imported successfully (%s).", os.path.basename(path))
        QMessageBox.information(
            self,
            "Configuration imported",
            "The imported configuration is now active and the Devices, Integrations and Automation "
            "pages have been updated.\n\n"
            "The previous configuration was kept as config.json.bak.",
        )

    def export_config_to_file(self):
        """Export the active configuration to a portable JSON file (local only)."""
        try:
            self._export_config_to_file()
        except Exception:
            logging.exception("[CONFIG] Unexpected error while exporting the configuration.")
            QMessageBox.critical(
                self,
                "Export failed",
                "An unexpected error occurred while exporting the configuration.\n\n"
                "See the debug log for details.",
            )

    def _export_config_to_file(self):
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export configuration",
            "yeelight-pc-companion-config.json",
            "JSON configuration (*.json);;All files (*)",
        )
        if not path:
            return

        proceed = QMessageBox.warning(
            self,
            "Export configuration",
            "The exported file will contain your location coordinates, local device addresses "
            "and local executable paths.\n\nKeep it private - nothing is uploaded anywhere.\n\n"
            "Export now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if proceed != QMessageBox.StandardButton.Yes:
            return

        try:
            exported_path = self.config_manager.export_config(path, config=self.config)
        except (ConfigError, OSError) as exc:
            QMessageBox.critical(
                self, "Export failed", f"The configuration could not be exported:\n\n{exc}"
            )
            return

        logging.info("Configuration exported (%s).", os.path.basename(exported_path))
        QMessageBox.information(
            self, "Configuration exported", f"Configuration exported to:\n{exported_path}"
        )

    def on_solar_update(self, is_dark, status_text):
        # Amber marks the night state, the accent marks daytime. The text names
        # the state, so the colour is never the only signal.
        self.lbl_sun_state.set_status(status_text, TONE_WARN if is_dark else TONE_ACCENT)

        if is_dark:
            self.lbl_action_req.setText("Run Yeelight Connector")
            self.lbl_action_req.setStyleSheet(text_qss(TONE_WARN, weight=600, size=14))
        else:
            self.lbl_action_req.setText("Kill Connector & Turn Off")
            self.lbl_action_req.setStyleSheet(text_qss(TONE_ACCENT, weight=600, size=14))

        # --- Self-Healing & Reconciliation ---
        # This periodically runs when the solar engine updates (every 60s).
        # It ensures that if the system is in a mismatched state (e.g. due to wake timing, crash, or clock drift),
        # it will automatically heal and align to the correct solar state.
        self.reconcile_solar_state(is_dark)

    def reconcile_solar_state(self, is_dark):
        if self._automation_paused:
            logging.info("[SOLAR ENGINE] Reconciliation skipped because automation is paused.")
            return
        # Only reconcile if we are not currently running a restoration thread to avoid conflicts
        if self.restore_thread and self.restore_thread.isRunning():
            return
        if self._sleep_transition_active or self._system_sleeping:
            logging.info("[SOLAR ENGINE] Reconciliation skipped while system sleep is in progress.")
            return

        try:
            self.load_config()
        except Exception:
            pass
        self._refresh_device_summary()

        automation = self.config.get("automation", {})
        if not automation.get("restore_apps_on_wake", True):
            return

        # Self-healing is driven by the Yeelight Chroma Connector's state, so it
        # only applies while that integration is enabled. When it is disabled its
        # status is not used, and it is never killed or relaunched.
        if not integration_enabled(self.config, "yeelight_connector"):
            logging.debug("[SOLAR ENGINE] Reconciliation skipped: Yeelight Chroma Connector integration is disabled.")
            return

        connector_name = INTEGRATIONS["yeelight_connector"]["process"]
        is_running = self.is_process_running(connector_name)

        if is_dark:
            # If it is dark, the connector should be running.
            # If it's not running, trigger the restore sequence to boot it up and turn on the lights.
            if not is_running:
                # Prevent self-healing from spamming triggers in a tight loop if the connector fails to launch.
                # Allow self-healing at most once every 15 minutes to avoid flooding bulb TCP connections.
                now = time.time()
                if hasattr(self, "last_self_heal_time") and now - self.last_self_heal_time < 900:
                    return
                self.last_self_heal_time = now

                logging.info("[SOLAR ENGINE] Self-healing trigger: Nighttime active but Yeelight Chroma Connector is not running. Initiating sync...")
                self.trigger_resume()
        else:
            # If it is light, the connector should NOT be running.
            # If it is running, kill it and turn off the devices.
            if is_running:
                logging.info("[SOLAR ENGINE] Self-healing trigger: Daytime active but Yeelight Chroma Connector is running. Stopping and turning off lights...")
                self.kill_process_immediate(connector_name)

                # Turn the devices off synchronously after a brief delay. Only
                # enabled devices are targeted, and each one is isolated so an
                # offline device cannot block the others.
                device_ips = enabled_device_ips(self.config)
                if automation.get("turn_off_yeelight_on_sleep", True) and device_ips:
                    time.sleep(0.4)
                    self.turn_off_devices_immediately(device_ips)

    def check_system_statuses(self):
        # Runs on the status timer and on every window show, in a single fast,
        # leak-free pass. The native process listing is the only source of the
        # state; the interval is the only thing that depends on visibility.
        try:
            running_processes = get_running_processes_win32()
        except Exception:
            running_processes = set()

        for _label, proc, _detail in SERVICE_ROWS:
            is_running = proc.lower() in running_processes
            badge = self.service_badges.get(proc)
            if badge:
                # The text names the state; the tone only reinforces it.
                badge.set_status("Running" if is_running else "Stopped",
                                 TONE_OK if is_running else TONE_NEUTRAL)

    def _apply_status_polling(self, visible):
        """(Re)start the process-status timer for the current window visibility.

        Only the interval changes: the timer, its slot and the check itself stay
        exactly as they were, so nothing runs more often than before.
        """
        interval = (
            STATUS_POLL_INTERVAL_VISIBLE_MS if visible else STATUS_POLL_INTERVAL_HIDDEN_MS
        )
        self.status_timer.start(interval)

    def showEvent(self, event):
        """Refresh the service rows as they become visible, then resume cadence."""
        super().showEvent(event)
        # While the window was hidden the rows were refreshed at the slow
        # interval, so they may already be out of date. Refresh before they can
        # be looked at, and only then switch back to the normal cadence.
        was_slow = self.status_timer.interval() != STATUS_POLL_INTERVAL_VISIBLE_MS
        self._apply_status_polling(visible=True)
        if was_slow:
            self.check_system_statuses()

    def hideEvent(self, event):
        """Drop back to the slow cadence once the dashboard is out of sight."""
        super().hideEvent(event)
        self._apply_status_polling(visible=False)

    def is_process_running(self, name):
        try:
            return name.lower() in get_running_processes_win32()
        except Exception:
            return False

    # ---------------------------------------------------------
    # Event Handlers (Sleep / Wake Automations)
    # ---------------------------------------------------------
    def _set_system_status(self, text, tone):
        """Update the header status pill (the sleep/wake handlers' only UI work).

        Presentation only: the state machine, the deduplication and the action
        sequence are untouched. Kept as a single place so the three callers
        cannot drift apart in wording or colour.
        """
        self.lbl_system_status.set_status(text, tone)

    def trigger_suspend(self):
        """
        Executes suspend routine. Called from GUI thread (WinPowerEventFilter or button).
        Delegates to _execute_suspend_actions() which is thread-safe and deduplicated.
        """
        logging.info("[POWER STATE EVENT] SUSPEND SIGNAL RECEIVED (via GUI event filter)")
        self._set_system_status("Suspending", TONE_WARN)
        self._execute_suspend_actions()
        self.check_system_statuses()

    def _execute_suspend_actions(self):
        """
        Thread-safe suspend sequence. Can be called from ANY thread:
        - GUI thread (via WinPowerEventFilter / button)
        - System thread pool (via Win32PowerCallback)

        Must complete within ~1.5 seconds to beat the Windows 2-second freeze deadline.
        Uses timestamp-based deduplication to prevent double execution when both
        detectors fire for the same sleep event.
        """
        now = time.time()
        if now - self._last_suspend_exec_time < 8.0:
            logging.info("[SUSPEND] Skipping duplicate execution (already ran within 8s)")
            return
        self._last_suspend_exec_time = now
        self._sleep_transition_active = True
        self._system_sleeping = True
        started = time.perf_counter()
        if self.restore_thread and self.restore_thread.isRunning():
            logging.info("[SUSPEND] Cancelling active restoration thread before sleep.")
            self.restore_thread.running = False

        # Lightweight, read-only config refresh for this timing-critical path:
        # JSON read + migration/normalization only. It performs no validation,
        # so it never probes integration executable paths on disk, never writes
        # and never creates backups. If that read fails, keep using the
        # configuration already loaded in memory.
        try:
            config = self.config_manager.load_runtime()
        except Exception:
            config = self.config

        automation = config.get("automation", {})

        # Integration enable flags, read once from the already-loaded config.
        # No validation, executable-path probing or allocation is added to this
        # timing-critical path.
        use_connector = integration_enabled(config, "yeelight_connector")
        use_openrgb = integration_enabled(config, "openrgb")
        use_artemis = integration_enabled(config, "artemis")

        # Runtime-safe device projection: only the addresses of the *enabled*
        # configured devices, read straight out of the already-loaded
        # configuration. No discovery, no Yeelight library objects, no DNS, no
        # filesystem probes and no validation are added to this path.
        device_ips = enabled_device_ips(config)

        # STEP 1: Kill Yeelight Chroma Connector FIRST (~50ms)
        # This is the most critical step — it holds the music mode TCP lock on the bulbs.
        # While music mode is active, bulbs IGNORE turn_off commands on port 55443.
        if automation.get("close_apps_on_sleep", True) and use_connector:
            logging.info("[SUSPEND] Step 1: Killing Yeelight Chroma Connector (releases music mode)...")
            killed = terminate_processes_win32(INTEGRATIONS["yeelight_connector"]["process"])
            logging.info(f"[SUSPEND] Yeelight Chroma Connector terminate requests sent: {killed}")
            time.sleep(0.35)

        # STEP 2: Bounded parallel OFF fan-out (one budget for ALL devices)
        # The Connector is already dead, so music mode is released and the bulbs
        # accept commands on port 55443 again. Every enabled device is addressed
        # concurrently, non-blocking, under the single suspend network budget —
        # never one socket timeout per device, and never any verification
        # round-trip: a single fast command that works 95% of the time beats 3
        # verified attempts that get frozen at attempt 2 by the Windows power
        # manager. An unreachable device costs the batch nothing beyond its own
        # slot in that shared budget, and can never delay or break the others.
        if automation.get("turn_off_yeelight_on_sleep", True):
            logging.info(
                "[SUSPEND] Step 2: Fire-and-forget OFF, one bounded batch → %s",
                ", ".join(device_ips) if device_ips else "(no enabled device configured)",
            )
            try:
                fire_and_forget_off_devices(device_ips)
            except Exception:  # defensive: the sequence must reach STEP 3
                logging.exception("[SUSPEND] The Yeelight OFF fan-out failed; continuing.")

        # STEP 3: Kill remaining light controller processes (~100ms)
        if automation.get("close_apps_on_sleep", True):
            # OpenRGB is stopped through its own elevation task FIRST.
            #
            # The `YeelightPCCompanion-OpenRGB` task runs OpenRGB with
            # HighestAvailable, so the process is high-integrity. An unelevated
            # Yeelight PC Companion cannot reliably terminate it with
            # OpenProcess(PROCESS_TERMINATE); Task Scheduler, however, will end
            # the task's own process tree for the task's owner. This is verified
            # on real hardware (see project_memory.md §4b) and it is what keeps
            # "OpenRGB is stopped on sleep" true now that the application
            # itself no longer runs elevated.
            #
            # Only the task-owned instance can be stopped this way, so the
            # generic terminate below still runs afterwards and still handles a
            # directly-launched (same-integrity) OpenRGB.
            #
            # `end_openrgb_task()` owns ONE global monotonic deadline of
            # `END_TASK_STOP_BUDGET_SECONDS` (0.4 s, measured real result 85-108 ms)
            # covering its `schtasks /end`, its verification and every wait in
            # between; it does not query the task first. A failed, absent or
            # deadline-expired stop returns immediately and this sequence simply
            # continues — the ~1.5 s suspend target is a hard contract, and the
            # budgeted phases are asserted against it in `suspend_budgeted_seconds()`.
            if use_openrgb:
                try:
                    stop_started = time.perf_counter()
                    outcome = end_openrgb_task()
                    logging.info(
                        "[SUSPEND] OpenRGB elevation-task stop: %s (%.3fs)",
                        outcome,
                        time.perf_counter() - stop_started,
                    )
                except Exception:
                    logging.exception(
                        "[SUSPEND] The task-aware OpenRGB stop failed; falling back to "
                        "direct termination."
                    )

            controllers = []
            if use_artemis:
                controllers.append(INTEGRATIONS["artemis"]["process"])
            if use_openrgb:
                controllers.append(INTEGRATIONS["openrgb"]["process"])
            if controllers:
                logging.info("[SUSPEND] Step 3: Killing Artemis and OpenRGB...")
                killed = terminate_processes_win32(*controllers)
                logging.info(f"[SUSPEND] Controller terminate requests sent: {killed}")

            # Verify rather than assume. Direct termination above is expected to
            # lose against an elevated OpenRGB, so a surviving process is
            # reported explicitly instead of being silently accepted.
            if use_openrgb:
                try:
                    still_running = openrgb_process_running()
                    if still_running:
                        logging.warning(
                            "[SUSPEND] OpenRGB is still running after the suspend stop "
                            "attempt. If it was not started through the elevation task, "
                            "it cannot be stopped from this privilege level."
                        )
                except Exception:
                    pass

        elapsed = time.perf_counter() - started
        logging.info(f"[SUSPEND] Suspend sequence completed successfully in {elapsed:.3f}s.")

    def trigger_resume(self):
        """
        Launches async background thread to handle wake sequence.
        This prevents the main event loop from locking.
        """
        # Prevent starting duplicate threads or interrupting an ongoing restore
        if self.restore_thread and self.restore_thread.isRunning():
            logging.info("[POWER STATE EVENT] RESUME SIGNAL RECEIVED, but restoration is already running. Ignoring duplicate trigger.")
            return

        # Clean up old finished thread to prevent memory/resource leaks in PyQt
        if self.restore_thread:
            try:
                self.restore_thread.deleteLater()
            except Exception:
                pass
            self.restore_thread = None

        try:
            self.load_config()
        except Exception:
            pass
            
        self._sleep_transition_active = False
        self._system_sleeping = False
        automation = self.config.get("automation", {})
        if not automation.get("restore_apps_on_wake", True):
            logging.info("[POWER STATE EVENT] RESUME SIGNAL RECEIVED, but restoration is disabled in settings.")
            return

        logging.info("[POWER STATE EVENT] RESUME SIGNAL RECEIVED. RESTORING SERVICES...")
        self._set_system_status("Restoring...", TONE_ACCENT)
        
        # Start new Restoration thread
        self.restore_thread = RestoreEngineThread(self.config_manager)
        self.restore_thread.progress_update.connect(lambda msg: logging.info(msg))
        self.restore_thread.finished_sequence.connect(self.on_resume_completed)
        self.restore_thread.start()

    def on_resume_completed(self):
        self._set_system_status(SYSTEM_ACTIVE_TEXT, TONE_OK)
        self.check_system_statuses()

    def turn_off_devices_immediately(self, device_ips):
        """Synchronous, per-device turn-off used by the daytime self-healing.

        Each device is isolated: a device that is off the network is logged and
        skipped, and the remaining devices are still switched off.
        """
        for ip in device_ips or []:
            try:
                self.safe_turn_off_immediate(ip)
            except Exception as exc:
                logging.warning(
                    "[SOLAR ENGINE] Device %s could not be switched off; continuing with "
                    "the remaining devices: %s",
                    ip,
                    exc,
                )

    def safe_turn_off_immediate(self, ip):
        if not ip or not str(ip).strip():
            # No address for this device — nothing to send, no error.
            return
        ip = str(ip).strip()
        bulb = None
        try:
            from yeelight import Bulb
            bulb = Bulb(ip)
            # Try to stop music mode first to return bulb to standard state
            try:
                bulb.stop_music()
            except Exception:
                pass
            # Short connection timeout to prevent locking if offline
            bulb.turn_off()
            logging.info(f"Synchronous Off: bulb {ip} successfully turned off.")
        except Exception as e:
            logging.warning(f"Synchronous Off Failed for {ip}: {e}")
        finally:
            if bulb:
                try:
                    if bulb._Bulb__socket is not None:
                        bulb._Bulb__socket.close()
                except Exception:
                    pass

    def busy_wait(self, seconds):
        start = time.perf_counter()
        while time.perf_counter() - start < seconds:
            pass

    def fire_and_forget_off(self, ip):
        """Send one raw turn_off command to a single Yeelight bulb.

        Single-device convenience wrapper around the suspend-time batch sender
        (`fire_and_forget_off_devices`): the same raw `set_power off sudden`
        payload, the same global network budget and no DNS — it does not open a
        serial blocking connection of its own. The suspend sequence itself uses
        the batch sender directly, so no code path pays one socket timeout per
        device any more. Thread-safe: can be called from any thread.
        """
        return fire_and_forget_off_devices([ip])

    def _watchdog_check(self):
        """
        Periodic health check (every 5 minutes) to verify power detection systems are alive.
        Logs diagnostics to the rotating file log for post-mortem analysis.
        """
        issues = []

        # Check if the native event filter is still alive (GC protection)
        if not hasattr(self, '_power_filter') or self._power_filter is None:
            issues.append("WinPowerEventFilter has been garbage collected!")

        # Check if the Win32 power callback is still registered
        if hasattr(self, '_power_callback'):
            if not self._power_callback.is_registered():
                issues.append("Win32PowerCallback is NOT registered!")
        else:
            issues.append("Win32PowerCallback was never initialized!")

        if issues:
            for issue in issues:
                logging.error(f"[WATCHDOG] CRITICAL: {issue}")
        else:
            logging.debug("[WATCHDOG] All power detection systems healthy.")

    def kill_process_immediate(self, name):
        try:
            killed = terminate_processes_win32(name)
            if killed == 0:
                subprocess.run(["taskkill", "/f", "/im", name], creationflags=0x08000000, capture_output=True)
            logging.info(f"Process killed: {name}")
        except Exception as e:
            logging.warning(f"Failed to kill process {name}: {e}")

    # ---------------------------------------------------------
    # Close Events & Exit Behavior
    # ---------------------------------------------------------
    def closeEvent(self, event):
        # Override close event to minimize to tray instead
        if self.tray_icon.isVisible():
            self.hide()
            logging.info("Yeelight PC Companion minimized to tray. Double click icon to open dashboard.")
            event.ignore()
        else:
            self.exit_app()

    def exit_app(self):
        logging.info("Shutting down Yeelight PC Companion...")
        try:
            self.status_timer.stop()
        except Exception:
            pass
        try:
            self._watchdog_timer.stop()
        except Exception:
            pass
        try:
            self.solar_thread.stop()
        except Exception:
            pass
        try:
            if self.restore_thread and self.restore_thread.isRunning():
                self.restore_thread.running = False
                self.restore_thread.wait(3000)
        except Exception:
            pass
        try:
            if hasattr(self, '_power_callback'):
                self._power_callback.unregister()
        except Exception:
            pass
        try:
            if hasattr(self, '_shutdown_window'):
                self._shutdown_window.destroy()
        except Exception:
            pass
        self.tray_icon.hide()
        QCoreApplication.quit()
        sys.exit(0)

# ---------------------------------------------------------
# Application Entry Point
# ---------------------------------------------------------
if __name__ == "__main__":
    import traceback

    # --- Narrow, elevated provisioning mode -------------------------------
    # The one-time administrator approval for the seamless OpenRGB launch runs
    # this same application with a single hardcoded mode. This interface can do
    # exactly one thing - create/update or remove the fixed OpenRGB elevation
    # task - and it never starts the UI, the tray, automation or configuration
    # handling. The task name and the OpenRGB arguments are not configurable.
    provisioning_exit = run_provisioning_cli(sys.argv)
    if provisioning_exit is not None:
        sys.exit(provisioning_exit)

    # Runtime state (config, logs, crash log, backups) lives in:
    #   * the repository directory when running from source,
    #   * %LOCALAPPDATA%\Yeelight PC Companion for a normal packaged install,
    #   * the application directory when portable.flag sits beside the executable.
    config_manager = ConfigManager()
    config_dir = config_manager.data_dir

    def write_crash_log(text):
        """Best-effort crash log in the data directory, else beside the app."""
        for candidate in (
            os.path.join(config_dir, CRASH_LOG_FILENAME),
            os.path.join(get_app_dir(), CRASH_LOG_FILENAME),
        ):
            try:
                os.makedirs(os.path.dirname(candidate), exist_ok=True)
                with open(candidate, "w", encoding="utf-8") as handle:
                    handle.write(text)
                return candidate
            except OSError:
                continue
        return None

    try:
        set_windows_app_id()

        # --- Set up file-based rotating logger for post-mortem diagnostics ---
        # Keeps up to 4 files × 2MB = 8MB max. Survives reboots.
        config_manager.ensure_data_dir()
        log_file = os.path.join(config_dir, DEBUG_LOG_FILENAME)
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=2*1024*1024, backupCount=3, encoding='utf-8'
            )
        except OSError:
            # Fall back next to the application if the data directory is not writable.
            file_handler = logging.handlers.RotatingFileHandler(
                os.path.join(get_app_dir(), DEBUG_LOG_FILENAME),
                maxBytes=2*1024*1024, backupCount=3, encoding='utf-8'
            )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S'
        ))
        file_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().setLevel(logging.DEBUG)
        logging.info("=" * 60)
        logging.info("YEELIGHT PC COMPANION STARTING — Session began at %s", datetime.now().isoformat())
        logging.info("Runtime storage mode: %s", storage_mode(config_manager.app_dir))
        logging.info("=" * 60)

        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False) # Essential for tray applications

        # Apply the shared visual theme before anything is shown, so the first-run
        # wizard and the main window (and every dialog they open) match.
        apply_app_theme(app)

        automation_paused = "--no-automation" in sys.argv
        auto_restore = "--no-autorestore" not in sys.argv
        start_hidden_in_tray = "--tray" in sys.argv

        # --- Configuration bootstrap: load, migrate, or run first-run setup ---
        config = None
        load_error = None

        if config_manager.exists():
            try:
                config = config_manager.load()
            except ConfigError as exc:
                load_error = str(exc)
                logging.error("[CONFIG] The existing configuration cannot be used: %s", exc)
            else:
                if config_manager.last_load_migrated_from is not None:
                    # Persist the migration explicitly, keeping one backup.
                    config_manager.save(config, create_backup=True)
                    logging.info(
                        "[CONFIG] Migrated configuration v%s -> v%s (previous file kept as %s).",
                        config_manager.last_load_migrated_from,
                        CONFIG_VERSION,
                        os.path.basename(config_manager.backup_path),
                    )
        else:
            # First launch at the new location: adopt a legacy configuration when
            # one can be found safely. The original file is never modified.
            legacy_config = config_manager.find_legacy_config()
            if legacy_config:
                try:
                    config = config_manager.import_config(legacy_config)
                except ConfigError as exc:
                    load_error = str(exc)
                    logging.error("[CONFIG] A legacy configuration was found but could not be used: %s", exc)
                else:
                    logging.info(
                        "[CONFIG] Adopted legacy configuration '%s' into the new location "
                        "(the original file was left untouched).",
                        os.path.basename(legacy_config),
                    )

        if config is None:
            # No usable configuration: the first-run wizard MUST be visible, even
            # when the app was started with --tray from the logon task.
            logging.info("[SETUP] No usable configuration found; starting first-run setup.")
            if not run_first_run_wizard(config_manager, stylesheet=APP_QSS, initial_error=load_error):
                logging.info(
                    "First-run setup was cancelled before a valid configuration existed. "
                    "Exiting without writing a partial configuration or starting automation."
                )
                sys.exit(0)
            logging.info("[SETUP] Setup finished; continuing startup with the new configuration.")

        # Initialize UI
        window = YeelightPCCompanionWindow(
            config_manager.config_path,
            auto_restore=auto_restore,
            automation_paused=automation_paused,
            config_manager=config_manager,
        )

        # --- Install Native Event Filter (SECONDARY power detector) ---
        # CRITICAL: Store as window attribute to prevent Python GC from collecting it.
        # A local variable here can be garbage-collected after ~2 days of uptime,
        # silently disabling all power event notifications forever.
        window._power_filter = WinPowerEventFilter(window.trigger_suspend, window.trigger_resume)
        app.installNativeEventFilter(window._power_filter)
        logging.info("[INIT] WinPowerEventFilter installed (secondary power detector)")

        # --- Register Win32 Direct Power Callback (PRIMARY power detector) ---
        # Uses PowerRegisterSuspendResumeNotification from PowrProf.dll.
        # Direct OS callback — no message loop dependency, no GC risk.
        # The OS calls our function directly on a system thread when sleep/wake occurs.
        window._power_callback = Win32PowerCallback(suspend_action=window._execute_suspend_actions)
        window._power_callback.resume_detected.connect(window.trigger_resume)
        window._power_callback.register()

        # Dedicated hidden HWND for Windows shutdown/logoff broadcasts.
        # This covers shutdowns where Qt's native event filter never sees WM_QUERYENDSESSION.
        window._shutdown_window = Win32ShutdownWindow(shutdown_action=window._execute_suspend_actions)
        window._shutdown_window.create()

        # If starting via shortcut or boot, keep window hidden and active only in tray
        if start_hidden_in_tray:
            logging.info("Yeelight PC Companion launched directly to system tray.")
        else:
            window.show()

        sys.exit(app.exec())
    except Exception as e:
        write_crash_log("YEELIGHT PC COMPANION CRASH:\n" + traceback.format_exc())
        print(f"CRASH OCCURRED: {e}")
        sys.exit(1)
