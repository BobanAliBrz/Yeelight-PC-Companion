"""Windows Scheduled Task elevation broker for OpenRGB.

OpenRGB needs administrator rights on some systems (RAM RGB and other
kernel-driver based devices). Launching it with ``ShellExecuteW(..., "runas",
...)`` works, but it raises a UAC prompt - which is unacceptable on the normal
sleep/wake path, because nobody is sitting in front of the PC when it resumes.

This module implements the normal Windows way of delegating that privilege once
and then reusing it silently:

* a dedicated, on-demand scheduled task (``YeelightPCCompanion-OpenRGB``) runs
  the configured OpenRGB executable with **highest available** privileges and
  the fixed OpenRGB arguments,
* the task has **no triggers at all**, so Task Scheduler never starts it by
  itself - it only runs when Yeelight PC Companion explicitly asks for it,
* an already-created highest-privilege task can be started on demand without a
  UAC prompt, which is what makes wake-time restoration silent,
* the task is created or updated **once**, from an explicit user action
  (first-run setup or the Settings tab), where a single administrator approval
  is expected and explained to the user.

Nothing here disables UAC, changes consent policy, edits the registry to
suppress prompts, abuses auto-elevated binaries or uses any other security
bypass. The only privilege escalation is a normal, user-approved, one-time
scheduled task definition.

The task action always carries an explicit working directory (the folder that
contains OpenRGB), so the elevated process never inherits an arbitrary Task
Scheduler working directory and never loads its runtime files from somewhere
else.

Because an approved highest-privilege task is a standing promise to run a fixed
path with administrator rights, that path may only point at an executable the
*unelevated* user cannot replace - otherwise the task would silently launch a
swapped-in file elevated. Provisioning therefore refuses user-writable targets
(``is_elevation_target_secure()``) instead of creating the task for them.

The one-time approval itself runs this same application as an elevated helper
and **waits for it**: the helper is launched with ``ShellExecuteExW`` and
``SEE_MASK_NOCLOSEPROCESS``, so the parent owns the helper's process handle,
reads its exit code and shows the helper's own user-safe reason (carried back
through one narrow diagnostics file) instead of guessing from task state.

The module is standard library only, so the task definition, the argument
handling and the lifecycle decisions stay unit-testable without touching the
real Task Scheduler.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  (loads the Windows type aliases used below)
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

# ---------------------------------------------------------
# Fixed task identity
# ---------------------------------------------------------
# The task name, the OpenRGB arguments and the task description are hardcoded
# on purpose. This task is effectively privileged execution, so nothing about it
# may be driven by user input beyond the one thing that has to vary: the OpenRGB
# executable path. See project_memory.md ("OpenRGB elevation model").
OPENRGB_TASK_NAME = "YeelightPCCompanion-OpenRGB"
OPENRGB_TASK_ARGS = ("--gui", "--startminimized", "--server")
OPENRGB_TASK_ARGS_STRING = " ".join(OPENRGB_TASK_ARGS)
OPENRGB_TASK_DESCRIPTION = (
    "Starts OpenRGB with the SDK server for Yeelight PC Companion. "
    "Created by Yeelight PC Companion; remove it when the OpenRGB integration is disabled."
)

TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_XML_VERSION = "1.2"

# Narrow, hardcoded command-line modes of this application. They are the only
# provisioning interface the elevated helper accepts.
PROVISION_FLAG = "--provision-openrgb-task"
REMOVE_FLAG = "--remove-openrgb-task"

# ---------------------------------------------------------
# Status model
# ---------------------------------------------------------
STATUS_READY = "ready"
STATUS_NEEDS_SETUP = "needs_setup"
STATUS_NEEDS_REPAIR = "needs_repair"
STATUS_PATH_MISMATCH = "path_mismatch"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"
# The configured OpenRGB executable cannot be handed to a privileged task,
# either because the current user can modify it or because that could not be
# established. Both are reported the same way: never ready, needs the user to
# put OpenRGB somewhere protected.
STATUS_UNSAFE_TARGET = "unsafe_target"

ACTION_NONE = "none"
ACTION_PROVISION = "provision"
ACTION_REMOVE = "remove"

SET_UP_ACTION_LABEL = "Set Up Seamless OpenRGB Launch"
REPAIR_ACTION_LABEL = "Repair OpenRGB Elevation"

# User-facing explanation for a target that may not be handed to the privileged
# task. The wording matters: the user decides where OpenRGB lives, this code
# never moves files, takes ownership or edits permissions.
UNSAFE_TARGET_MESSAGE = (
    "OpenRGB cannot use seamless elevated launch from this location because the "
    "executable can be modified by your normal user account.\n\n"
    "Install or move OpenRGB to a protected location such as Program Files, then "
    "update the path in Settings."
)
UNVERIFIED_TARGET_MESSAGE = (
    "OpenRGB cannot use seamless elevated launch from this location because Windows "
    "could not confirm that the executable is protected from your normal user "
    "account.\n\n"
    "Install or move OpenRGB to a protected location such as Program Files, then "
    "update the path in Settings."
)
UNSAFE_TARGET_LABEL = "Unsafe OpenRGB location"
UNVERIFIED_TARGET_LABEL = "OpenRGB location could not be verified"

# ShellExecuteW()/ShellExecuteExW() report a declined UAC prompt with this code.
SE_ERR_ACCESSDENIED = 5
# GetLastError() reports the same thing this way once ShellExecuteExW fails.
ERROR_CANCELLED = 1223

# ---------------------------------------------------------
# Provisioning result channel (diagnostics only)
# ---------------------------------------------------------
# The elevated helper used to fail opaquely: the parent only saw "the task did
# not appear". These two files - both inside the application data directory -
# carry the child's own reason back to the parent and into the provisioning
# log. They are diagnostics only: nothing here is ever read to decide which
# privileged action to take, and the file has no command/task/path interface.
PROVISION_RESULT_FILENAME = "openrgb_provision_result.json"
PROVISIONING_LOG_FILENAME = "openrgb_provisioning.log"
PROVISION_RESULT_MAX_BYTES = 64 * 1024
PROVISION_LOG_MAX_BYTES = 64 * 1024

# Exit codes of the narrow provisioning CLI (see run_provisioning_cli).
PROVISION_EXIT_OK = 0
PROVISION_EXIT_FAILED = 1
PROVISION_EXIT_BAD_COMMAND_LINE = 2
PROVISION_EXIT_SECURITY = 3
PROVISION_EXIT_TASK_CREATION = 4
PROVISION_EXIT_NOT_READY = 5

# What a bare exit code means when the helper could not write a result file.
PROVISION_EXIT_MESSAGES = {
    PROVISION_EXIT_OK: "",
    PROVISION_EXIT_FAILED: "The provisioning helper reported a failure.",
    PROVISION_EXIT_BAD_COMMAND_LINE: (
        "The provisioning helper rejected its own command line."
    ),
    PROVISION_EXIT_SECURITY: (
        "The protected-location check could not confirm that the OpenRGB "
        "executable is protected from your normal user account."
    ),
    PROVISION_EXIT_TASK_CREATION: (
        "Windows Task Scheduler rejected the task definition."
    ),
    PROVISION_EXIT_NOT_READY: (
        "Provisioning helper completed, but the resulting task is not usable."
    ),
}

# Actions are reported with their subject so a failure never says just
# "something went wrong".
PROVISION_ACTION_SUBJECTS = {
    ACTION_PROVISION: "OpenRGB elevated launch could not be configured.",
    ACTION_REMOVE: "OpenRGB elevated launch could not be removed.",
}

# The helper process is waited on directly (no polling); this is only the
# guard against a broken elevated process hanging the Settings UI forever.
PROVISIONING_TIMEOUT_SECONDS = 30.0
# How long a killed helper's process handle may take to become signalled.
PROCESS_EXIT_GRACE_SECONDS = 2.0
SCHTASKS_TIMEOUT_SECONDS = 30.0

# ShellExecuteExW()
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259


class WindowsTaskError(Exception):
    """A Windows Task Scheduler problem with a user-friendly message."""


class SecurityPolicyViolation(WindowsTaskError):
    """The protected-location requirement refused a target.

    A subclass of :class:`WindowsTaskError` so existing handling keeps working,
    but distinguishable: this is a deliberate security decision ("this
    executable may not be handed to a privileged task"), not a Task Scheduler or
    environment failure. Provisioning reports the two with different exit codes.
    """


class OpenRgbElevationStatus:
    """Result of inspecting the OpenRGB elevation task."""

    __slots__ = ("state", "label", "detail", "action_label")

    def __init__(self, state, label, detail="", action_label=None):
        self.state = state
        self.label = label
        self.detail = detail
        self.action_label = action_label

    @property
    def is_ready(self):
        return self.state == STATUS_READY

    @property
    def needs_action(self):
        return self.state in (
            STATUS_NEEDS_SETUP,
            STATUS_NEEDS_REPAIR,
            STATUS_PATH_MISMATCH,
            STATUS_UNSAFE_TARGET,
        )

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"OpenRgbElevationStatus({self.state!r}, {self.label!r})"


class TaskProbe:
    """Raw inspection result for the OpenRGB task.

    ``working_directory``, ``logon_type``, ``user_id`` and ``has_triggers``
    describe the task identity that this application itself creates, so a task
    with the right name can be told apart from a task with the right content.
    Task Scheduler omits values that equal its own defaults, so a missing
    ``LogonType`` or ``UserId`` is stored as an empty string and read as "not
    stated" rather than as a mismatch.
    """

    __slots__ = (
        "exists",
        "command",
        "arguments",
        "working_directory",
        "run_level",
        "logon_type",
        "user_id",
        "has_triggers",
        "enabled",
        "error",
    )

    def __init__(
        self,
        exists=False,
        command="",
        arguments="",
        run_level="",
        enabled=True,
        error="",
        working_directory="",
        logon_type="",
        user_id="",
        has_triggers=False,
    ):
        self.exists = exists
        self.command = command
        self.arguments = arguments
        self.working_directory = working_directory
        self.run_level = run_level
        self.logon_type = logon_type
        self.user_id = user_id
        self.has_triggers = has_triggers
        self.enabled = enabled
        self.error = error

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"TaskProbe(exists={self.exists!r}, enabled={self.enabled!r}, "
            f"run_level={self.run_level!r})"
        )


# ---------------------------------------------------------
# Privilege detection
# ---------------------------------------------------------
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", ctypes.wintypes.DWORD)]


def _read_process_elevation():
    """Whether this process runs with an elevated token.

    Uses the documented ``TokenElevation`` check on the current process token.
    ``IsUserAnAdmin()`` is not used first because it answers "is the user a
    member of the administrators group", which is a different question when UAC
    is involved.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL

    token = ctypes.wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError("OpenProcessToken failed")
    try:
        elevation = _TOKEN_ELEVATION()
        returned = ctypes.wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_ELEVATION_CLASS,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            raise OSError("GetTokenInformation failed")
        return bool(elevation.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


def is_process_elevated():
    """True when the current process is running elevated (administrator)."""
    if os.name != "nt":
        return False
    try:
        return bool(_read_process_elevation())
    except Exception:
        pass
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------
# Provisioning diagnostics (narrow child -> parent result channel)
# ---------------------------------------------------------
# The elevated helper runs before any application logging exists, and the
# parent cannot see its console output, so a failure used to reach the user as
# "not ready yet". These helpers write the helper's own user-safe reason into
# one fixed file in the application data directory, which the parent reads
# after the helper process has exited.
#
# Constraints that keep this channel harmless:
#   * the file name and location are fixed by this module,
#   * it carries only ``success``, the exit code and one short message,
#   * it is never read to choose which privileged command to run - the parent
#     only formats the text the helper already decided on,
#   * it contains no configuration values, no coordinates, no device addresses
#     and no secrets,
#   * a stale result is deleted before every attempt,
#   * every read/write here is best effort: a failure to use it degrades the
#     message, never the behaviour.
DATA_DIR_ENVIRONMENT_VARIABLE = "LOCALAPPDATA"
FALLBACK_APP_DATA_DIR_NAME = "Yeelight PC Companion"


def provisioning_data_dir():
    """Directory for the provisioning diagnostics, or "" when there is none.

    Mirrors the application's own storage rule (``%LOCALAPPDATA%\\Yeelight PC
    Companion``) without importing the configuration layer, so the elevated
    helper still works when its writable locations differ. The path comes from
    the environment, never from a hardcoded user name.
    """
    base = os.environ.get(DATA_DIR_ENVIRONMENT_VARIABLE) or ""
    if not base:
        return ""
    return os.path.join(base, FALLBACK_APP_DATA_DIR_NAME)


def provision_result_path():
    """Absolute path of the provisioning result file, or "" when unavailable."""
    directory = provisioning_data_dir()
    if not directory:
        return ""
    return os.path.join(directory, PROVISION_RESULT_FILENAME)


def provisioning_log_path():
    """Absolute path of the provisioning log, or "" when unavailable."""
    directory = provisioning_data_dir()
    if not directory:
        return ""
    return os.path.join(directory, PROVISIONING_LOG_FILENAME)


def log_provisioning(message):
    """Best-effort provisioning diagnostics. Never raises, never logs secrets.

    Only the fixed task name, the OpenRGB executable *basename* and task state
    wording belong in here: no configuration contents, no coordinates, no
    device addresses and no unrelated local paths.
    """
    path = provisioning_log_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > PROVISION_LOG_MAX_BYTES:
            # Keep it bounded; a provisioning log is a diagnostic aid, not a
            # second application log.
            with open(path, "w", encoding="utf-8"):
                pass
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{timestamp} - {message}\n")
    except Exception:
        pass


def clear_provision_result():
    """Delete a previous attempt's result. Returns True when it is gone.

    Called before starting the helper and again after reading it, so a stale
    result can neither be mistaken for this attempt's outcome nor be left
    behind in the data directory.
    """
    path = provision_result_path()
    if not path:
        return True
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def write_provision_result(success, message, exit_code=None):
    """Best-effort write of the helper's own result. Returns True on success.

    Contains exactly what the parent needs in order to show the real reason.
    """
    path = provision_result_path()
    if not path:
        return False
    payload = {
        "success": bool(success),
        "message": str(message or "")[:2000],
    }
    if exit_code is not None:
        payload["exit_code"] = int(exit_code)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(temporary, path)
        return True
    except Exception:
        try:
            os.remove(f"{path}.tmp")
        except OSError:
            pass
        return False


def read_provision_result(not_before=None):
    """Read the helper's result. Returns a dict, or None when unusable.

    ``not_before`` rejects an older file, so a result left behind by an earlier
    attempt (for example after a helper crash) is ignored instead of being
    reported as this attempt's outcome. Any malformed or unreadable file is
    treated as "no result", never as an error.
    """
    path = provision_result_path()
    if not path:
        return None
    try:
        if not_before is not None and os.path.getmtime(path) < float(not_before):
            return None
        if os.path.getsize(path) > PROVISION_RESULT_MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except Exception:
        return None
    if not isinstance(payload, dict) or "success" not in payload:
        return None
    return {
        "success": bool(payload.get("success")),
        "message": str(payload.get("message") or "").strip(),
        "exit_code": payload.get("exit_code"),
    }


# ---------------------------------------------------------
# Privileged-target security (what may be launched elevated)
# ---------------------------------------------------------
# An approved highest-privilege task is a standing promise that Windows will
# start *this path* with administrator rights whenever the application asks for
# it. If the unelevated user can replace the file behind that path, the promise
# becomes a persistent privilege-escalation path: the replacement is launched
# elevated on the next wake. Creating the task therefore requires evidence that
# the configured OpenRGB executable - and the folder it lives in - is protected
# from the current user.
#
# The evidence is an *effective rights* check: the object's DACL is evaluated
# against the user's own (unelevated) access token with AccessCheck(), which is
# the same decision Windows itself makes for that user. A path prefix test is
# not enough ("C:\Program Files" is only protected by its ACL, and a copy could
# live anywhere), and os.access() is not usable on Windows here because it does
# not consider token elevation, group membership, deny ACEs or object
# ownership.
#
# Rights that count as "the user can replace or modify the target":
#   FILE_WRITE_DATA        modify the file / add a file to the folder
#   FILE_APPEND_DATA       extend the file / add a subfolder to the folder
#   FILE_WRITE_EA, FILE_WRITE_ATTRIBUTES
#                          rewrite attributes of what the privileged task loads
#   FILE_DELETE_CHILD      delete or rename an entry of the folder
#   DELETE                 delete the file or the folder itself
#   WRITE_DAC              rewrite the permissions, which grants everything above
#   WRITE_OWNER            take ownership, which implies WRITE_DAC
# A folder *above* the executable's own folder is only checked for the rights
# that can reach the executable through it (DELETE, FILE_DELETE_CHILD,
# WRITE_DAC, WRITE_OWNER). Create rights alone are deliberately not checked
# there: adding a file or a subfolder somewhere higher up cannot change what a
# fixed path deeper down resolves to, and replacing the folder in between always
# requires deleting or renaming it first - and that needs DELETE on that folder
# or FILE_DELETE_CHILD on its parent, both of which *are* checked. (Checking
# create rights on ancestors instead would reject legitimate installs, because
# some Windows volume roots grant users the legacy "create folders" right.)
#
# Ownership matters on top of the DACL: an object's owner can always rewrite its
# permissions, so a target owned by the current user counts as replaceable even
# when its DACL looks read-only.
#
# The check runs before the task is created and again on every status query -
# including the wake path - so a target whose permissions change later stops
# being reported as usable instead of being trusted forever.
#
# Everything here fails closed. A missing token, an unreadable security
# descriptor or a failed access check is "could not be verified", and
# provisioning refuses to create the task for any target that is not provably
# protected. Nothing in this section writes anything: it only inspects.
SECURITY_OK = "ok"
SECURITY_USER_WRITABLE = "user_writable"
SECURITY_UNVERIFIED = "unverified"
SECURITY_MISSING = "missing"

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
GROUP_SECURITY_INFORMATION = 0x00000002
DACL_SECURITY_INFORMATION = 0x00000004

TOKEN_DUPLICATE = 0x0002
TOKEN_LINKED_TOKEN_CLASS = 19

# Impersonation levels (``SECURITY_IMPERSONATION_LEVEL``). Which one may be
# asked for when a token is duplicated is dictated by the source token:
# ``DuplicateToken()`` refuses to *raise* the level, so a duplicate can only
# ever keep or lower it. This matters for the UAC linked token below.
SECURITY_ANONYMOUS = 0
SECURITY_IDENTIFICATION = 1
SECURITY_IMPERSONATION = 2
SECURITY_DELEGATION = 3

# Access mask bits (WinNT.h) and the standard file generic mapping used by
# AccessCheck(). Only specific bits are requested below, so the mapping is only
# needed because the API requires it.
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_WRITE_EA = 0x00000010
FILE_DELETE_CHILD = 0x00000040
FILE_WRITE_ATTRIBUTES = 0x00000100
DELETE = 0x00010000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000

FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
FILE_ALL_ACCESS = 0x001F01FF

_REPLACEMENT_RIGHTS = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_WRITE_ATTRIBUTES
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
)
_FOLDER_REPLACEMENT_RIGHTS = _REPLACEMENT_RIGHTS | FILE_DELETE_CHILD
_ANCESTOR_REPLACEMENT_RIGHTS = DELETE | FILE_DELETE_CHILD | WRITE_DAC | WRITE_OWNER

_RIGHT_NAMES = (
    (FILE_WRITE_DATA, "write"),
    (FILE_APPEND_DATA, "append"),
    (FILE_WRITE_EA, "write extended attributes"),
    (FILE_WRITE_ATTRIBUTES, "write attributes"),
    (FILE_DELETE_CHILD, "delete child items"),
    (DELETE, "delete"),
    (WRITE_DAC, "change permissions"),
    (WRITE_OWNER, "take ownership"),
)


class _GENERIC_MAPPING(ctypes.Structure):
    _fields_ = [
        ("GenericRead", ctypes.wintypes.DWORD),
        ("GenericWrite", ctypes.wintypes.DWORD),
        ("GenericExecute", ctypes.wintypes.DWORD),
        ("GenericAll", ctypes.wintypes.DWORD),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", ctypes.wintypes.DWORD),
        ("HighPart", ctypes.wintypes.LONG),
    ]


class _PRIVILEGE_SET(ctypes.Structure):
    # Sized generously: one privilege is normally enough, the API reports a
    # failure instead of overflowing.
    _fields_ = [
        ("PrivilegeCount", ctypes.wintypes.DWORD),
        ("Control", ctypes.wintypes.DWORD),
        ("Privilege", _LUID * 16),
    ]


class _TOKEN_LINKED_TOKEN(ctypes.Structure):
    _fields_ = [("LinkedToken", ctypes.wintypes.HANDLE)]


class ElevationTargetSecurity:
    """Result of inspecting whether a privileged-launch target is protected.

    Truthy exactly when the target may be handed to the highest-privilege task.
    ``reason`` is the technical explanation for the debug log, ``message`` is
    the plain-language text for the user (empty when the target is fine).
    """

    __slots__ = ("is_secure", "code", "reason")

    def __init__(self, is_secure, code, reason=""):
        self.is_secure = is_secure
        self.code = code
        self.reason = reason

    def __bool__(self):
        return bool(self.is_secure)

    @property
    def is_user_writable(self):
        return self.code == SECURITY_USER_WRITABLE

    @property
    def executable_missing(self):
        return self.code == SECURITY_MISSING

    @property
    def message(self):
        """Plain-language explanation, or '' when there is nothing to report."""
        if self.code == SECURITY_USER_WRITABLE:
            return UNSAFE_TARGET_MESSAGE
        if self.code == SECURITY_UNVERIFIED:
            return UNVERIFIED_TARGET_MESSAGE
        return ""

    @property
    def label(self):
        if self.code == SECURITY_USER_WRITABLE:
            return UNSAFE_TARGET_LABEL
        if self.code == SECURITY_UNVERIFIED:
            return UNVERIFIED_TARGET_LABEL
        return ""

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"ElevationTargetSecurity({self.code!r}, secure={self.is_secure!r})"


def _describe_rights(mask):
    return ", ".join(name for bit, name in _RIGHT_NAMES if mask & bit) or "no rights"


def _replacement_check_targets(executable_path):
    """``(path, rights, description)`` for everything that must be protected.

    The executable and the folder that contains it are checked for every way of
    replacing them. Folders above that folder only get the rights that could
    reach the executable *through* them: to replace a folder in the chain, an
    attacker must first delete or rename it, which requires DELETE on the folder
    or FILE_DELETE_CHILD on its parent - checked here as well. The create rights
    (write data, append) are not part of the ancestor mask on purpose: they
    cannot replace anything below them by themselves, and some Windows volume
    roots grant users exactly that right for their own folders, so treating it
    as dangerous would reject protected installations for no gain.
    """
    absolute = os.path.abspath(executable_path)
    folder = os.path.dirname(absolute)
    targets = [(absolute, _REPLACEMENT_RIGHTS, "The OpenRGB executable")]
    while folder:
        first = len(targets) == 1
        targets.append(
            (
                folder,
                _FOLDER_REPLACEMENT_RIGHTS if first else _ANCESTOR_REPLACEMENT_RIGHTS,
                "The OpenRGB folder" if first else "A parent folder",
            )
        )
        parent = os.path.dirname(folder)
        if not parent or os.path.normcase(parent) == os.path.normcase(folder):
            break  # a volume root (C:\) or a UNC share root
        folder = parent
    return targets


def _file_generic_mapping():
    return _GENERIC_MAPPING(
        FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS
    )


def _duplicate_token_at_usable_level(advapi32, source, levels=None):
    """Duplicate ``source`` at the first impersonation level Windows accepts.

    ``DuplicateToken()`` refuses to *raise* a token's impersonation level
    (``ERROR_BAD_IMPERSONATION_LEVEL``, 1346), so a duplicate can only keep or
    lower it. Which level is available depends on the source token, and only
    Windows knows that, so the levels are attempted in order and the first
    accepted one wins.

    Returns a new handle the caller owns, or None when no level is accepted.
    Kept separate from the handle plumbing around it so the fallback itself is
    testable without the real Windows APIs.
    """
    if levels is None:
        levels = (SECURITY_IMPERSONATION, SECURITY_IDENTIFICATION)
    duplicate = ctypes.wintypes.HANDLE()
    for level in levels:
        if advapi32.DuplicateToken(source, level, ctypes.byref(duplicate)):
            if duplicate.value:
                return duplicate
    return None


def _open_standard_user_token():
    """An impersonation token for the *unelevated* user, or None on failure.

    The DACL must be evaluated with the token the user's normal (unelevated)
    processes carry: while the provisioning helper runs elevated, its own token
    is the wrong identity, because administrators may write into otherwise
    protected locations - but only with an elevated token. Windows exposes the
    limited token as the linked token of an elevated token (``TokenLinkedToken``),
    and for an unelevated process the current token already *is* that token.

    Whether this token is elevated comes from the token check itself
    (``TokenElevation``), not from the group-membership fallback: only the token
    can answer whether *this* process holds an elevated token.

    The duplicate keeps an impersonation level the source token actually
    carries. The UAC linked token is an *identification-level* token, so
    demanding ``SecurityImpersonation`` unconditionally - as this used to do -
    made the whole check fail inside the elevated helper while it worked
    unelevated. ``SecurityIdentification`` is not a weaker identity for
    ``AccessCheck()``: the access decision is made from the token's identity
    (SID, groups), and this check only ever queries, it never acts as the user.

    Returns None when the token cannot be produced; callers treat that as
    "could not be verified" and refuse to provision.
    """
    try:
        elevated = bool(_read_process_elevation())
    except Exception:
        # Without knowing which identity this process runs as, no trustworthy
        # access check is possible.
        return None

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL
    advapi32.DuplicateToken.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.POINTER(ctypes.wintypes.HANDLE),
    ]
    advapi32.DuplicateToken.restype = ctypes.wintypes.BOOL

    primary = ctypes.wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(primary)
    ):
        return None
    try:
        source = primary
        linked = None
        if elevated:
            info = _TOKEN_LINKED_TOKEN()
            returned = ctypes.wintypes.DWORD(0)
            if not advapi32.GetTokenInformation(
                primary,
                TOKEN_LINKED_TOKEN_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ctypes.byref(returned),
            ):
                # Elevated without a limited token (for example UAC disabled):
                # "the current user" is then always the elevated user and no
                # protected-location claim can be made.
                return None
            linked = info.LinkedToken
            if not linked:
                return None
            source = linked

        try:
            return _duplicate_token_at_usable_level(advapi32, source)
        finally:
            if linked is not None:
                kernel32.CloseHandle(linked)
    finally:
        kernel32.CloseHandle(primary)


