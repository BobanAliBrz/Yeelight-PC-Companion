"""OpenRGB Windows-service probe, identity check, conflict policy and repair.

OpenRGB 1.0 can install a Windows service named ``OpenRGB`` (the "OpenRGB SDK
Server"). When that service starts automatically it owns OpenRGB's lifecycle
and hardware detection, which conflicts with Yeelight PC Companion's own
launch/restore model (``YeelightPCCompanion-OpenRGB``). The conflict was
reproduced on real hardware: the service starts OpenRGB at boot even with
OpenRGB's own "Start at login" option off and no startup entry present.

This module is the only place that knows about that service:

* :class:`OpenRgbServiceProbe` - a small, read-only inspection result.
* :func:`probe_openrgb_service` - unelevated Service Control Manager query
  through ``ctypes`` (minimum access rights, handles always closed).
* :func:`extract_service_binary_path` / :func:`service_binary_matches` -
  safe ImagePath parsing and executable-identity comparison (quotes, case,
  separators, trailing arguments).
* :func:`evaluate_openrgb_service_status` - the pure conflict policy used by
  UI and restore diagnostics. Nothing else re-implements the rules.
* :func:`disable_openrgb_service` - the *only* mutation. It stops the fixed
  ``OpenRGB`` service and sets its startup type to Disabled, and only after
  the binary identity has been verified. It is meant to run inside the
  narrowly scoped elevated helper after an explicit user action.

Security properties kept deliberately:

* The service name is a hardcoded constant. There is no ``--service-name``,
  no generic command runner and no way to mutate any other service.
* The expected OpenRGB path is used **only** to refuse a mismatched service
  identity. It is never executed and never becomes a command interface.
* Read-only inspection works unelevated. Mutation is never attempted from
  startup, sleep, wake, status polling or solar reconciliation.
* The module is standard library only so every decision stays unit-testable
  without a real SCM, a real service or elevation.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  (loads the Windows type aliases used below)
import logging
import os
import re
import time

# ---------------------------------------------------------
# Fixed service identity
# ---------------------------------------------------------
# Hardcoded on purpose. This feature may mutate exactly one Windows service,
# and the name is never accepted from the command line, the configuration or
# any other caller.
OPENRGB_SERVICE_NAME = "OpenRGB"

# ---------------------------------------------------------
# Service Control Manager constants (winnt.h / winsvc.h)
# ---------------------------------------------------------
SC_MANAGER_CONNECT = 0x0001

SERVICE_QUERY_CONFIG = 0x0001
SERVICE_CHANGE_CONFIG = 0x0002
SERVICE_QUERY_STATUS = 0x0004
SERVICE_STOP = 0x0020
SERVICE_INTERROGATE = 0x0080

SERVICE_CONTROL_STOP = 0x00000001

SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SERVICE_CONTINUE_PENDING = 0x00000005
SERVICE_PAUSE_PENDING = 0x00000006
SERVICE_PAUSED = 0x00000007

SERVICE_BOOT_START = 0x00000000
SERVICE_SYSTEM_START = 0x00000001
SERVICE_AUTO_START = 0x00000002
SERVICE_DEMAND_START = 0x00000003
SERVICE_DISABLED = 0x00000004

SERVICE_NO_CHANGE = 0xFFFFFFFF

ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_SERVICE_ALREADY_RUNNING = 1056

# Human-facing names that do not depend on a localized `sc.exe` message table.
STATE_NAMES = {
    SERVICE_STOPPED: "stopped",
    SERVICE_START_PENDING: "start_pending",
    SERVICE_STOP_PENDING: "stop_pending",
    SERVICE_RUNNING: "running",
    SERVICE_CONTINUE_PENDING: "continue_pending",
    SERVICE_PAUSE_PENDING: "pause_pending",
    SERVICE_PAUSED: "paused",
}
STATE_UNKNOWN = "unknown"

START_TYPE_NAMES = {
    SERVICE_BOOT_START: "boot",
    SERVICE_SYSTEM_START: "system",
    SERVICE_AUTO_START: "automatic",
    SERVICE_DEMAND_START: "manual",
    SERVICE_DISABLED: "disabled",
}
START_TYPE_UNKNOWN = "unknown"

# Start types that mean "this service is configured to start by itself".
AUTOMATIC_START_TYPES = frozenset({"boot", "system", "automatic"})

# States that mean "not safely idle right now". Only a confirmed STOPPED
# service is treated as not currently owning OpenRGB.
NOT_STOPPED_STATES = frozenset(
    {
        "start_pending",
        "stop_pending",
        "running",
        "continue_pending",
        "pause_pending",
        "paused",
    }
)

# ---------------------------------------------------------
# Mutation timing
# ---------------------------------------------------------
# This is an explicit user-action path (UAC already shown), not the suspend
# hot path. It may wait a reasonable bounded time for the service to stop.
# The ~0.4 s `END_TASK_STOP_BUDGET_SECONDS` is deliberately NOT reused here.
OPENRGB_SERVICE_DISABLE_TIMEOUT_SECONDS = 15.0
SERVICE_STOP_POLL_SECONDS = 0.1

# ---------------------------------------------------------
# UI / policy status states
# ---------------------------------------------------------
SERVICE_STATE_NO_CONFLICT = "no_conflict"
SERVICE_STATE_CONFLICT = "conflict"
SERVICE_STATE_INSTALLED_IDLE = "installed_idle"
SERVICE_STATE_BINARY_MISMATCH = "binary_mismatch"
SERVICE_STATE_UNKNOWN = "unknown"
SERVICE_STATE_NOT_USED = "not_used"

DISABLE_SERVICE_ACTION_LABEL = "Disable conflicting service"

SERVICE_CONFLICT_HINT = (
    "Yeelight PC Companion manages OpenRGB's SDK server and lifecycle itself. "
    "OpenRGB's separate Windows service can start OpenRGB before hardware is "
    "ready and prevents YPC from cleanly restarting it across sleep/wake."
)


class OpenRgbServiceError(Exception):
    """A service-control problem with a user-friendly message."""


class OpenRgbServiceProbe:
    """Read-only inspection result for the fixed OpenRGB Windows service.

    ``exists`` is True only when the service was actually found. It is False
    both for a confirmed absent service and for a query failure, so ``error``
    is what distinguishes them: an empty error with ``exists=False`` means
    "genuinely absent", a non-empty error means "could not tell / could not
    read". Callers must never treat a query failure as "no conflict".
    """

    __slots__ = ("exists", "state", "start_type", "binary_path", "error")

    def __init__(
        self,
        exists=False,
        state=STATE_UNKNOWN,
        start_type=START_TYPE_UNKNOWN,
        binary_path="",
        error="",
    ):
        self.exists = bool(exists)
        self.state = state or STATE_UNKNOWN
        self.start_type = start_type or START_TYPE_UNKNOWN
        self.binary_path = binary_path or ""
        self.error = error or ""

    @property
    def query_failed(self):
        return bool(self.error)

    @property
    def is_absent(self):
        return (not self.exists) and (not self.error)

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"OpenRgbServiceProbe(exists={self.exists!r}, state={self.state!r}, "
            f"start_type={self.start_type!r}, error={self.error!r})"
        )


class OpenRgbServiceStatus:
    """What the UI / policy layer reports about the OpenRGB Windows service."""

    __slots__ = ("state", "label", "detail", "can_auto_fix", "is_conflict", "probe")

    def __init__(
        self,
        state,
        label,
        detail="",
        can_auto_fix=False,
        is_conflict=False,
        probe=None,
    ):
        self.state = state
        self.label = label
        self.detail = detail
        self.can_auto_fix = bool(can_auto_fix)
        self.is_conflict = bool(is_conflict)
        self.probe = probe

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"OpenRgbServiceStatus({self.state!r}, {self.label!r}, can_auto_fix={self.can_auto_fix!r})"


class OpenRgbServiceDisableResult:
    """Outcome of one stop+disable attempt on the fixed OpenRGB service."""

    __slots__ = ("ok", "message", "exit_code")

    def __init__(self, ok, message, exit_code=0):
        self.ok = bool(ok)
        self.message = message
        self.exit_code = int(exit_code)

    def __iter__(self):
        return iter((self.ok, self.message))

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"OpenRgbServiceDisableResult(ok={self.ok!r}, "
            f"exit_code={self.exit_code!r})"
        )


# ---------------------------------------------------------
# Binary-path extraction and identity matching
# ---------------------------------------------------------
_QUOTED_PATH = re.compile(r'^\s*"([^"]+)"')
_UNQUOTED_EXE_PATH = re.compile(r"^(.*?\.exe)(?:\s|$)", re.IGNORECASE)


def extract_service_binary_path(image_path):
    """Return the executable path from a service ``ImagePath``.

    Handles the forms Windows actually writes:

    * ``C:\\Path\\OpenRGB.exe``
    * ``"C:\\Path With Spaces\\OpenRGB.exe"``
    * ``"C:\\Path\\OpenRGB.exe" --server --gui``
    * ``C:\\Path\\OpenRGB.exe --server`` (unquoted, with arguments)
    * ``\\??\\C:\\Path\\OpenRGB.exe`` / ``\\\\?\\C:\\Path\\OpenRGB.exe``

    Returns ``''`` when no executable path can be determined. Never raises.
    """
    text = (image_path or "").strip()
    if not text:
        return ""

    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break

    quoted = _QUOTED_PATH.match(text)
    if quoted:
        return quoted.group(1).strip()

    match = _UNQUOTED_EXE_PATH.match(text)
    if match:
        return match.group(1).strip()

    # No .exe marker: fall back to the first whitespace-delimited token only
    # when that token still looks like a path. Otherwise the path is malformed.
    first = text.split()[0] if text.split() else text
    if first.lower().endswith(".exe") or os.path.isabs(first):
        return first
    return ""


def normalize_executable_path(path):
    """Normalize a filesystem path for case-insensitive Windows comparison."""
    text = (path or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if not text:
        return ""
    text = os.path.expandvars(text)
    try:
        text = os.path.normpath(text)
    except (OSError, ValueError):
        return ""
    # normcase lowercases and unifies separators on Windows.
    return os.path.normcase(text)


def service_binary_matches(image_path, expected_exe):
    """Whether the service ``ImagePath`` is the configured OpenRGB executable.

    Returns:

    * ``True``  - same executable (quotes, case, separators and trailing
      arguments already accounted for),
    * ``False`` - a different executable,
    * ``None``  - the service path is malformed/unreadable, or the expected
      path is empty, so identity cannot be established.

    ``None`` is never treated as "matching": an unverifiable identity blocks
    the automated fix instead of allowing a privileged mutation.
    """
    extracted = extract_service_binary_path(image_path)
    expected = normalize_executable_path(expected_exe)
    if not extracted or not expected:
        return None
    actual = normalize_executable_path(extracted)
    if not actual:
        return None
    return actual == expected


# ---------------------------------------------------------
# SCM plumbing (ctypes)
# ---------------------------------------------------------
class _SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", ctypes.wintypes.DWORD),
        ("dwCurrentState", ctypes.wintypes.DWORD),
        ("dwControlsAccepted", ctypes.wintypes.DWORD),
        ("dwWin32ExitCode", ctypes.wintypes.DWORD),
        ("dwServiceSpecificExitCode", ctypes.wintypes.DWORD),
        ("dwCheckPoint", ctypes.wintypes.DWORD),
        ("dwWaitHint", ctypes.wintypes.DWORD),
    ]


class _QUERY_SERVICE_CONFIG(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", ctypes.wintypes.DWORD),
        ("dwStartType", ctypes.wintypes.DWORD),
        ("dwErrorControl", ctypes.wintypes.DWORD),
        ("lpBinaryPathName", ctypes.wintypes.LPWSTR),
        ("lpLoadOrderGroup", ctypes.wintypes.LPWSTR),
        ("dwTagId", ctypes.wintypes.DWORD),
        ("lpDependencies", ctypes.wintypes.LPWSTR),
        ("lpServiceStartName", ctypes.wintypes.LPWSTR),
        ("lpDisplayName", ctypes.wintypes.LPWSTR),
    ]


def _advapi32():
    return ctypes.WinDLL("advapi32", use_last_error=True)


def _close_service_handle(handle):
    """Close an SC_HANDLE. The handle always ends up closed when non-zero."""
    if not handle:
        return
    try:
        advapi32 = _advapi32()
        advapi32.CloseServiceHandle.argtypes = [ctypes.wintypes.HANDLE]
        advapi32.CloseServiceHandle.restype = ctypes.wintypes.BOOL
        advapi32.CloseServiceHandle(handle)
    except Exception:  # pragma: no cover - defensive
        pass


def _win32_error_text(code):
    return f"Windows error {int(code)}"


def _open_scm(desired_access=SC_MANAGER_CONNECT):
    advapi32 = _advapi32()
    advapi32.OpenSCManagerW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
    ]
    advapi32.OpenSCManagerW.restype = ctypes.wintypes.HANDLE
    handle = advapi32.OpenSCManagerW(None, None, desired_access)
    if not handle:
        raise OpenRgbServiceError(
            "The Windows Service Control Manager could not be opened "
            f"({_win32_error_text(ctypes.get_last_error())})."
        )
    return advapi32, handle


def _open_service(advapi32, scm_handle, desired_access):
    """Open the fixed OpenRGB service. Returns ``(handle, error_code)``.

    ``handle`` is 0 when the service could not be opened. ``error_code`` is the
    Win32 error (``ERROR_SERVICE_DOES_NOT_EXIST`` means genuinely absent).
    """
    advapi32.OpenServiceW.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
    ]
    advapi32.OpenServiceW.restype = ctypes.wintypes.HANDLE
    handle = advapi32.OpenServiceW(scm_handle, OPENRGB_SERVICE_NAME, desired_access)
    if not handle:
        return 0, int(ctypes.get_last_error())
    return handle, 0


def _query_service_status(advapi32, service_handle):
    advapi32.QueryServiceStatus.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(_SERVICE_STATUS),
    ]
    advapi32.QueryServiceStatus.restype = ctypes.wintypes.BOOL
    status = _SERVICE_STATUS()
    if not advapi32.QueryServiceStatus(service_handle, ctypes.byref(status)):
        raise OpenRgbServiceError(
            "The OpenRGB service status could not be read "
            f"({_win32_error_text(ctypes.get_last_error())})."
        )
    return status


def _query_service_binary_path(advapi32, service_handle):
    """Read ``lpBinaryPathName`` with minimum ``SERVICE_QUERY_CONFIG`` access."""
    advapi32.QueryServiceConfigW.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    advapi32.QueryServiceConfigW.restype = ctypes.wintypes.BOOL

    needed = ctypes.wintypes.DWORD(0)
    advapi32.QueryServiceConfigW(service_handle, None, 0, ctypes.byref(needed))
    # ERROR_INSUFFICIENT_BUFFER is the expected first-call result.
    size = int(needed.value) if needed.value else 1024
    size = max(size, ctypes.sizeof(_QUERY_SERVICE_CONFIG) + 512)

    buffer = ctypes.create_unicode_buffer(size // 2 if size >= 2 else 512)
    raw = ctypes.cast(buffer, ctypes.c_void_p)
    if not advapi32.QueryServiceConfigW(service_handle, raw, size, ctypes.byref(needed)):
        raise OpenRgbServiceError(
            "The OpenRGB service configuration could not be read "
            f"({_win32_error_text(ctypes.get_last_error())})."
        )
    config = ctypes.cast(raw, ctypes.POINTER(_QUERY_SERVICE_CONFIG)).contents
    start_type = START_TYPE_NAMES.get(int(config.dwStartType), START_TYPE_UNKNOWN)
    binary_path = config.lpBinaryPathName or ""
    return start_type, binary_path


def probe_openrgb_service():
    """Read-only inspection of the fixed ``OpenRGB`` Windows service.

    Works unelevated with ``SC_MANAGER_CONNECT`` plus
    ``SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS``. Every handle is closed on
    every path. Never raises: failures are reported through
    :attr:`OpenRgbServiceProbe.error` so a query failure is never mistaken for
    "service absent".
    """
    probe = OpenRgbServiceProbe()
    if os.name != "nt":
        probe.error = "Windows services are only available on Windows."
        return probe

    scm_handle = 0
    service_handle = 0
    try:
        try:
            advapi32, scm_handle = _open_scm(SC_MANAGER_CONNECT)
        except OpenRgbServiceError as exc:
            probe.error = str(exc)
            return probe

        service_handle, error_code = _open_service(
            advapi32, scm_handle, SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS
        )
        if not service_handle:
            if error_code == ERROR_SERVICE_DOES_NOT_EXIST:
                # Confirmed absent: not an error.
                probe.exists = False
                return probe
            probe.exists = False
            probe.error = (
                "The OpenRGB service could not be inspected "
                f"({_win32_error_text(error_code)})."
            )
            return probe

        probe.exists = True
        try:
            status = _query_service_status(advapi32, service_handle)
            probe.state = STATE_NAMES.get(
                int(status.dwCurrentState), STATE_UNKNOWN
            )
        except OpenRgbServiceError as exc:
            probe.error = str(exc)

        try:
            start_type, binary_path = _query_service_binary_path(
                advapi32, service_handle
            )
            probe.start_type = start_type
            probe.binary_path = binary_path or ""
        except OpenRgbServiceError as exc:
            if probe.error:
                probe.error = f"{probe.error} {exc}"
            else:
                probe.error = str(exc)

        return probe
    except Exception as exc:  # pragma: no cover - defensive
        probe.exists = False
        probe.error = (
            f"The OpenRGB service could not be inspected ({type(exc).__name__})."
        )
        return probe
    finally:
        _close_service_handle(service_handle)
        _close_service_handle(scm_handle)


# ---------------------------------------------------------
# Conflict policy (pure / read-only)
# ---------------------------------------------------------
def evaluate_openrgb_service_status(probe, integration_enabled, expected_path):
    """Classify the OpenRGB service for the UI and for restore diagnostics.

    A service is an active YPC/OpenRGB conflict when OpenRGB integration is
    enabled and the fixed OpenRGB service:

    * is currently not stopped (running / starting / pausing / ...), OR
    * is configured for automatic startup (``automatic``/``boot``/``system``).

    Stopped + Disabled is not a conflict. Stopped + Manual is not an immediate
    boot conflict and is reported as installed/idle rather than as disabled.
    A running service is a current conflict even when its start type is Manual.

    A query failure is unknown - never "safe". This includes a *partial*
    failure where the service was proven to exist but only one of the later
    queries (status or config) failed: any non-empty ``probe.error`` blocks
    the safe/no-conflict and installed-idle classifications and blocks the
    automated fix. A binary-path mismatch is a review state: the automated fix
    is not offered.
    """
    if not integration_enabled:
        return OpenRgbServiceStatus(
            SERVICE_STATE_NOT_USED,
            "Not used",
            "The OpenRGB integration is disabled, so this service is not managed.",
            can_auto_fix=False,
            is_conflict=False,
            probe=probe,
        )

    # Fail closed on ANY inspection failure - including a partial one where
    # exists=True and only the status or the config query failed. "Unknown"
    # is never treated as safe, and an error never reaches No conflict or
    # Installed idle.
    if probe is None or probe.query_failed:
        if probe is not None and probe.error:
            if probe.exists:
                detail = (
                    "The OpenRGB Windows service exists but could not be fully "
                    f"inspected ({probe.error})"
                )
            else:
                detail = probe.error
        else:
            detail = "The OpenRGB Windows service could not be inspected."
        return OpenRgbServiceStatus(
            SERVICE_STATE_UNKNOWN,
            "Unknown",
            f"{detail} Review the Windows service named 'OpenRGB' manually.",
            can_auto_fix=False,
            is_conflict=False,
            probe=probe,
        )

    if probe.is_absent:
        return OpenRgbServiceStatus(
            SERVICE_STATE_NO_CONFLICT,
            "No conflict",
            "No OpenRGB Windows service is installed.",
            can_auto_fix=False,
            is_conflict=False,
            probe=probe,
        )

    # Identity first: a service that is not the configured OpenRGB is never
    # offered for automatic stop/disable, whatever its state is.
    identity = service_binary_matches(probe.binary_path, expected_path)
    if identity is None:
        return OpenRgbServiceStatus(
            SERVICE_STATE_UNKNOWN,
            "Review needed",
            (
                "The OpenRGB service binary path could not be verified against "
                "your configured OpenRGB executable. Review it manually before "
                "letting Yeelight PC Companion change the service."
            ),
            can_auto_fix=False,
            is_conflict=False,
            probe=probe,
        )
    if identity is False:
        return OpenRgbServiceStatus(
            SERVICE_STATE_BINARY_MISMATCH,
            "Needs review",
            (
                "The Windows service 'OpenRGB' points at a different executable "
                "than the OpenRGB path configured in Yeelight PC Companion. It "
                "needs manual review; Yeelight PC Companion will not stop or "
                "disable it automatically."
            ),
            can_auto_fix=False,
            is_conflict=True,
            probe=probe,
        )

    # Identity matches. Apply the conflict policy.
    running_now = probe.state in NOT_STOPPED_STATES
    automatic = probe.start_type in AUTOMATIC_START_TYPES
    disabled = probe.start_type == "disabled"
    manual = probe.start_type == "manual"

    if running_now:
        return OpenRgbServiceStatus(
            SERVICE_STATE_CONFLICT,
            "Conflict detected",
            SERVICE_CONFLICT_HINT,
            can_auto_fix=True,
            is_conflict=True,
            probe=probe,
        )

    if automatic:
        return OpenRgbServiceStatus(
            SERVICE_STATE_CONFLICT,
            "Conflict detected",
            (
                f"{SERVICE_CONFLICT_HINT} "
                "The service is set to start automatically with Windows."
            ),
            can_auto_fix=True,
            is_conflict=True,
            probe=probe,
        )

    if disabled:
        return OpenRgbServiceStatus(
            SERVICE_STATE_NO_CONFLICT,
            "No conflict",
            "The OpenRGB Windows service is stopped and disabled.",
            can_auto_fix=False,
            is_conflict=False,
            probe=probe,
        )

    if manual:
        # Stopped + Manual is not an immediate boot conflict. Say so honestly
        # instead of pretending the service is disabled.
        return OpenRgbServiceStatus(
            SERVICE_STATE_INSTALLED_IDLE,
            "Installed, not conflicting",
            (
                "The OpenRGB Windows service is installed but stopped and set "
                "to Manual, so it does not start with Windows. If something "
                "starts it, it will conflict again."
            ),
            can_auto_fix=False,
            is_conflict=False,
            probe=probe,
        )

    # Unknown start type or a partial query: never invent a safe state.
    return OpenRgbServiceStatus(
        SERVICE_STATE_UNKNOWN,
        "Unknown",
        (
            "The OpenRGB Windows service could not be fully classified "
            f"(state={probe.state}, start type={probe.start_type}). "
            "Review it manually."
        ),
        can_auto_fix=False,
        is_conflict=False,
        probe=probe,
    )


# ---------------------------------------------------------
# Privileged mutation (elevated helper only)
# ---------------------------------------------------------
def _change_startup_disabled(advapi32, service_handle):
    advapi32.ChangeServiceConfigW.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPCWSTR,
    ]
    advapi32.ChangeServiceConfigW.restype = ctypes.wintypes.BOOL
    ok = advapi32.ChangeServiceConfigW(
        service_handle,
        SERVICE_NO_CHANGE,
        SERVICE_DISABLED,
        SERVICE_NO_CHANGE,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    if not ok:
        raise OpenRgbServiceError(
            "The OpenRGB service startup type could not be set to Disabled "
            f"({_win32_error_text(ctypes.get_last_error())})."
        )


def _request_service_stop(advapi32, service_handle):
    """Ask the service to stop. Returns True when a stop was accepted/unnecessary."""
    advapi32.ControlService.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_SERVICE_STATUS),
    ]
    advapi32.ControlService.restype = ctypes.wintypes.BOOL
    status = _SERVICE_STATUS()
    if advapi32.ControlService(service_handle, SERVICE_CONTROL_STOP, ctypes.byref(status)):
        return True
    error = int(ctypes.get_last_error())
    if error == ERROR_SERVICE_NOT_ACTIVE:
        # Already stopped: the stop request is unnecessary.
        return True
    raise OpenRgbServiceError(
        f"The OpenRGB service could not be stopped ({_win32_error_text(error)})."
    )


def _wait_for_service_stopped(advapi32, service_handle, timeout_seconds):
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        status = _query_service_status(advapi32, service_handle)
        if int(status.dwCurrentState) == SERVICE_STOPPED:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SERVICE_STOP_POLL_SECONDS)


def disable_openrgb_service(
    expected_openrgb_path,
    probe_fn=None,
    timeout_seconds=OPENRGB_SERVICE_DISABLE_TIMEOUT_SECONDS,
):
    """Stop the fixed ``OpenRGB`` service and set its startup type to Disabled.

    This is the **only** service mutation in the product. It must run inside
    the narrowly scoped elevated helper after an explicit user action - never
    from startup, sleep, wake, status polling or solar reconciliation.

    Behaviour:

    1. Inspect the fixed service (read-only first).
    2. No-op successfully when it is genuinely absent.
    3. Refuse to mutate when the service binary path does not match the
       configured OpenRGB executable, or when identity cannot be verified.
    4. Change startup type to Disabled, then stop the service if needed.
    5. Wait for STOPPED under ``timeout_seconds``.
    6. Re-query and report success **only** when startup is Disabled **and**
       state is Stopped. A partial result (disabled but still running) is
       reported as a failure with its own diagnostic.

    Returns :class:`OpenRgbServiceDisableResult`. Never raises.
    """
    probe_fn = probe_fn or probe_openrgb_service
    try:
        probe = probe_fn()
    except Exception as exc:  # pragma: no cover - defensive
        return OpenRgbServiceDisableResult(
            False,
            f"The OpenRGB service could not be inspected ({type(exc).__name__}).",
            1,
        )

    if probe.is_absent:
        return OpenRgbServiceDisableResult(
            True,
            "No OpenRGB Windows service is installed; nothing to change.",
            0,
        )

    if probe.query_failed and not probe.exists:
        return OpenRgbServiceDisableResult(
            False,
            probe.error or "The OpenRGB service could not be inspected.",
            1,
        )

    identity = service_binary_matches(probe.binary_path, expected_openrgb_path)
    if identity is None:
        return OpenRgbServiceDisableResult(
            False,
            (
                "The OpenRGB service binary path could not be verified against "
                "the configured OpenRGB executable, so the service was not changed."
            ),
            3,
        )
    if identity is False:
        return OpenRgbServiceDisableResult(
            False,
            (
                "The Windows service 'OpenRGB' points at a different executable "
                "than the configured OpenRGB path. The service was not changed."
            ),
            3,
        )

    if os.name != "nt":  # pragma: no cover - non-Windows guard
        return OpenRgbServiceDisableResult(
            False,
            "Windows services are only available on Windows.",
            1,
        )

    scm_handle = 0
    service_handle = 0
    try:
        try:
            advapi32, scm_handle = _open_scm(SC_MANAGER_CONNECT)
        except OpenRgbServiceError as exc:
            return OpenRgbServiceDisableResult(False, str(exc), 1)

        # Minimum rights for this handle's whole lifetime:
        #   SERVICE_CHANGE_CONFIG - set startup type to Disabled
        #   SERVICE_STOP          - request a stop
        #   SERVICE_QUERY_STATUS  - wait for / re-query the final state
        #   SERVICE_QUERY_CONFIG  - re-query the startup type (QueryServiceConfigW
        #                           on this same handle during final verification)
        # Nothing broader; never SERVICE_ALL_ACCESS.
        service_handle, error_code = _open_service(
            advapi32,
            scm_handle,
            SERVICE_QUERY_CONFIG
            | SERVICE_QUERY_STATUS
            | SERVICE_STOP
            | SERVICE_CHANGE_CONFIG,
        )
        if not service_handle:
            if error_code == ERROR_SERVICE_DOES_NOT_EXIST:
                return OpenRgbServiceDisableResult(
                    True,
                    "No OpenRGB Windows service is installed; nothing to change.",
                    0,
                )
            return OpenRgbServiceDisableResult(
                False,
                (
                    "The OpenRGB service could not be opened for repair "
                    f"({_win32_error_text(error_code)})."
                ),
                1,
            )

        try:
            _change_startup_disabled(advapi32, service_handle)
        except OpenRgbServiceError as exc:
            return OpenRgbServiceDisableResult(False, str(exc), 1)

        stop_error = ""
        try:
            _request_service_stop(advapi32, service_handle)
        except OpenRgbServiceError as exc:
            stop_error = str(exc)

        stopped = False
        if not stop_error:
            try:
                stopped = _wait_for_service_stopped(
                    advapi32, service_handle, timeout_seconds
                )
            except OpenRgbServiceError as exc:
                stop_error = str(exc)

        # Re-query. Success requires the FINAL re-query to prove Disabled AND
        # Stopped - nothing less. The earlier wait observation is useful
        # process information but must never override a contradictory final
        # state (e.g. wait returned True while the service is RUNNING again).
        final_state = STATE_UNKNOWN
        final_start = START_TYPE_UNKNOWN
        verify_error = ""
        try:
            status = _query_service_status(advapi32, service_handle)
            final_state = STATE_NAMES.get(int(status.dwCurrentState), STATE_UNKNOWN)
            start_type, _binary = _query_service_binary_path(advapi32, service_handle)
            final_start = start_type
        except OpenRgbServiceError as exc:
            verify_error = str(exc)

        startup_ok = final_start == "disabled"
        state_ok = final_state == "stopped"

        if startup_ok and state_ok and not verify_error:
            return OpenRgbServiceDisableResult(
                True,
                "The OpenRGB Windows service is stopped and disabled.",
                0,
            )

        if startup_ok and not state_ok:
            note = ""
            if stopped and final_state != "stopped":
                note = " The stop wait reported stopped, but the final state disagrees."
            return OpenRgbServiceDisableResult(
                False,
                (
                    "The OpenRGB service startup type is now Disabled, but the "
                    f"service did not stop (state={final_state})."
                    + note
                    + (f" {stop_error}" if stop_error else "")
                ),
                5,
            )

        parts = []
        if not startup_ok:
            parts.append(f"startup type is {final_start}")
        if not state_ok:
            parts.append(f"state is {final_state}")
        if verify_error:
            parts.append(verify_error)
        if stop_error:
            parts.append(stop_error)
        return OpenRgbServiceDisableResult(
            False,
            "The OpenRGB service was not fully repaired: " + "; ".join(parts) + ".",
            5,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return OpenRgbServiceDisableResult(
            False,
            f"The OpenRGB service repair failed ({type(exc).__name__}).",
            1,
        )
    finally:
        _close_service_handle(service_handle)
        _close_service_handle(scm_handle)


def log_openrgb_service_conflict(status):
    """Log exactly one warning when a real conflict is present at startup."""
    if status is None or not status.is_conflict:
        return
    logging.warning(
        "[OPENRGB] Windows service conflict: %s. %s Resolve it from the "
        "Integrations page for reliable sleep/wake control.",
        status.label,
        (status.detail or SERVICE_CONFLICT_HINT).split("\n")[0],
    )


__all__ = [
    "AUTOMATIC_START_TYPES",
    "DISABLE_SERVICE_ACTION_LABEL",
    "NOT_STOPPED_STATES",
    "OPENRGB_SERVICE_DISABLE_TIMEOUT_SECONDS",
    "OPENRGB_SERVICE_NAME",
    "SERVICE_CONFLICT_HINT",
    "SERVICE_STATE_BINARY_MISMATCH",
    "SERVICE_STATE_CONFLICT",
    "SERVICE_STATE_INSTALLED_IDLE",
    "SERVICE_STATE_NOT_USED",
    "SERVICE_STATE_NO_CONFLICT",
    "SERVICE_STATE_UNKNOWN",
    "OpenRgbServiceDisableResult",
    "OpenRgbServiceError",
    "OpenRgbServiceProbe",
    "OpenRgbServiceStatus",
    "disable_openrgb_service",
    "evaluate_openrgb_service_status",
    "extract_service_binary_path",
    "log_openrgb_service_conflict",
    "normalize_executable_path",
    "probe_openrgb_service",
    "service_binary_matches",
]