def _close_token(handle):
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - defensive
        pass


def _owner_sid_string(path):
    """String SID of the owner of ``path``, or None when it cannot be read."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.wintypes.LPWSTR,
        ctypes.c_int,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = ctypes.wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
    kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL

    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        path,
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not descriptor.value:
        return None
    try:
        if not owner.value:
            return None
        text = ctypes.wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(text)):
            return None
        try:
            return text.value or None
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


def _effective_access(path, access_mask, token):
    """Rights ``token`` effectively has on ``path``, or None when undeterminable.

    The real Windows access check: deny ACEs, group membership, the object's
    own DACL (including inherited entries) and ownership all take part, exactly
    as they would for the user's own process.

    The owner and group are requested together with the DACL because
    ``AccessCheck`` rejects a DACL-only security descriptor; only the return
    value and the granted mask are used, never the last-error value.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.wintypes.LPWSTR,
        ctypes.c_int,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.wintypes.DWORD
    advapi32.AccessCheck.argtypes = [
        ctypes.c_void_p,
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_GENERIC_MAPPING),
        ctypes.POINTER(_PRIVILEGE_SET),
        ctypes.POINTER(ctypes.wintypes.DWORD),
        ctypes.POINTER(ctypes.wintypes.DWORD),
        ctypes.POINTER(ctypes.wintypes.BOOL),
    ]
    advapi32.AccessCheck.restype = ctypes.wintypes.BOOL
    advapi32.MapGenericMask.argtypes = [
        ctypes.POINTER(ctypes.wintypes.DWORD),
        ctypes.POINTER(_GENERIC_MAPPING),
    ]
    advapi32.MapGenericMask.restype = None
    kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
    kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        path,
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not descriptor.value:
        return None
    try:
        if not dacl.value:
            # No DACL at all means "everyone may do anything" to Windows.
            return access_mask
        mapping = _file_generic_mapping()
        mask = ctypes.wintypes.DWORD(access_mask)
        advapi32.MapGenericMask(ctypes.byref(mask), ctypes.byref(mapping))
        privileges = _PRIVILEGE_SET()
        length = ctypes.wintypes.DWORD(ctypes.sizeof(privileges))
        granted = ctypes.wintypes.DWORD(0)
        status = ctypes.wintypes.BOOL(0)
        if not advapi32.AccessCheck(
            descriptor,
            token,
            mask,
            ctypes.byref(mapping),
            ctypes.byref(privileges),
            ctypes.byref(length),
            ctypes.byref(granted),
            ctypes.byref(status),
        ):
            return None
        return int(granted.value)
    finally:
        kernel32.LocalFree(descriptor)


def _unverified_target(reason):
    return ElevationTargetSecurity(False, SECURITY_UNVERIFIED, reason)


def _user_writable_target(reason):
    return ElevationTargetSecurity(False, SECURITY_USER_WRITABLE, reason)


def is_elevation_target_secure(executable_path):
    """Whether ``executable_path`` may be launched by a highest-privilege task.

    Returns an :class:`ElevationTargetSecurity` (truthy only when the location is
    provably protected). The property established is:

        the unelevated current user must not be able to replace or modify the
        executable - or anything the executable loads from its own folder - that
        a privileged task would later start.

    An executable that is not present yet is reported as
    :data:`SECURITY_MISSING`: there is nothing to replace, the Task Scheduler
    itself fails such a task immediately, and ``validate_openrgb_executable()``
    still refuses to provision one.
    """
    if os.name != "nt":
        return _unverified_target("Windows ACLs are only available on Windows.")
    if not executable_path:
        return _unverified_target("No OpenRGB executable path is configured.")

    absolute = os.path.abspath(str(executable_path))
    if not os.path.isfile(absolute):
        return ElevationTargetSecurity(
            False, SECURITY_MISSING, "The configured OpenRGB executable is not present."
        )

    token = None
    try:
        token = _open_standard_user_token()
        if not token:
            return _unverified_target(
                "The current user's unelevated security token could not be opened."
            )

        current_sid = _current_user_sid()
        if not current_sid:
            return _unverified_target(
                "The current user's security identifier could not be read."
            )

        for path, rights, description in _replacement_check_targets(absolute):
            owner = _owner_sid_string(path)
            if owner is None:
                return _unverified_target(f"The owner of {description} could not be read.")
            if owner.upper() == current_sid.upper():
                return _user_writable_target(
                    f"{description} is owned by the current user, who can always "
                    "change its permissions."
                )

            granted = _effective_access(path, rights, token)
            if granted is None:
                return _unverified_target(
                    f"The permissions of {description} could not be evaluated."
                )
            if granted & rights:
                return _user_writable_target(
                    f"{description} grants the current user "
                    f"{_describe_rights(granted & rights)}."
                )
    except Exception as exc:  # pragma: no cover - defensive, must never raise
        return _unverified_target(f"The target's permissions could not be inspected ({exc}).")
    finally:
        if token:
            _close_token(token)

    return ElevationTargetSecurity(
        True,
        SECURITY_OK,
        "The OpenRGB executable is protected from modification by this user account.",
    )


# ---------------------------------------------------------
# Command-line validation for the narrow provisioning modes
# ---------------------------------------------------------
def validate_openrgb_executable(exe_path):
    """Return the absolute OpenRGB executable path, or raise WindowsTaskError.

    Only the executable path may vary in the privileged task action, so this is
    the single place where it is constrained: an absolute path to an existing
    ``.exe`` file, without control characters and without anything that could be
    read as extra command-line syntax.
    """
    if not isinstance(exe_path, str):
        raise WindowsTaskError("The OpenRGB executable path must be text.")
    text = exe_path.strip()
    if not text:
        raise WindowsTaskError("No OpenRGB executable path is configured.")
    if any(character in text for character in "\r\n\t\0"):
        raise WindowsTaskError("The OpenRGB executable path contains invalid characters.")
    absolute = os.path.abspath(text)
    if not os.path.isabs(absolute):
        raise WindowsTaskError("The OpenRGB executable path must be an absolute path.")
    if os.path.splitext(absolute)[1].lower() != ".exe":
        raise WindowsTaskError("The OpenRGB executable must be an .exe file.")
    if not os.path.isfile(absolute):
        raise WindowsTaskError("The OpenRGB executable could not be found on this PC.")
    return absolute


def parse_provisioning_argv(argv):
    """Interpret the narrow provisioning command line.

    Returns ``(action, path)`` where ``action`` is :data:`ACTION_PROVISION` or
    :data:`ACTION_REMOVE`, or ``None`` when this is an ordinary application
    start. Raises :class:`WindowsTaskError` for a malformed provisioning
    command line.

    The accepted forms are exactly ``--provision-openrgb-task <path>`` and
    ``--remove-openrgb-task``, as the first argument. There is deliberately no
    generic ``--run-command`` / ``--task-name`` / ``--arguments`` interface: the
    task name and the OpenRGB arguments are hardcoded and only the executable
    path is accepted.
    """
    args = [str(item) for item in (argv or [])[1:]]
    if not args:
        return None

    if args[0] not in (PROVISION_FLAG, REMOVE_FLAG):
        if PROVISION_FLAG in args or REMOVE_FLAG in args:
            raise WindowsTaskError("A provisioning flag must be the first argument.")
        return None

    if args[0] == REMOVE_FLAG:
        if len(args) != 1:
            raise WindowsTaskError(f"{REMOVE_FLAG} does not accept any additional arguments.")
        return ACTION_REMOVE, ""

    if len(args) != 2:
        raise WindowsTaskError(
            f"{PROVISION_FLAG} requires exactly one OpenRGB executable path."
        )
    return ACTION_PROVISION, args[1]


def run_provisioning_cli(argv, output=None):
    """Run one elevated provisioning request. Returns a process exit code.

    Returns ``None`` when this is an ordinary application start (neither
    provisioning flag is present), so the caller can continue normally.

    Exit codes (a caller - and the parent that waits for this process - can tell
    the failure classes apart without reading any text):

    * :data:`PROVISION_EXIT_OK` (0) - the requested action succeeded,
    * :data:`PROVISION_EXIT_FAILED` (1) - provisioning failed for another reason,
    * :data:`PROVISION_EXIT_BAD_COMMAND_LINE` (2) - malformed command line,
    * :data:`PROVISION_EXIT_SECURITY` (3) - the protected-location check refused
      the target or could not verify it,
    * :data:`PROVISION_EXIT_TASK_CREATION` (4) - ``schtasks.exe`` refused the
      task definition,
    * :data:`PROVISION_EXIT_NOT_READY` (5) - the task was created but is not
      usable in its resulting state.

    The same reason is written to the narrow result file so the parent can show
    it verbatim; the numeric code is the fallback when it cannot. Nothing else
    happens in this mode: no window, no tray icon, no automation, and no
    configuration read or write.
    """
    print_output = output if output is not None else _print
    try:
        parsed = parse_provisioning_argv(argv)
    except WindowsTaskError as exc:
        print_output(f"ERROR: {exc}")
        log_provisioning(f"Command line rejected: {exc}")
        return PROVISION_EXIT_BAD_COMMAND_LINE
    if parsed is None:
        return None

    action, path = parsed

    # A result from an earlier attempt must never be mistaken for this one.
    clear_provision_result()
    log_provisioning(f"Provisioning requested: {action} (script side).")

    code, diagnostic = _perform_provisioning_action(action, path, print_output)
    log_provisioning(f"Provisioning finished: {action} exit={code}.")
    write_provision_result(code == PROVISION_EXIT_OK, diagnostic, exit_code=code)
    return code


def _perform_provisioning_action(action, path, print_output):
    """Do the one allowed action. Returns ``(exit_code, user_safe_reason)``."""
    if action == ACTION_REMOVE:
        try:
            removed = remove_openrgb_task()
        except WindowsTaskError as exc:
            print_output(f"ERROR: {exc}")
            return PROVISION_EXIT_TASK_CREATION, str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            print_output(f"ERROR: unexpected failure ({type(exc).__name__}).")
            return PROVISION_EXIT_FAILED, (
                f"The task could not be removed ({type(exc).__name__})."
            )
        print_output("Removed." if removed else "No task to remove.")
        return PROVISION_EXIT_OK, "Removed." if removed else "No task to remove."

    try:
        validated = validate_openrgb_executable(path)
    except WindowsTaskError as exc:
        print_output(f"ERROR: {exc}")
        return PROVISION_EXIT_FAILED, str(exc)

    try:
        provision_openrgb_task(validated)
    except SecurityPolicyViolation as exc:
        # The protected-location requirement itself refused the target. This is
        # a security decision, not an environment failure, and it is reported as
        # its own class so the parent can word it that way.
        print_output(f"ERROR: {exc}")
        return PROVISION_EXIT_SECURITY, str(exc)
    except WindowsTaskError as exc:
        # Everything that reaches here is a real Task Scheduler failure: the
        # security check already approved this target before any task work.
        print_output(f"ERROR: {exc}")
        return PROVISION_EXIT_TASK_CREATION, str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        print_output(f"ERROR: unexpected failure ({type(exc).__name__}).")
        return PROVISION_EXIT_FAILED, (
            f"The task could not be created ({type(exc).__name__})."
        )

    status = openrgb_elevation_status(validated)
    if status.is_ready:
        print_output("OpenRGB elevation task is ready.")
        return PROVISION_EXIT_OK, "OpenRGB seamless elevated launch is ready."

    # The task work reported success but the resulting task is not usable. That
    # is a distinct bug from "provisioning failed", so it is reported as its own
    # state with the exact status the inspection produced.
    reason = (
        "Provisioning helper completed, but the task is not ready: "
        f"{status.label or status.state}."
    )
    print_output(f"ERROR: {reason}")
    return PROVISION_EXIT_NOT_READY, reason


def _print(message):  # pragma: no cover - console mode only
    try:
        print(message)
    except Exception:
        pass


# ---------------------------------------------------------
# Task definition
# ---------------------------------------------------------
def _current_user_name():
    """Return ``DOMAIN\\user`` for the current process, or '' when unknown."""
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.GetUserNameW.argtypes = [
            ctypes.wintypes.LPWSTR,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        advapi32.GetUserNameW.restype = ctypes.wintypes.BOOL

        size = ctypes.wintypes.DWORD(0)
        advapi32.GetUserNameW(None, ctypes.byref(size))
        if size.value:
            buffer = ctypes.create_unicode_buffer(size.value + 1)
            if advapi32.GetUserNameW(buffer, ctypes.byref(size)):
                return buffer.value
    except Exception:
        pass

    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    if user:
        return f"{domain}\\{user}" if domain else user
    return ""


TOKEN_USER_CLASS = 1


def _current_user_sid():
    """Return the current user's SID string (``S-1-5-21-...``), or '' if unknown.

    A SID is used for the task principal because it is unambiguous: it needs no
    name resolution by the Task Scheduler service and is exactly the form
    Windows itself writes when it exports a task definition.
    """
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
        kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL
        advapi32.OpenProcessToken.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = ctypes.wintypes.BOOL

        token = ctypes.wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            return ""
        try:
            needed = ctypes.wintypes.DWORD(0)
            advapi32.GetTokenInformation(
                token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed)
            )
            if not needed.value:
                return ""
            # TOKEN_USER is a SID_AND_ATTRIBUTES: the SID pointer comes first.
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                token, TOKEN_USER_CLASS, buffer, needed.value, ctypes.byref(needed)
            ):
                return ""
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            string_sid = ctypes.wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(sid_pointer), ctypes.byref(string_sid)
            ):
                return ""
            try:
                return string_sid.value or ""
            finally:
                kernel32.LocalFree(string_sid)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return ""


def current_task_principal():
    """Best identifier for the task principal: the SID, else ``DOMAIN\\user``."""
    return _current_user_sid() or _current_user_name()


def build_openrgb_task_xml(exe_path, user_id=None):
    """Return the task definition XML for the fixed OpenRGB elevation task.

    The action is built from the validated executable path plus the hardcoded
    OpenRGB arguments, and it always carries an explicit ``<WorkingDirectory>``
    of ``dirname(executable)``. Without it the elevated process would inherit an
    arbitrary Task Scheduler working directory (Windows otherwise defaults it)
    and could load its runtime files from somewhere unexpected. Command,
    arguments and working directory stay separate XML fields - no shell command
    is ever assembled.

    ``<Triggers />`` is intentionally empty: the task must never run on a
    schedule or at logon, only when the application asks for it.
    """
    # Imported here rather than at module level on purpose. `xml.sax.saxutils`
    # pulls in `urllib.request`, and with it `ssl`, `http.client` and `email`;
    # that chain measured ~90 ms of the application's cold import (~20%), and it
    # is fetched for `escape` and nothing else. This function runs only while the
    # OpenRGB elevation task is being created or repaired, so keeping the import
    # at its point of use moves that cost off the startup path completely. The
    # escaping itself is unchanged.
    from xml.sax.saxutils import escape

    principal_user = ""
    if user_id:
        principal_user = f"<UserId>{escape(user_id)}</UserId>"

    working_directory = os.path.dirname(str(exe_path or ""))
    working_directory_element = ""
    if working_directory:
        working_directory_element = (
            f"<WorkingDirectory>{escape(working_directory)}</WorkingDirectory>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="{TASK_XML_VERSION}" xmlns="{TASK_XML_NAMESPACE}">'
        "<RegistrationInfo>"
        f"<Description>{escape(OPENRGB_TASK_DESCRIPTION)}</Description>"
        "</RegistrationInfo>"
        "<Triggers />"
        "<Principals>"
        '<Principal id="Author">'
        f"{principal_user}"
        "<LogonType>InteractiveToken</LogonType>"
        "<RunLevel>HighestAvailable</RunLevel>"
        "</Principal>"
        "</Principals>"
        "<Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<AllowHardTerminate>true</AllowHardTerminate>"
        "<StartWhenAvailable>false</StartWhenAvailable>"
        "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>"
        "<IdleSettings>"
        "<StopOnIdleEnd>false</StopOnIdleEnd>"
        "<RestartOnIdle>false</RestartOnIdle>"
        "</IdleSettings>"
        "<AllowStartOnDemand>true</AllowStartOnDemand>"
        "<Enabled>true</Enabled>"
        "<Hidden>false</Hidden>"
        "<RunOnlyIfIdle>false</RunOnlyIfIdle>"
        "<WakeToRun>false</WakeToRun>"
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
        "<Priority>7</Priority>"
        "</Settings>"
        '<Actions Context="Author">'
        "<Exec>"
        f"<Command>{escape(exe_path)}</Command>"
        f"<Arguments>{escape(OPENRGB_TASK_ARGS_STRING)}</Arguments>"
        f"{working_directory_element}"
        "</Exec>"
        "</Actions>"
        "</Task>"
    )


# ---------------------------------------------------------
# schtasks.exe plumbing
# ---------------------------------------------------------
def schtasks_path():
    """Absolute path of the system ``schtasks.exe``.

    Never resolved through ``PATH``: the provisioning helper may run elevated,
    and a hijacked ``schtasks.exe`` earlier in ``PATH`` would be a privilege
    escalation.
    """
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(system_root, "System32", "schtasks.exe")


def _decode_output(raw):
    """Decode schtasks output: UTF-16 for XML, locale text otherwise."""
    if raw is None or isinstance(raw, str):
        return raw or ""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if raw.count(b"\x00") > (len(raw) // 4):
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
    for encoding in ("utf-8", "mbcs", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")  # pragma: no cover - defensive


def _run_schtasks(args, timeout=SCHTASKS_TIMEOUT_SECONDS):
    """Run schtasks.exe from an argument list (never a shell string).

    Returns ``(returncode, stdout_text, stderr_text)``.
    """
    completed = subprocess.run(
        [schtasks_path()] + [str(item) for item in args],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=timeout,
    )
    return (
        completed.returncode,
        _decode_output(completed.stdout),
        _decode_output(completed.stderr),
    )


def _failure_text(stdout, stderr):
    text = (stderr or "").strip() or (stdout or "").strip()
    if not text:
        return "no details reported"
    return " ".join(text.split())[:400]


def _strip_namespaces(element):
    """Make Task Scheduler XML namespace-insensitive."""
    for child in element.iter():
        if isinstance(child.tag, str) and "}" in child.tag:
            child.tag = child.tag.split("}", 1)[1]


def _normalize_path(value):
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(os.path.expandvars(text)))


def _principal_matches(user_id):
    """Whether a task's ``UserId`` denotes the current user.

    Only a *stated* principal is compared: Task Scheduler omits the element when
    the task uses its default (the current interactive user), and that default
    is exactly what this application wants. The value is matched against both
    forms Windows uses - the string SID and ``DOMAIN\\user`` - because an export
    may contain either.
    """
    text = (user_id or "").strip()
    if not text:
        return True
    candidates = {
        value
        for value in (
            current_task_principal(),
            _current_user_sid(),
            _current_user_name(),
        )
        if value
    }
    return text.lower() in {value.lower() for value in candidates}


# ---------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------
def query_openrgb_task():
    """Inspect the fixed OpenRGB task. Returns a :class:`TaskProbe`."""
    probe = TaskProbe()
    if os.name != "nt":
        probe.error = "Windows Task Scheduler is only available on Windows."
        return probe

    try:
        code, stdout, stderr = _run_schtasks(["/query", "/tn", OPENRGB_TASK_NAME, "/xml"])
    except FileNotFoundError:
        probe.error = "schtasks.exe could not be found."
        return probe
    except OSError as exc:
        probe.error = f"schtasks.exe could not be started ({type(exc).__name__})."
        return probe

    if code != 0:
        # Overwhelmingly this means "no such task". Keep the reported reason so
        # callers can still tell a missing task from a broken installation.
        probe.error = _failure_text(stdout, stderr)
        return probe

    try:
        root = ET.fromstring(stdout)
    except ET.ParseError:
        probe.error = "The task definition could not be read."
        return probe

    _strip_namespaces(root)
    probe.exists = True
    probe.command = (root.findtext("Actions/Exec/Command") or "").strip()
    probe.arguments = (root.findtext("Actions/Exec/Arguments") or "").strip()
    probe.working_directory = (root.findtext("Actions/Exec/WorkingDirectory") or "").strip()
    probe.run_level = (root.findtext("Principals/Principal/RunLevel") or "").strip()
    probe.logon_type = (root.findtext("Principals/Principal/LogonType") or "").strip()
    probe.user_id = (root.findtext("Principals/Principal/UserId") or "").strip()
    enabled_text = (root.findtext("Settings/Enabled") or "").strip().lower()
    probe.enabled = enabled_text != "false"
    # A task without triggers is exported as an empty <Triggers /> element, and a
    # task that has one always carries the triggering element, so a missing or
    # empty <Triggers> means "no triggers". Anything else is reported as a
    # trigger, which makes the status demand a repair instead of trusting it.
    triggers = root.find("Triggers")
    probe.has_triggers = triggers is not None and len(list(triggers)) > 0
    return probe


def openrgb_task_exists():
    """True when a task with the fixed OpenRGB elevation-task name exists."""
    return query_openrgb_task().exists


def openrgb_elevation_status(openrgb_path, integration_is_enabled=True):
    """Describe whether the seamless elevated launch is usable.

    ``openrgb_path`` is the currently *configured and saved* OpenRGB path, so a
    task that still points somewhere else is reported as needing repair instead
    of being silently trusted. The same goes for a task that does not carry the
    identity this application creates (working directory, logon type, principal,
    no triggers), and for an OpenRGB executable that a privileged task may not
    be allowed to launch at all because the current user can modify it.
    """
    if not integration_is_enabled:
        return OpenRgbElevationStatus(
            STATUS_DISABLED,
            "Not used",
            "The OpenRGB integration is disabled, so no elevated launch is needed.",
        )

    configured = (openrgb_path or "").strip()
    if os.name != "nt":
        return OpenRgbElevationStatus(
            STATUS_UNAVAILABLE,
            "Unavailable",
            "Windows Task Scheduler is only available on Windows.",
        )
    if not configured:
        return OpenRgbElevationStatus(
            STATUS_NEEDS_SETUP,
            "Needs setup",
            "No OpenRGB executable path is configured yet.",
            SET_UP_ACTION_LABEL,
        )

    # Before anything else: a highest-privilege task may only ever be pointed at
    # an executable the current user cannot replace. A checked location that is
    # user-writable - or one that could not be verified - can never be Ready,
    # no matter how well the task definition itself matches.
    security = is_elevation_target_secure(configured)
    if not security.is_secure and not security.executable_missing:
        return OpenRgbElevationStatus(
            STATUS_UNSAFE_TARGET,
            security.label or UNSAFE_TARGET_LABEL,
            security.message
            or "Windows could not confirm that the OpenRGB executable is protected.",
            REPAIR_ACTION_LABEL,
        )
    # A target that is not installed yet is not an unsafe location (there is
    # nothing to replace, and Task Scheduler fails such a task immediately);
    # provisioning still refuses it through validate_openrgb_executable().

    probe = query_openrgb_task()
    if not probe.exists:
        return OpenRgbElevationStatus(
            STATUS_NEEDS_SETUP,
            "Needs setup",
            "Windows has not been given one-time approval to start OpenRGB with "
            "administrator rights yet.",
            SET_UP_ACTION_LABEL,
        )

    if not probe.enabled:
        return OpenRgbElevationStatus(
            STATUS_NEEDS_REPAIR,
            "Needs repair",
            "The OpenRGB elevation task is disabled in Windows Task Scheduler.",
            REPAIR_ACTION_LABEL,
        )
    if (probe.run_level or "").strip().lower() != "highestavailable":
        return OpenRgbElevationStatus(
            STATUS_NEEDS_REPAIR,
            "Needs repair",
            "The OpenRGB elevation task does not run with highest privileges.",
            REPAIR_ACTION_LABEL,
        )
    if _normalize_path(probe.command) != _normalize_path(configured):
        return OpenRgbElevationStatus(
            STATUS_PATH_MISMATCH,
            "Task points to a different OpenRGB path",
            "The elevation task still targets the previously configured OpenRGB executable.",
            REPAIR_ACTION_LABEL,
        )
    if (probe.arguments or "").strip() != OPENRGB_TASK_ARGS_STRING:
        return OpenRgbElevationStatus(
            STATUS_NEEDS_REPAIR,
            "Needs repair",
            "The OpenRGB elevation task does not use the expected arguments.",
            REPAIR_ACTION_LABEL,
        )
    # Windows defaults the working directory of an action that does not state
    # one, so a task without it must not be trusted either.
    if _normalize_path(probe.working_directory) != _normalize_path(
        os.path.dirname(configured)
    ):
        return OpenRgbElevationStatus(
            STATUS_NEEDS_REPAIR,
            "Needs repair",
            "The OpenRGB elevation task does not start OpenRGB in the folder that "
            "contains it.",
            REPAIR_ACTION_LABEL,
        )
    # A stated logon type has to be the interactive one; an omitted value is the
    # Task Scheduler default, which is exactly what the task definition asks for.
    stated_logon_type = (probe.logon_type or "").strip().lower()
    if stated_logon_type and stated_logon_type != "interactivetoken":
        return OpenRgbElevationStatus(
            STATUS_NEEDS_REPAIR,
            "Needs repair",
            "The OpenRGB elevation task does not run in the current interactive "
            "user's context.",
            REPAIR_ACTION_LABEL,
        )
    if not _principal_matches(probe.user_id):
        return OpenRgbElevationStatus(
            STATUS_NEEDS_REPAIR,
            "Needs repair",
            "The OpenRGB elevation task belongs to a different user account.",
            REPAIR_ACTION_LABEL,
        )
    if probe.has_triggers:
        return OpenRgbElevationStatus(
            STATUS_NEEDS_REPAIR,
            "Needs repair",
            "The OpenRGB elevation task has a trigger, so Windows could start "
            "OpenRGB on its own. It must only ever run on request.",
            REPAIR_ACTION_LABEL,
        )

    return OpenRgbElevationStatus(
        STATUS_READY,
        "Ready",
        "OpenRGB can be started after wake without any further UAC prompt.",
    )


def provision_openrgb_task(exe_path, user_id=None):
    """Create or update the OpenRGB elevation task.

    Requires administrator rights, because the task runs with highest
    privileges. Refuses any executable the current user could replace - that has
    to be established *before* a persistent privileged task is created, so a
    failed or inconclusive inspection blocks provisioning. Raises
    :class:`WindowsTaskError` on failure.
    """
    if os.name != "nt":
        raise WindowsTaskError("Windows Task Scheduler is only available on Windows.")

    validated = validate_openrgb_executable(exe_path)

    security = is_elevation_target_secure(validated)
    if not security.is_secure:
        logging.warning(
            "[OPENRGB] Refused to provision the elevation task for %s: %s",
            os.path.basename(validated),
            security.reason,
        )
        raise SecurityPolicyViolation(
            security.message
            or "The configured OpenRGB executable could not be verified as protected "
            "from modification by this user account."
        )

    if user_id is None:
        user_id = current_task_principal()

    xml_text = build_openrgb_task_xml(validated, user_id=user_id)

    handle, xml_path = tempfile.mkstemp(prefix="yeelight-openrgb-task-", suffix=".xml")
    stdout = stderr = ""
    code = 1
    try:
        with os.fdopen(handle, "w", encoding="utf-16") as stream:
            stream.write(xml_text)
        code, stdout, stderr = _run_schtasks(
            ["/create", "/tn", OPENRGB_TASK_NAME, "/xml", xml_path, "/f"]
        )
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass

    if code != 0:
        raise WindowsTaskError(
            "The scheduled task could not be created or updated "
            f"({_failure_text(stdout, stderr)})."
        )
    logging.info("[OPENRGB] Scheduled task %s provisioned.", OPENRGB_TASK_NAME)


def remove_openrgb_task():
    """Delete the OpenRGB elevation task.

    Returns True when a task was removed and False when there was nothing to
    remove. Raises :class:`WindowsTaskError` when removal fails for another
    reason. Only the fixed task name is ever touched.
    """
    if os.name != "nt":
        return False
    if not query_openrgb_task().exists:
        return False

    code, stdout, stderr = _run_schtasks(["/delete", "/tn", OPENRGB_TASK_NAME, "/f"])
    if code != 0:
        raise WindowsTaskError(
            f"The scheduled task could not be removed ({_failure_text(stdout, stderr)})."
        )
    logging.info("[OPENRGB] Scheduled task %s removed.", OPENRGB_TASK_NAME)
    return True


def run_openrgb_task():
    """Ask Task Scheduler to start the OpenRGB elevation task now.

    This is the silent wake-time launch path: starting an existing
    highest-privilege task does not raise a UAC prompt.
    """
    if os.name != "nt":
        raise WindowsTaskError("Windows Task Scheduler is only available on Windows.")

    code, stdout, stderr = _run_schtasks(["/run", "/tn", OPENRGB_TASK_NAME])
    if code != 0:
        raise WindowsTaskError(
            "Windows could not start the OpenRGB elevation task "
            f"({_failure_text(stdout, stderr)})."
        )


# ---------------------------------------------------------
# Task-aware stop (sleep path)
# ---------------------------------------------------------
# The suspend sequence is a hard real-time path: `_execute_suspend_actions()`
# aims for roughly <1.5 s in total because Windows may freeze user-space work
# about 2 s after the suspend notification. Every phase of that sequence is
# bounded *globally*, never per call site:
#
#   kill Chroma Connector (native, ~50 ms measured)
#   + connector release wait  SUSPEND_CONNECTOR_RELEASE_SECONDS   0.35 s
#   + Yeelight OFF fan-out    SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS  0.35 s
#   + OpenRGB task stop       END_TASK_STOP_BUDGET_SECONDS        0.40 s  <- here
#   + native controller terminate  SUSPEND_NATIVE_TERMINATE_SECONDS  0.35 s
#   ----------------------------------------------------------------
#   budgeted worst case                                           1.45 s
#   (against the 1.5 s target of `SUSPEND_SEQUENCE_TARGET_SECONDS`, which
#   `yeelight_pc_companion.suspend_budgeted_seconds()` asserts and the test
#   suite pins)
#
# The measured happy path of the task stop is 85-108 ms (see project_memory.md
# §4a/§4b), re-measured on the real machine during Stage 7 hardening as
# 0.109-0.138 s including the verification (whole suspend sequence 0.47-0.50 s),
# so this 0.40 s is a guard, not the expected duration - and it is
# deliberately the *complete* operation: the `schtasks /end` invocation, its
# process-exit verification and any waiting between the two.
#
# What it must never become again: a 30 s `schtasks /query` before the stop plus
# a 5 s `/end` timeout plus a 2 s post-stop poll. Those were three independent
# per-call guards that multiplied into a multi-second worst case on a path that
# owns less than half a second. A measured fast average does not buy a slow
# failure budget.
#
#: Complete global budget for :func:`end_openrgb_task`, in seconds.
END_TASK_STOP_BUDGET_SECONDS = 0.4

#: How long the module waits between two process-existence checks. Small enough
#: to fit several checks, large enough not to spin a core while sleeping.
END_TASK_VERIFY_INTERVAL_SECONDS = 0.05

#: `schtasks.exe` is passed whatever is left of the global deadline. If it
#: cannot even be started in this much time, the stop fails immediately.
END_TASK_MIN_SCHTASKS_SECONDS = 0.05

#: Outcome codes for :func:`end_openrgb_task`.
END_TASK_STOPPED = "stopped"
END_TASK_NOT_RUNNING = "not_running"
END_TASK_ABSENT = "absent"
END_TASK_FAILED = "failed"

#: `schtasks /end` wording, as reported by Windows, when the task does not exist
#: or exists but is not currently running. Matched so the sleep path can classify
#: the outcome without paying for a separate `/query` first. The absent markers
#: are checked first because they are the more specific report.
END_TASK_ABSENT_MARKERS = (
    "cannot find the file specified",
    "cannot find the path specified",
)
END_TASK_NOT_RUNNING_MARKERS = (
    "not currently running",
    "not running",
)


#: Image name of the process the elevation task launches. Kept local so this
#: module does not import the GUI application's integration table.
INTEGRATIONS_OPENRGB_PROCESS = "OpenRGB.exe"


def _openrgb_process_ids():
    """PIDs of running ``OpenRGB.exe`` processes, or None if unreadable."""
    if os.name != "nt":
        return None

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (-1, 0):
        return None

    target = INTEGRATIONS_OPENRGB_PROCESS.lower()
    found = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        more = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while more:
            try:
                name = entry.szExeFile.decode("utf-8", errors="ignore").strip().lower()
                if name == target:
                    found.append(int(entry.th32ProcessID))
            except Exception:
                pass
            more = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    except Exception:
        return None
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass
    return found


def _end_task_monotonic():
    """The monotonic clock used for the sleep-path deadline.

    A module-level indirection, not a lambda at the call site, so a test can
    substitute a fake clock and assert the timing contract deterministically
    without ever sleeping for real.
    """
    return time.monotonic()


def _end_task_sleep(seconds):
    """The sleep used inside the global deadline window (patchable in tests)."""
    time.sleep(seconds)


def _end_task_output(result):
    """The combined, normalised `schtasks` output of an ``(code, stdout, stderr)``.

    Returns ``(returncode, stdout, stderr, normalised_lowercase_text)``.
    """
    try:
        code, stdout, stderr = result
    except (TypeError, ValueError):  # pragma: no cover - defensive
        code, stdout, stderr = 1, "", ""
    stdout = stdout or ""
    stderr = stderr or ""
    text = " ".join(f"{stdout} {stderr}".split()).lower()
    return code, stdout, stderr, text


def end_openrgb_task():
    """Stop the OpenRGB instance owned by the elevation task.

    This is the **task-aware** counterpart of :func:`run_openrgb_task`, and it is
    what makes an unelevated application able to stop an elevated OpenRGB.

    Why this exists (verified on a real machine, see ``project_memory.md`` §4b):
    the elevation task runs OpenRGB with ``HighestAvailable``, so the process is
    high-integrity. An unelevated process cannot reliably terminate it through
    ``OpenProcess(PROCESS_TERMINATE)`` - that is exactly the privilege boundary
    the task exists to cross. Task Scheduler, however, will happily terminate a
    task's own process tree on behalf of the task's owner: ``schtasks /end /tn
    YeelightPCCompanion-OpenRGB`` returns exit code 0 from an unelevated context
    and the elevated ``OpenRGB.exe`` really does exit.

    **The whole operation shares ONE small monotonic deadline**
    (:data:`END_TASK_STOP_BUDGET_SECONDS`, 0.4 s), which covers the `schtasks`
    invocation, the process-exit verification and every wait in between. There is
    deliberately no task-existence query first: the suspend path must not spend a
    multi-second guard finding out that there is nothing to stop, and an absent
    task simply produces a bounded :data:`END_TASK_ABSENT` - the caller's native
    same-integrity terminate still runs afterwards. When the deadline expires the
    function reports failure immediately and the caller continues its sequence.

    This function never raises, never elevates, never prompts, never probes the
    filesystem, never validates configuration and never resolves a name: the
    sleep path must continue into its remaining steps no matter what happens
    here.

    Returns one of :data:`END_TASK_STOPPED`, :data:`END_TASK_NOT_RUNNING`,
    :data:`END_TASK_ABSENT` or :data:`END_TASK_FAILED`.
    """
    if os.name != "nt":
        return END_TASK_ABSENT

    deadline = _end_task_monotonic() + END_TASK_STOP_BUDGET_SECONDS

    def remaining():
        return deadline - _end_task_monotonic()

    try:
        asked = _run_schtasks(
            ["/end", "/tn", OPENRGB_TASK_NAME],
            timeout=max(END_TASK_MIN_SCHTASKS_SECONDS, remaining()),
        )
    except Exception as exc:  # timeout, missing schtasks.exe, OSError, ...
        logging.warning("[OPENRGB] Could not ask Task Scheduler to stop OpenRGB: %s", exc)
        return END_TASK_FAILED

    try:
        code, stdout, stderr, text = _end_task_output(asked)
    except Exception:  # pragma: no cover - defensive
        return END_TASK_FAILED

    if code != 0:
        # An absent or idle task makes `/end` fail on some Windows builds. That is
        # classified from the reported wording (no `/query` is spent on finding
        # out) and is not an error worth reporting loudly. The absent wording is
        # tested first: "cannot find the file specified" is the more specific
        # report, and a not-running report must never be read as "no such task".
        if any(marker in text for marker in END_TASK_ABSENT_MARKERS):
            logging.debug("[OPENRGB] No OpenRGB elevation task to stop.")
            return END_TASK_ABSENT
        if any(marker in text for marker in END_TASK_NOT_RUNNING_MARKERS):
            logging.info("[OPENRGB] The OpenRGB elevation task is not currently running.")
            return END_TASK_NOT_RUNNING
        logging.info(
            "[OPENRGB] Task Scheduler did not report a stop for %s (%s).",
            OPENRGB_TASK_NAME,
            _failure_text(stdout, stderr),
        )
        return END_TASK_FAILED

    # Task Scheduler accepted the stop. Confirm the process actually went away
    # rather than trusting the exit code - but only inside the same deadline.
    while True:
        try:
            remaining_ids = _openrgb_process_ids()
        except Exception:  # pragma: no cover - defensive
            remaining_ids = None

        if remaining_ids == []:
            logging.info("[OPENRGB] OpenRGB stopped through its elevation task.")
            return END_TASK_STOPPED

        if remaining() <= END_TASK_VERIFY_INTERVAL_SECONDS:
            logging.warning(
                "[OPENRGB] Task Scheduler accepted the stop, but OpenRGB is still running "
                "after the %.2fs suspend budget for this step.",
                END_TASK_STOP_BUDGET_SECONDS,
            )
            return END_TASK_FAILED

        _end_task_sleep(END_TASK_VERIFY_INTERVAL_SECONDS)


def openrgb_process_running():
    """Whether an ``OpenRGB.exe`` process is currently running.

    Returns None when the process list could not be read, so callers can report
    "unknown" instead of inventing a definite answer.
    """
    pids = _openrgb_process_ids()
    if pids is None:
        return None
    return bool(pids)


# ---------------------------------------------------------
# One-time elevated provisioning
# ---------------------------------------------------------
def _python_executable():
    executable = sys.executable or "python.exe"
    if os.path.basename(executable).lower() == "python.exe":
        windowless = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.isfile(windowless):
            return windowless
    return executable


def _self_invocation(cli_args):
    """Command line that re-invokes this application in provisioning mode."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + list(cli_args)

    script = ""
    if sys.argv and sys.argv[0]:
        script = os.path.abspath(sys.argv[0])
    if not script or not os.path.isfile(script):
        # Fall back to the file of the running __main__ module (for example when
        # the application was started with `python -m`).
        main_module = sys.modules.get("__main__")
        candidate = getattr(main_module, "__file__", "") or ""
        script = os.path.abspath(candidate) if candidate else ""
    if not script or not os.path.isfile(script):
        raise WindowsTaskError("The application could not be re-launched for one-time approval.")
    return [_python_executable(), script] + list(cli_args)


class _SHELLEXECUTEINFOW(ctypes.Structure):
    """The documented ``SHELLEXECUTEINFOW`` structure.

    Only the fields this module sets are used, but the full documented layout is
    declared because ``cbSize`` must match ``sizeof()`` exactly: Windows
    validates it and rejects the call otherwise. ``hProcess`` exists only
    because ``SEE_MASK_NOCLOSEPROCESS`` is requested.
    """

    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.wintypes.HWND),
        ("lpVerb", ctypes.wintypes.LPCWSTR),
        ("lpFile", ctypes.wintypes.LPCWSTR),
        ("lpParameters", ctypes.wintypes.LPCWSTR),
        ("lpDirectory", ctypes.wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.wintypes.LPCWSTR),
        ("hkeyClass", ctypes.wintypes.HKEY),
        ("dwHotKey", ctypes.wintypes.DWORD),
        ("hIcon", ctypes.wintypes.HANDLE),
        ("hProcess", ctypes.wintypes.HANDLE),
    ]


def _shell_execute_ex_runas(executable, arguments):
    """Request one elevated run and return a handle to the elevated process.

    Returns ``(result, process_handle)``:

    * ``result > 32`` and a usable handle - Windows started the helper; the
      caller owns the handle and must close it,
    * ``result == SE_ERR_ACCESSDENIED`` - the user declined the UAC prompt (no
      handle),
    * ``result <= 32`` - the request itself failed (no handle).

    ``SEE_MASK_NOCLOSEPROCESS`` is what makes the difference to the plain
    ``ShellExecuteW()`` form: without it the parent has no way to tell "the
    helper ran and failed" from "the helper never ran", which is exactly the
    blind spot that made an elevate-then-fail helper look like a timeout.
    """
    parameters = subprocess.list2cmdline([str(item) for item in arguments])
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = parameters
    info.lpDirectory = os.path.dirname(executable) or None
    info.nShow = SW_SHOWNORMAL

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
        shell32.ShellExecuteExW.restype = ctypes.wintypes.BOOL
        started = shell32.ShellExecuteExW(ctypes.byref(info))
    except Exception:
        # Without shell32 there is no way to ask for the elevation at all.
        return -1, None
    if started:
        handle = info.hProcess
        return 42, (handle if handle else None)

    last_error = ctypes.get_last_error()
    if last_error == ERROR_CANCELLED:
        return SE_ERR_ACCESSDENIED, None
    # ShellExecuteExW's failure code is the Win32 error. Keep the historical
    # "<= 32 means failure" shape for the caller.
    return (last_error if 0 < last_error <= 32 else 2), None


def _wait_for_process(handle, timeout_seconds):
    """Wait for ``handle``. Returns the exit code, or None on timeout/failure.

    Waiting on the helper's own process is what replaced the blind polling loop:
    the parent now knows the helper finished instead of guessing from whether a
    task appeared meanwhile.
    """
    if not handle:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    kernel32.TerminateProcess.restype = ctypes.wintypes.BOOL

    milliseconds = INFINITE
    if timeout_seconds is not None:
        milliseconds = int(max(0.0, float(timeout_seconds)) * 1000)
    try:
        waited = kernel32.WaitForSingleObject(handle, milliseconds)
    except Exception:  # pragma: no cover - defensive
        return None

    if waited == WAIT_TIMEOUT:
        # A helper that ignored its timeout might still finish while we give up
        # on it. Giving up must not leave a privileged process behind, and this
        # is the one case where terminating is clearly the safe choice: the
        # helper is only ever asked to create or delete one fixed task, and it
        # never touches anything else in this mode.
        try:
            finished = False
            for _attempt in range(2):
                if kernel32.WaitForSingleObject(
                    handle, int(PROCESS_EXIT_GRACE_SECONDS * 1000)
                ) != WAIT_TIMEOUT:
                    finished = True
                    break
            if not finished:
                kernel32.TerminateProcess(handle, PROVISION_EXIT_FAILED)
                kernel32.WaitForSingleObject(handle, int(PROCESS_EXIT_GRACE_SECONDS * 1000))
        except Exception:  # pragma: no cover - defensive
            pass
        return None
    if waited != WAIT_OBJECT_0:
        return None

    try:
        code = ctypes.wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value == STILL_ACTIVE:  # pragma: no cover - defensive
            return None
        return int(code.value)
    except Exception:  # pragma: no cover - defensive
        return None


def _close_process_handle(handle):
    """Close a process handle. The handle always ends up closed."""
    if not handle:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - defensive
        pass


class ProvisionOutcome:
    """What one elevated provisioning attempt produced.

    ``message`` is always filled in, so the UI never has to fall back to a
    vague "not ready yet". ``exit_code`` is None when the helper could not be
    waited on at all.
    """

    __slots__ = ("ok", "message", "exit_code")

    def __init__(self, ok, message, exit_code=None):
        self.ok = bool(ok)
        self.message = message
        self.exit_code = exit_code

    def __iter__(self):
        # ``ok, message = request_elevated_openrgb_provisioning(...)`` stays
        # valid for every existing caller.
        return iter((self.ok, self.message))

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"ProvisionOutcome(ok={self.ok!r}, exit_code={self.exit_code!r})"


def _declined_message(action=ACTION_PROVISION):
    if action == ACTION_REMOVE:
        return (
            "Administrator approval was declined, so the OpenRGB launch task "
            "could not be removed."
        )
    return (
        "Administrator approval was declined, so OpenRGB cannot be started "
        "after wake without a UAC prompt yet."
    )


def _action_subject(action):
    return PROVISION_ACTION_SUBJECTS.get(
        action, "OpenRGB elevated launch could not be configured."
    )


def _outcome_from_exit_code(exit_code, detail, status, action=ACTION_PROVISION):
    """Turn the helper's exit code (and result) into a user-facing outcome."""
    subject = _action_subject(action)
    if exit_code == PROVISION_EXIT_OK:
        # The helper reported success, so the task must exist and be usable. If
        # the inspection disagrees, that is its own bug and is reported as one.
        if status is not None and status.is_ready:
            return ProvisionOutcome(True, "OpenRGB seamless elevated launch is ready.", 0)
        label = status.label if status is not None else "the task was not found"
        return ProvisionOutcome(
            False,
            f"Provisioning helper completed, but the task is not ready: {label}.",
            0,
        )

    # The helper's own message is already a complete sentence with the specific
    # reason, so it is shown as the report itself. The exit-code fallback is
    # generic, so it is prefixed with what failed.
    if detail:
        return ProvisionOutcome(False, detail, exit_code)
    reason = PROVISION_EXIT_MESSAGES.get(
        exit_code, "The provisioning helper reported a failure."
    )
    return ProvisionOutcome(False, f"{subject}\n\n{reason}", exit_code)


def _finish_elevated_provisioning(action, exit_code, status, result):
    """Log and convert one finished helper run into its outcome.

    The helper's own diagnostic wins over the bare exit code: the code says what
    class of failure happened, the message says what actually went wrong.
    """
    detail = ""
    if isinstance(result, dict):
        detail = result.get("message") or ""
        if result.get("exit_code") is not None:
            exit_code = result.get("exit_code")
    else:
        log_provisioning("The helper wrote no result file; using its exit code only.")

    # The result file is transient diagnostics: read once, then remove it.
    clear_provision_result()

    outcome = _outcome_from_exit_code(exit_code, detail, status, action=action)
    log_provisioning(
        f"Helper exited with code {exit_code}; "
        f"{'success' if outcome.ok else 'reported failure'}. "
        f"Result file {'present' if detail else 'absent'}."
    )
    return outcome


def run_elevated_provisioning(
    action,
    validated_path,
    timeout_seconds=PROVISIONING_TIMEOUT_SECONDS,
    launcher=None,
    waiter=None,
    status_query=None,
    result_reader=None,
    clock=None,
):
    """Run one elevated provisioning helper and report what actually happened.

    The sequence is: launch the helper, wait for it to exit, read its exit code,
    read the narrow diagnostic result it wrote, and *then* inspect the resulting
    task state. Nothing here polls for the task to appear.

    Never raises; returns a :class:`ProvisionOutcome`.
    """
    launcher = launcher or _shell_execute_ex_runas
    waiter = waiter or _wait_for_process
    status_query = status_query or openrgb_elevation_status
    result_reader = result_reader or read_provision_result
    clock = clock or time.time

    try:
        argv = _self_invocation(
            [PROVISION_FLAG if action == ACTION_PROVISION else REMOVE_FLAG]
            + ([validated_path] if action == ACTION_PROVISION else [])
        )
    except WindowsTaskError as exc:
        return ProvisionOutcome(False, str(exc))

    # A result from an earlier attempt must never be read back as this one's.
    clear_provision_result()
    started_at = clock()

    log_provisioning(
        f"Launching elevated helper for {action} "
        f"(target: {os.path.basename(validated_path) or 'none'})."
    )
    try:
        result, handle = launcher(argv[0], argv[1:])
    except Exception as exc:
        # A launch that cannot even be attempted is reported, never raised: the
        # Settings action must not fail silently or take the UI down.
        log_provisioning(f"The elevated helper could not be launched ({type(exc).__name__}).")
        return ProvisionOutcome(
            False,
            "The one-time administrator approval could not be requested "
            f"({type(exc).__name__}).\n\nNothing was changed.",
            None,
        )
    if result == SE_ERR_ACCESSDENIED:
        log_provisioning("The administrator approval was declined.")
        return ProvisionOutcome(False, _declined_message(action), None)
    if result <= 32 or not handle:
        log_provisioning(f"The elevated helper could not be started (result {result}).")
        return ProvisionOutcome(
            False,
            "The one-time administrator approval could not be requested "
            f"(Windows error {result}).\n\nNothing was changed.",
            None,
        )

    exit_code = None
    try:
        exit_code = waiter(handle, timeout_seconds)
    except Exception as exc:
        # The wait itself failing is not a reason to leak the handle, and it is
        # reported rather than raised: the caller only needs to know the helper
        # could not be observed.
        log_provisioning(f"Waiting for the helper failed ({type(exc).__name__}).")
        exit_code = None
    finally:
        # Whatever happened above, the handle is closed exactly once.
        _close_process_handle(handle)

    if exit_code is None:
        log_provisioning("The elevated helper did not finish within the timeout.")
        status = _safe_status(status_query, validated_path)
        if status is not None and status.is_ready:
            # It may have finished the task and only failed to report back.
            clear_provision_result()
            return ProvisionOutcome(True, "OpenRGB seamless elevated launch is ready.", 0)
        return ProvisionOutcome(
            False,
            "OpenRGB elevated launch could not be configured.\n\n"
            "The one-time administrator approval did not finish within "
            f"{int(max(0.0, float(timeout_seconds)))} seconds, so the task was not "
            "created. No task state was changed.",
            None,
        )

    status = _safe_status(status_query, validated_path)
    result = result_reader(not_before=started_at)
    return _finish_elevated_provisioning(action, exit_code, status, result)


def _safe_status(status_query, openrgb_path):
    """Inspect the resulting task state, or None when it cannot be read."""
    try:
        return status_query(openrgb_path)
    except Exception:
        return None


def request_elevated_openrgb_provisioning(
    openrgb_path, timeout_seconds=PROVISIONING_TIMEOUT_SECONDS
):
    """Provision the elevation task with one administrator approval.

    Returns ``(ok, message)`` (iterable as a :class:`ProvisionOutcome`) and never
    raises. When the application is already elevated the task is provisioned
    in-process; otherwise a narrowly scoped, elevated provisioning run of this
    same application is requested once and **waited for**, so a helper that
    starts and then fails is reported with its own reason instead of as a
    timeout.

    An executable that the current user could replace - or one that cannot be
    verified as protected - is refused here, before any administrator approval
    is requested: there is no point in asking for UAC for a task that must not
    be created.
    """
    try:
        validated = validate_openrgb_executable(openrgb_path)
    except WindowsTaskError as exc:
        return ProvisionOutcome(False, str(exc))

    security = is_elevation_target_secure(validated)
    if not security.is_secure:
        logging.warning(
            "[OPENRGB] Seamless elevated launch is not available for %s: %s",
            os.path.basename(validated),
            security.reason,
        )
        return ProvisionOutcome(
            False,
            security.message
            or "The configured OpenRGB executable could not be verified as protected "
            "from modification by this user account.",
        )

    if is_process_elevated():
        try:
            provision_openrgb_task(validated)
        except WindowsTaskError as exc:
            return ProvisionOutcome(False, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return ProvisionOutcome(
                False, f"The task could not be created ({type(exc).__name__})."
            )
        return ProvisionOutcome(True, "OpenRGB seamless elevated launch is ready.", 0)

    return run_elevated_provisioning(
        ACTION_PROVISION, validated, timeout_seconds=timeout_seconds
    )


# ---------------------------------------------------------
# Lifecycle policy used by the UI
# ---------------------------------------------------------
def openrgb_task_action(enabled_before, path_before, enabled_after, path_after):
    """Which scheduled-task action a saved configuration change requires.

    * OpenRGB disabled                      -> remove the task
    * OpenRGB enabled but no path configured -> nothing (validation blocks this)
    * newly enabled, or the path changed     -> provision or update the task
    * unchanged                              -> nothing
    """
    if not enabled_after:
        return ACTION_REMOVE
    if not (path_after or "").strip():
        return ACTION_NONE
    if not enabled_before:
        return ACTION_PROVISION
    if _normalize_path(path_before) != _normalize_path(path_after):
        return ACTION_PROVISION
    return ACTION_NONE


def apply_openrgb_task_action(action, openrgb_path, provisioner=None):
    """Perform a task lifecycle action for an already saved configuration.

    Never raises: a failed cleanup or a declined approval must not make the
    application unusable, and must never roll back a valid configuration.
    Returns ``(ok, message)``.
    """
    provisioner = provisioner or request_elevated_openrgb_provisioning
    try:
        if action == ACTION_REMOVE:
            try:
                removed = remove_openrgb_task()
            except WindowsTaskError as exc:
                logging.warning(
                    "[OPENRGB] The %s scheduled task could not be removed: %s",
                    OPENRGB_TASK_NAME,
                    exc,
                )
                return False, (
                    "The OpenRGB elevation task could not be removed automatically, "
                    "but OpenRGB stays disabled in this application."
                )
            if removed:
                logging.info("[OPENRGB] Elevated launch task removed.")
            return True, ""

        if action == ACTION_PROVISION:
            ok, message = provisioner(openrgb_path)
            if ok:
                logging.info("[OPENRGB] Elevated launch task is ready.")
            else:
                logging.warning("[OPENRGB] Elevated launch task is not ready: %s", message)
            return ok, message

        return True, ""
    except Exception:
        logging.exception("[OPENRGB] Unexpected error while updating the scheduled task.")
        return False, (
            "An unexpected error occurred while updating the OpenRGB launch task. "
            "OpenRGB may be skipped after wake until this is repaired."
        )
