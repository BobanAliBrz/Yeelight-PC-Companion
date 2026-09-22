# Yeelight PC Companion — Project Memory

> **Read this file before making changes.** This is the canonical project context for coding agents and maintainers.
>
> Keep it current. If a change materially alters architecture, behavior, configuration, build/release flow, naming, important constraints, or known issues, update this file in the same change. Also update `changelog.md` for user-visible or noteworthy development changes.

## 1. Project identity

- **Canonical/public project name:** **Yeelight PC Companion**
- **Repository:** `BobanAliBrz/Yeelight-PC-Companion`
- **Platform:** Windows
- **Language/UI:** Python + PyQt6
- **Application type:** Desktop system-tray utility with a configuration/dashboard window.
- **Current repository state:** Release-ready open-source tree for the first public release.
- **Former internal name:** **Lumina** (with the `Slon Inc` metadata vendor). The source module, packaging, startup scripts, Windows metadata, AppUserModelID, scheduled task, log file, window/tray text, and icon filename have been renamed to the Yeelight PC Companion naming as part of the *Rename & Packaging Foundation* change.
- **Rename status:** complete for all active source/build/runtime branding. `Lumina` only survives where it is deliberately historical or migration-related (changelog history, the historical note above, the `.gitignore` legacy-log compatibility rule, and the legacy scheduled-task removal in `register_startup.bat`).
- **License:** **GPL-3.0-only** (`LICENSE`), chosen because PyQt6 is itself `GPL-3.0-only` under its open-source terms. Direct dependencies are all compatible (§11).
- **Version source of truth:** `app_metadata.py` (`VERSION = (1, 0, 0)`). Since Stage 7 nothing else may hard-code a version: `version_info.txt` and `installer/version.iss` are *generated* from it (§10b).
- **Current release:** `v1.0.0`, the first public GitHub release.

## 1b. Release foundation (Stage 7)

Stage 7 adds the installation experience and the release tooling. It deliberately
does **not** publish anything, rewrite history, or add an updater.

- **The application runs UNELEVATED.** This is now a verified design property, not
  an assumption (§10a). Only OpenRGB needs administrator rights, and it gets them
  through its own one-time-approved task `YeelightPCCompanion-OpenRGB` (§4b).
- **Startup is a per-user `HKCU\...\Run` entry**, owned and removed by the
  installer. The retired highest-privilege logon task is gone (§10).
- **The installer is Inno Setup 6**, per-user by default (§10c).
- **Artifact privacy is enforced per artifact kind** by
  `tools/release_privacy_scan.py` (§13).
- **Two release artifacts:** an installer and a portable ZIP, differing exactly by
  `portable.flag` (§10c).
- **CI** (`.github/workflows/ci.yml`) and a **tag-triggered build-only release
  workflow** (`.github/workflows/release.yml`) exist; the release workflow uploads
  artifacts to the workflow run and never creates a GitHub Release (§10d).

## 2. What the application does

Yeelight PC Companion keeps the user's room lighting and local RGB software synchronized with the Windows PC's lifecycle and the local day/night cycle.

The important behavior is:

1. **PC sleep / shutdown / logoff**
   - Detect the Windows power/session transition reliably.
   - Stop the Yeelight Chroma Connector first so it releases Yeelight music-mode control.
   - Turn off every **enabled configured** Yeelight device quickly over the LAN.
   - Stop selected RGB controller applications.

2. **PC wake / application startup**
   - Restore RGB software in a controlled sequence.
   - Determine whether it is currently dark using local solar calculations.
   - At night, turn on the enabled Yeelight devices and start the Yeelight Chroma Connector.
   - During daytime, keep/turn the enabled Yeelight devices off and make sure the connector is not running.
   - Coordinate OpenRGB, Razer Synapse, Artemis, and the Yeelight connector.

3. **While the PC is running**
   - Recalculate the solar state periodically.
   - Reconcile the real running state with the expected day/night state so the system can self-heal after crashes, missed transitions, clock changes, or other mismatches.
   - Expose current service state, settings, manual controls, and logs through the PyQt6 UI and system tray.

This began as a personal utility for a specific Windows/RGB setup. The open-source work should progressively make setup safer and more general without breaking the behavior that already works for the original user.

## 3. Supported/integrated software and devices

### Yeelight

An **arbitrary number of Yeelight LAN devices** is supported, persisted as the
`lights.devices` list (§8). Each device has a stable id, a friendly name, an
address and an enable flag. Devices can be discovered on the LAN or added by
hand, and each one can be enabled/disabled, edited or removed independently.

Normal Yeelight operations use the `yeelight` Python package. The suspend-critical power-off path intentionally uses a small raw TCP command to port **55443** for speed.

Every device is addressed on its own: one unreachable device never stops the
other devices from being switched (§8c).

### Local RGB/control applications

The current orchestration knows about:

- **OpenRGB** (`OpenRGB.exe`)
- **Yeelight Chroma Connector** (`Yeelight Chroma Connector.exe`)
- **Razer Synapse 3** (`Razer Synapse 3.exe`)
- **Artemis 2** (`Artemis.UI.Windows.exe`)

These are configured by executable path in `config.json`.

### Artemis integration

`SolarEngineThread` publishes the solar state to the local Artemis JSON module API:

- Schema: `http://localhost:9696/json-modules/SunsetInfo/schema`
- Data: `http://localhost:9696/json-modules/SunsetInfo/data`

Payload data contains `isDark` and `statusText`. Failure to reach Artemis is expected/normal when Artemis is not running and should not crash the app.

## 4. Core architecture

The main implementation is currently a single large module: `yeelight_pc_companion.py`.

### `YeelightPCCompanionWindow`

Main `QMainWindow`. It owns configuration access (through `ConfigManager`), the sidebar/page UI, system tray integration, timers, the solar engine thread, the restore thread, sleep/wake actions, and power-detection objects.

Window structure after the **Stage 5 redesign (presentation only)**:

- A left **navigation sidebar** (one `SidebarButton` per page, exactly one checked, accent-marked) next to a `QStackedWidget` of pages: **Overview**, **Devices**, **Integrations**, **Automation**, **Logs** (module constants `PAGE_*`, `NAV_PAGES`; `select_page(name)` keeps the sidebar, the title and the action bar in step).
- A content header with the page title/subtitle on the left and the live system-status pill on the right. The pill *is* `lbl_system_status`; `trigger_suspend()` / `trigger_resume()` / `on_resume_completed()` update it through `_set_system_status(text, tone)` (`System active` / `Suspending` / `Restoring...`). The state machine and the action sequences behind those handlers are unchanged.
- One shared action area at the bottom of the pages that edit configuration (**Save Changes** primary, **Import Configuration** / **Export Configuration** secondary), instead of a button row per page. Overview and Logs hide it.
- Page ownership of the configuration controls (nothing moved between pages semantically, only visually):
  - **Overview** — day/night state (`lbl_sun_state`), expected action (`lbl_action_req`), `lbl_device_summary`, the coordinates as secondary detail (`lbl_lat`/`lbl_lon`), the four service rows (`service_badges`, filled by the unchanged `check_system_statuses()`), and the two manual actions (`trigger_resume` / `trigger_suspend`). The sleep button is labelled **Run Sleep Actions** and states that it does not suspend Windows.
  - **Devices** — the `DeviceListWidget` (`device_list`) with `show_actions=False`; the page provides the `Discover` / `Add Manually` buttons, which call the widget's own `on_discover()` / `on_add_manually()`. All device logic, dialogs, matching and identity behaviour are unchanged.
  - **Integrations** — one `IntegrationCard` per `INTEGRATION_KEYS` entry; `integration_widgets[key]` is still the `(enable check box, path field, Browse button)` tuple, and OpenRGB's card hosts the elevation presentation (`lbl_openrgb_elevation`, `btn_openrgb_elevation`, `lbl_openrgb_elevation_hint`).
  - **Automation** — `txt_lat` / `txt_lon` / `txt_elev` / `txt_buf`, `chk_close_apps` / `chk_turn_off_yeelight` / `chk_restore_apps`, the Razer options (`chk_launch_synapse`, `chk_wait_synapse`, `txt_synapse_timeout`, plus a note that reflects the Razer enable flag) and the **Compatibility** card with `chk_turn_on_yeelight`.
  - **Logs** — `log_display` and its Clear button.
- `_populate_settings_widgets()` / `_collect_settings_config()` keep their names and semantics (unknown keys are still preserved), and `init_ui()` calls the populate + elevation-refresh pair that the old Settings tab used to call.

The presentation layer itself lives outside this module: `ui_theme.py` (palette, typography, spacing, application/dialog/wizard stylesheets, semantic tones) and `ui_components.py` (the small reusable widgets). Neither contains configuration, power, process or network logic.

Closing the window normally minimizes to the tray rather than terminating the process.

### `SolarEngineThread`

Runs continuously in the background and reloads configuration from disk through `ConfigManager`. It uses `ephem` and the configured latitude/longitude/elevation plus `light_buffer_hours` to derive day/night state.

Important current behavior:

- Recalculates approximately every 60 seconds.
- Emits the UI solar state.
- Publishes state to Artemis **only while the Artemis integration is enabled**.
- `YeelightPCCompanionWindow.on_solar_update()` calls reconciliation.
- Reconciliation only runs while the Yeelight Chroma Connector integration is enabled (that integration's status drives self-healing).
- At night, reconciliation can trigger a restore/self-heal when the Yeelight Connector is missing.
- Nighttime self-heal is throttled to at most once every 15 minutes.
- During daytime, reconciliation kills the connector and turns the enabled Yeelight devices off if the connector is unexpectedly running.

### `RestoreEngineThread`

Performs the wake/startup restoration sequence without blocking the Qt UI thread.

Current broad sequence (each step is additionally gated by the integration enable flags from §8; with everything enabled the order and waits are exactly as before):

1. Wait ~5 seconds for Windows/network initialization.
2. Kill stale Yeelight Connector, Artemis, and OpenRGB processes (enabled ones only).
3. Wait ~3 seconds so stale Yeelight music-mode connections can close.
4. Start OpenRGB with `--gui --startminimized --server` (or reuse an already running instance), then **wait until OpenRGB confirms that its controller detection is complete** (its SDK `DETECTION_COMPLETE` event) — see the readiness gate below. The launch mechanism is elevated-aware and never raises a UAC prompt — see §4b.
5. Evaluate solar state.
6. If dark: turn on the configured Yeelight devices and start the Yeelight Chroma Connector with its own executable's folder as the working directory and a bundle-free DLL search environment (§3).
7. If daytime: ensure connector is stopped and Yeelight devices are off.
8. Either launch Razer Synapse or wait/poll for an externally started Synapse instance according to configuration.
9. Start Artemis minimized; retry once if it does not stay running.

**Artemis depends on step 4, nothing else.** Artemis is started only after the OpenRGB readiness gate has finished, and that gate depends on OpenRGB alone — not on Yeelight discovery, the Connector, Razer Synapse or the solar state. Razer handling still sits between the two, as before.

The sequence contains intentionally conservative waits because the relevant third-party applications and network/device stacks need time to become usable after wake. A blank/unconfigured device address is skipped without opening a socket, and an enabled-but-missing executable is skipped with a warning so the remaining steps still run.

### OpenRGB detection-complete readiness gate (`wait_for_openrgb_ready`)

Starting OpenRGB is not the same as OpenRGB being usable: it binds its SDK server **31 ms** into startup, then spends almost all of the rest of it detecting controllers (measured on the maintainer's PC: first controller at 1.1 s, the rest between 11.7 s and 11.9 s, detection complete at 12.3 s — see below). A consumer that connects during that window sees an empty or incomplete device list and only recovers by being restarted. The restore sequence therefore used to sleep a fixed 8 s after requesting the launch, and then briefly (2026-09-18, during the OpenRGB→Artemis readiness race work) waited for a controller count that stayed identical for three consecutive probes. **The second rule was a false positive too**: the count sits unchanged at *one* for 10.5 of the measured 12.3 seconds, so "the count did not move" declared readiness while three of the four controllers did not exist yet. It was reproduced against that earlier readiness rule — ready at 4.0 s, roughly eight seconds early. See §17.

**The readiness signal is OpenRGB's own detection event stream** (SDK protocol 6). Verified in the source of the installed build — the `release_1.0` tag, commit `81bbe18a`, which is the commit the installed `OpenRGB.exe` reports in its own log:

| Source location | What it establishes |
| --- | --- |
| `NetworkProtocol.h:28`, `:125-127` | `OPENRGB_SDK_PROTOCOL_VERSION` is `6`; `NET_PACKET_ID_DETECTION_STARTED` (`101`), `..._PROGRESS_CHANGED` (`102`) and `..._COMPLETE` (`103`) were introduced with it |
| `NetworkServer.cpp:873` | `SignalDetectionCompleted()` calls `SendRequest_DetectionCompleted()` for **every** entry of `ServerClients` |
| `NetworkServer.cpp:3945` | …which sends `103` only to clients whose `client_protocol_version >= 6`. **No client flag and no subscription is required** (the ProfileManager, by contrast, needs `NET_CLIENT_FLAG_SUPPORTS_PROFILEMANAGER`) |
| `NetworkServer.cpp:1668`, `:3704` | `NET_PACKET_ID_REQUEST_PROTOCOL_VERSION` (`40`) stores `min(requested, server max)` on the connection and answers with the *server's* maximum |
| `DetectionManager.cpp:867` | detection's last act is `SignalUpdate(DETECTIONMANAGER_UPDATE_REASON_DETECTION_COMPLETE)` |
| `NetworkServer.cpp:3586` | the controller-count reply is a `4s` count, plus **one `4s` id per controller when protocol ≥ 6** |
| `Documentation/OpenRGBSDK.md` | "103 … Indicate to clients that detection completed." — and the packet table contains **no** request that queries the current detection state |

Two consequences shape the gate: (1) a client that is connected when detection completes receives `103` however early during detection it connected, and (2) protocol 6 cannot be *asked* for the current detection state, so a client that connects after `103` was sent never learns on that connection that detection finished.

Three pieces, all standard library, all in `yeelight_pc_companion.py`:

| Piece | Role |
| --- | --- |
| `OpenRgbSdkConnection` + `open_openrgb_sdk_connection(timeout)` | A persistent, read-only SDK connection with the protocol **negotiated** (`protocol_version`) that records the controller count (`decode_openrgb_controller_count`) and the detection events the server pushes. Only requests `40` (once) and `0` are ever sent. |
| `wait_for_openrgb_ready(…, launched)` | The gate. See the two paths below. Returns `True`/`False`; never raises. |
| `RestoreEngineThread.wait_for_openrgb_ready(launched)` | Binds the gate to the thread: `cancelled=lambda: not self.running`, `publish=self.progress_update.emit`, and `QThread.msleep` for every wait. |

**The gate is told which situation it is in and treats them differently** — this is the caller's knowledge, not a guess:

| Situation | Acceptance |
| --- | --- |
| `launched=True` — this restore started that process | `NET_PACKET_ID_DETECTION_COMPLETE` (`103`) **only**. No count, and no amount of waiting without the event, is accepted. |
| `launched=False` — OpenRGB was already running | `103`, **or** a positive controller count that has been unchanged *and* free of any detection event for `OPENRGB_READINESS_ALREADY_RUNNING_SECONDS`. Any detection event voids the quiet window. |
| protocol < 6, or the server never answers the negotiation | nothing can confirm completion, so the budget runs out and the restore continues with the warning. |

The already-running window exists because that case cannot be distinguished from "detection is running silently" by the protocol: it is set **longer than the widest silence between two detection events** in the measured cold start (10.6 s, the HID-enumeration stage), so a count that stays put across it cannot be inside a detection stage. It is the one intentional latency knob of the design: ~13.5 s of gate on that path, against at most one poll plus the settle on the launched path.

**The connection is read-only and bounded.** It connects to `127.0.0.1:6742`, sends `NET_PACKET_ID_REQUEST_PROTOCOL_VERSION` (`40`) once with its own maximum and thereafter `NET_PACKET_ID_REQUEST_CONTROLLER_COUNT` (`0`) with an empty body and `pkt_dev_id = 0`. No RGB data, no rescan, no profile or configuration command, one OpenRGB process, no elevation, no second `OpenRGB.exe` — an ordinary unprivileged localhost SDK client like Artemis' plugin. Every read is bounded by a timeout, and a disconnect, a short read, a bad magic, an impossible `pkt_size` (limit `OPENRGB_SDK_MAX_PAYLOAD_SIZE`, faithful to OpenRGB's own `OPENRGB_SDK_MAX_PACKET_SIZE`) or a foreign `pkt_dev_id` ends the stream so the gate can reconnect. Packets the gate did not ask for (acknowledgements, device-list updates, controller updates) are consumed in full, so the stream cannot desynchronise. The count reply is parsed **according to the negotiated protocol** (`4` bytes below protocol 6, `4 + 4 × count` from protocol 6 up), and the device list is asked for again the moment `103` arrives, so the readiness line names the list that detection actually produced.

```text
request:   4s "ORGB" | 4s pkt_dev_id=0 | 4s pkt_id | 4s pkt_size | payload
reply:     4s "ORGB" | 4s pkt_dev_id   | 4s pkt_id | 4s pkt_size | payload
```

**The constants** (`OPENRGB_*` at the top of the module):

| Constant | Value | Meaning |
| --- | --- | --- |
| `OPENRGB_SDK_HOST` / `OPENRGB_SDK_PORT` | `127.0.0.1` / `6742` | Loopback and the OpenRGB SDK default port |
| `OPENRGB_SDK_CLIENT_PROTOCOL_VERSION` | `6` | The highest SDK protocol this client speaks |
| `OPENRGB_SDK_DETECTION_PROTOCOL_VERSION` | `6` | The lowest one that has the detection events |
| `OPENRGB_SDK_MAX_PAYLOAD_SIZE` | `8 MiB` | Faithful to OpenRGB's own `OPENRGB_SDK_MAX_PACKET_SIZE` |
| `OPENRGB_SDK_PROBE_TIMEOUT_SECONDS` | `0.6` | Connect and read timeout of one exchange |
| `OPENRGB_SDK_NEGOTIATION_TIMEOUT_SECONDS` | `1.0` | How long a version reply may take before the server counts as protocol 0 |
| `OPENRGB_READINESS_TIMEOUT_SECONDS` | `25.0` | Budget for the whole gate (~2× the measured detection) |
| `OPENRGB_READINESS_POLL_SECONDS` | `0.5` | Count-poll interval and read slice |
| `OPENRGB_READINESS_SETTLE_SECONDS` | `1.5` | Settling delay after readiness is confirmed |
| `OPENRGB_READINESS_ALREADY_RUNNING_SECONDS` | `12.0` | The quiet window of the already-running path |
| `OPENRGB_READINESS_PROGRESS_LOG_SECONDS` | `2.0` | Progress-logging heartbeat |

**It never stalls the restore.** On timeout the sequence logs `WARNING: OpenRGB detection readiness could not be confirmed within 25s. Continuing.` and carries on with the rest of the restore (Yeelight, Razer, Artemis). The gate's `False` return is *not* the cancellation signal: only `self.running` decides that, so a readiness timeout continues while a suspend aborts the sequence on the same path as every other step. A suspend during the wait stops it at the next poll (≤ 0.5 s plus the read timeout), never after the full budget.

**Already-running OpenRGB is gated too.** When `OpenRGB.exe` is already present the sequence skips the launch but still opens the gate with `launched=False`, because "the process exists" says nothing about whether its detection finished. When OpenRGB is disabled, has no usable executable, or has no elevation task (skipped for this restore), the gate is never opened — those paths behave exactly as they did before.

**Measured timelines (maintainer's PC, OpenRGB v1.0, 4 controllers).** From OpenRGB's own log (`%APPDATA%\OpenRGB\logs`), milliseconds after process start:

| Event | 2026-09-18 (first measurement) | 2026-09-19 cold start (acceptance run) |
| --- | --- | --- |
| SDK server accepts connections | 31 ms | 42 ms |
| start of device detection | 33 ms | 48 ms |
| controller #1 registered | 1 136 ms | 1 264 ms |
| controllers #2–#4 registered | 11 684 / 11 755 / 11 883 ms | 11 907 / 12 033 / 12 161 ms |
| detection completed | 12 267 ms | 12 659 ms |

The count therefore sits at **one for 10.5 s of a ~12.5 s startup** — which is exactly why the count-stability rule failed, why elapsed time is not a valid readiness signal, and what the `103` event removes.

**Log lines** (one per real state change — never one per poll): `OpenRGB launched. Waiting for OpenRGB to finish detecting controllers...`, `OpenRGB is detecting controllers...`, `OpenRGB detection progress: N% - <detector>`, `OpenRGB SDK detected N controller(s).`, `OpenRGB SDK controller count changed: A -> B.`, `OpenRGB detection completed (Xs).`, `OpenRGB SDK ready: N controller(s), detection completed (Xs).`, the `… unchanged for 12s …` variant of the readiness line for the already-running path, and the timeout warning above. The detection-progress line is **rate-limited** (a percentage change, or one line per `OPENRGB_READINESS_PROGRESS_LOG_SECONDS`): OpenRGB emits one progress event per detector it walks through, and before the throttle a single real cold start wrote **991** progress lines into the log.

### Native process handling

Process enumeration and termination primarily use the Windows Toolhelp32 API through `ctypes` rather than repeatedly spawning shell tools. `taskkill` exists as a fallback in some paths.

### Child-process working directories and DLL search hygiene

`RestoreEngineThread.launch_process(path, args, hidden, cwd=None)` is the single gate for starting **installed external programs**, and it does three things: passes `cwd` to `subprocess.Popen` (opt-in, so an integration that does not care keeps the inherited directory), clears this process's injected DLL search directory for the duration of the spawn, and hands the child a `PATH` without the frozen bundle. Subprocesses belonging to the application itself (`taskkill`, the elevated OpenRGB provisioning helper) do not go through it; the OpenRGB scheduled task is isolated by Task Scheduler.

**The frozen-build contamination.** A PyInstaller run does two things to the launcher that an external child inherits:

1. it becomes the process-wide DLL search directory (`SetDllDirectoryW`/`AddDllDirectory` from the runtime hooks), and
2. it adds its bundle directory to `PATH` — `_internal`, `_internal\PyQt6\Qt6\bin`, `_internal\pywin32_system32` — which the child inherits verbatim.

Both were measured on a live connector launched by the packaged app: it had the bundle on its `PATH` and loaded `dist\YeelightPCCompanion\_internal\VCRUNTIME140.dll` (and `VCRUNTIME140_1.dll`), keeping the build output locked so the next PyInstaller `COLLECT` failed with `PermissionError: [WinError 5]`.

**The fix** (in `yeelight_pc_companion.py`):

| Piece | Role |
| --- | --- |
| `external_process_environment(base=None)` | copy of the environment whose `PATH` drops the bundle directory and its descendants; `os.environ` is never mutated |
| `_is_inside(path, directory)` | path-component comparison (`abspath`/`normcase`/`commonpath`, separator-normalised), so `_internal_backup` survives and a differently-cased spelling still matches |
| `external_process_environment_scope()` | `SetDllDirectoryW(None)` for one spawn, previous value read with `GetDllDirectoryW()` and restored in `finally` |
| `_LAUNCH_LOCK` | the DLL directory is process-global and this application is multithreaded, so the reset/spawn/restore window is serialised |

`external_process_environment()` returns the environment unchanged when `sys._MEIPASS` is absent, so source runs are unaffected. Bundled DLLs stay where they are, `SetDllDirectoryW` is never left cleared, and nothing is copied into an integration's folder.

**Per-integration `cwd`.** It stays opt-in; only these two pass their executable's folder explicitly:

| Integration | Why |
| --- | --- |
| OpenRGB (direct launch) | the child is elevated; the scheduled task pins the same folder through `<WorkingDirectory>` (§4b) |
| Yeelight Chroma Connector | it resolves runtime files relative to the current directory; launched with the inherited directory it looked in `dist\YeelightPCCompanion\_internal` |

Razer Synapse and Artemis keep the inherited working directory; they are still covered by the DLL/PATH sanitation, which applies to every launch through the gate.

### `RestoreEngineThread.launch_openrgb()`

The single entry point for starting OpenRGB, and the only place the elevated-launch decision is made (see §4b). It never calls `ShellExecuteW(..., "runas", ...)`, and it never opens an interactive prompt.

## 4a. Does the main application need elevation? — NO (measured, Stage 7)

This was an open question before Stage 7 and is now settled by measurement on the
real machine. **The application does not need administrator rights for any normal
runtime feature, and it therefore runs unelevated.**

Measured integrity/elevation states (Windows 11-class machine, UAC enabled,
maintainer account is a *filtered* administrator: `BUILTIN\Administrators` is
present but marked "Group used for deny only", so a normal process is **medium**
integrity):

| Process | How it was started | Measured state |
| --- | --- | --- |
| Yeelight PC Companion | manually, from source | medium / unelevated (`is_process_elevated()` False) |
| Yeelight PC Companion | retired `YeelightPCCompanion` logon task (`/rl highest`) | would be elevated — this is exactly why the task was retired |
| OpenRGB | `schtasks /run /tn YeelightPCCompanion-OpenRGB` | **elevated** (`HighestAvailable`) |

What was checked for a normal runtime feature, and why each is fine unelevated:

| Feature | API | Elevation needed? |
| --- | --- | --- |
| Suspend/resume detection | `PowerRegisterSuspendResumeNotification` + the `WM_POWERBROADCAST` window filter | No — verified by an unelevated run logging `[POWER CALLBACK] Successfully registered` and `[INIT] WinPowerEventFilter installed` |
| Shutdown/logoff receiver | hidden Win32 window | No — verified unelevated |
| Process listing | `CreateToolhelp32Snapshot` | No (`AccessDenied`-safe by design, §19) |
| Stopping the Yeelight Chroma Connector / Artemis | `OpenProcess` + `TerminateProcess` (same integrity) | No |
| Stopping OpenRGB | see the important finding below | **Yes — solved via Task Scheduler, not privilege** |
| Task Scheduler query | `schtasks /query` | No |
| Starting OpenRGB through the task | `schtasks /run` on an existing highest-privilege task | No (no UAC prompt) |
| Tray icon, UI, LAN, config | — | No |

### The important finding: an unelevated app cannot `TerminateProcess` an elevated OpenRGB

`_execute_suspend_actions()` stops controllers with the native
`terminate_processes_win32()` helper (`OpenProcess(PROCESS_TERMINATE)` +
`TerminateProcess`). That cannot be relied on against the elevated OpenRGB the
elevation task launches: the two processes sit at different integrity levels.

The **task-aware** replacement is `windows_tasks.end_openrgb_task()`, which runs
`schtasks /end /tn YeelightPCCompanion-OpenRGB`. Verified on the real machine from
an **unelevated** Python process against a genuinely elevated OpenRGB:

| Check | Result |
| --- | --- |
| Command | `schtasks /end /tn YeelightPCCompanion-OpenRGB` |
| Context | unelevated (medium integrity) |
| Exit code | **0** (`SUCCESS: The scheduled task ... has been terminated successfully.`) |
| Did the OpenRGB PID actually exit? | **Yes** — the process was gone afterwards |
| Task state afterwards | `Ready` (correct: no triggers, so it is idle again, not "disabled") |
| Latency | **85–108 ms** idle and while terminating, so it fits the ~1.5 s suspend budget |

The suspend path therefore calls `end_openrgb_task()` **before** the generic
terminate, and keeps the generic terminate afterwards because it still handles a
directly-launched, same-integrity OpenRGB. It then verifies with
`openrgb_process_running()` and logs a warning naming the privilege boundary if an
OpenRGB somehow survives — the check reports, it never assumes.

#### The task-aware stop owns ONE 0.4 s global deadline (hardened)

The first version of `end_openrgb_task()` violated the suspend timing contract:
it ran `query_openrgb_task()` (up to `SCHTASKS_TIMEOUT_SECONDS = 30 s`) to decide
whether the task existed, then `schtasks /end` with its own 5 s
`END_TASK_TIMEOUT_SECONDS`, then polled for the process for another 2 s. Three
independent per-call guards multiplied into a multi-second worst case on a path
that owns less than half a second, and a measured 85–108 ms happy path does not
make a multi-second failure budget acceptable.

The stop is now built around **one small monotonic deadline** —
`windows_tasks.END_TASK_STOP_BUDGET_SECONDS = 0.4 s` — which covers the whole
operation: the `schtasks /end` invocation, its process-exit verification and every
wait in between.

| Property | Behaviour |
| --- | --- |
| Task-existence query | **None.** The suspend path never calls `query_openrgb_task()` and never runs `/query`. |
| Absent / idle task | Classified from the `/end` wording (`cannot find the file specified` → `absent`, `not running` → `not_running`), inside the same budget. The native terminate fallback still runs afterwards. |
| `schtasks` timeout | The *remaining* deadline, via `timeout=max(END_TASK_MIN_SCHTASKS_SECONDS, deadline - now)` — never a fixed constant. |
| Verification | `_openrgb_process_ids()` immediately, then at most every `END_TASK_VERIFY_INTERVAL_SECONDS` (0.05 s) while the deadline holds. `None` ("could not read") is never treated as "nothing is running". |
| Deadline expiry | Returns `END_TASK_FAILED` immediately; the suspend sequence continues. |
| Retries | None beyond the single global deadline. |
| UAC / filesystem / config / DNS | None on this path. |
| Interpolation | No — the budget is derived from the sequence, not the measured mean. |

**Where 0.4 s comes from.** It is derived against the *whole* suspend sequence
rather than against the stop alone. `SUSPEND_SEQUENCE_TARGET_SECONDS` is 1.5 s,
and `yeelight_pc_companion.suspend_budgeted_seconds()` sums the phases that carry
an explicit worst-case budget:

| Phase | Constant | Budget |
| --- | --- | --- |
| Connector release wait | `SUSPEND_CONNECTOR_RELEASE_SECONDS` | 0.35 s |
| Yeelight OFF fan-out (all devices together) | `SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS` | 0.35 s |
| **OpenRGB task-aware stop (complete)** | `END_TASK_STOP_BUDGET_SECONDS` | **0.40 s** |
| Native controller terminate | `SUSPEND_NATIVE_TERMINATE_SECONDS` | 0.35 s |
| **Total** | | **1.45 s** (0.05 s headroom under the 1.5 s target) |

The Connector release wait and the Yeelight network budget are unchanged. The
0.40 s is about 3× the measured 85–108 ms real result (**re-measured at
0.109–0.138 s including the verification** during the Stage 7 hardening run, with
the whole suspend sequence at 0.472–0.502 s), which is the guard margin;
a test asserts that the budgeted total stays strictly below the target, so a
future change that spends the whole 1.5 s on budgets fails rather than passes
silently.

The post-stop honesty logging is kept and fits inside the same contract: the
outcome is logged with the measured duration
(`[SUSPEND] OpenRGB elevation-task stop: <outcome> (0.09s)`), the deadline-expiry
warning names the budget it ran out of, and the caller still runs
`openrgb_process_running()` and logs the privilege-boundary warning if an OpenRGB
survives.

**"OpenRGB is stopped on sleep" was not regressed.** An earlier observation
(`is_process_elevated() == False`, OpenRGB launched via the task, yet OpenRGB
apparently terminated) was resolved by measuring the integrity level directly
instead of inferring it from a `Stop-Process` result: the elevated instance is
real, and Task Scheduler is the mechanism that stops it.

**Nothing about the privilege model was loosened to achieve this:** no UAC
disablement, no consent-policy change, no registry prompt suppression, no
auto-elevated-binary abuse, and no new `runas` call anywhere in the sleep path.

## 4c. OpenRGB Windows-service conflict (v1.0.1 bugfix)

### Real-world reproduction

OpenRGB 1.0 can install a Windows service:

| Item | Value |
| --- | --- |
| Service name | `OpenRGB` |
| Display name | `OpenRGB` |
| Description | OpenRGB SDK Server |
| Executable | `C:\Program Files\OpenRGB\OpenRGB.exe` |
| Startup type | `Automatic` |

Observed on real hardware: OpenRGB's own "Start at login" option is **off**,
OpenRGB is absent from Task Manager Startup Apps, and no shortcut exists in
`shell:startup`. Nevertheless, with YPC startup **disabled**, rebooting Windows
still produces an `OpenRGB.exe` process. Expanding that process in Task Manager
exposes the OpenRGB Windows service — the service is what starts OpenRGB at
boot.

Failure when the service is enabled:

1. Windows boots; the OpenRGB service starts automatically.
2. Some RGB hardware comes up incorrectly (motherboard/RAM can still be
   controlled by Artemis; fans / motherboard-connected ARGB strip can remain
   default rainbow).
3. YPC's normal restore sees the existing OpenRGB process. Stale-process
   cleanup cannot reliably terminate it (unelevated YPC vs. service-owned /
   privileged OpenRGB).
4. Run Sleep Actions removes YPC's task/tray OpenRGB, but `OpenRGB.exe` can
   remain visible because the service stays alive.
5. Killing the remaining OpenRGB process manually, then **Force System Sync**,
   launches one clean YPC-controlled OpenRGB and **all RGB works**.

**Confirmed manual fix:** stop the Windows service `OpenRGB`, set it from
Automatic to **Disabled**, reboot. The problem disappears.

**Important correction:** this is **not** primarily "YPC launches two OpenRGB
instances". `RestoreEngineThread` kills stale OpenRGB, then checks whether
`OpenRGB.exe` is still running before launching. A service-owned process can
survive the unelevated kill; the restore then sees OpenRGB as already running
and reuses it. The core incompatibility is:

    OpenRGB Windows service owns lifecycle/hardware detection
vs
    Yeelight PC Companion expects to own OpenRGB lifecycle through
    YeelightPCCompanion-OpenRGB.

### Architecture chosen

New module `openrgb_service.py` (standard library + ctypes only):

| Piece | Role |
| --- | --- |
| `OpenRgbServiceProbe` | exists / state / start_type / binary_path / error |
| `probe_openrgb_service()` | read-only SCM query, unelevated, handles always closed |
| `extract_service_binary_path()` / `service_binary_matches()` | ImagePath parsing and executable-identity comparison |
| `evaluate_openrgb_service_status()` | the pure conflict policy (UI + restore diagnostics) |
| `disable_openrgb_service()` | the **only** mutation: stop + set startup Disabled + verify |

`windows_tasks.py` gained one more narrow elevated CLI mode
`--disable-openrgb-service <expected-openrgb-path>` (first argument only, no
service-name parameter) and `request_elevated_disable_openrgb_service()`, which
reuses the existing ShellExecuteEx + process-handle + result-channel model.

UI: a separate **Windows service** row on the OpenRGB Integrations card
(`with_service_status=True`), distinct from the elevated-launch row.

Restore: on the already-running path only, a read-only diagnostic explains that
an externally managed OpenRGB is being reused. The protocol-6 readiness gate is
unchanged.

### Security / elevation boundary

* The service name is a hardcoded constant `OpenRGB`. No `--service-name`, no
  generic command runner.
* The expected OpenRGB path is used **only** to refuse a mismatched identity; it
  is never executed.
* Binary-identity mismatch or unverifiable identity → refuse mutation, no UAC
  fix offered.
* Read-only inspection works unelevated with `SC_MANAGER_CONNECT` +
  `SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS`.
* Mutation is reachable **only** from the explicit Integrations-page action
  (`repair_openrgb_service_conflict`). Startup, sleep, wake, status polling and
  solar reconciliation never call it.
* Success is reported **only** when startup == Disabled **and** state ==
  Stopped. Partial repair is a failure with its own diagnostic.
* The suspend hot path and `END_TASK_STOP_BUDGET_SECONDS = 0.4 s` are unchanged.
  The service-stop wait is a separate, longer, user-action budget.

### Tests added

`tests/test_openrgb_service.py` (mocked SCM, no real service, no elevation):
read-only probe (absent/stopped/running/Automatic/Manual/Disabled/query error/
handle closing), binary identity (quotes, case, spaces, arguments, mismatch,
malformed, mismatch never reaches mutation), conflict policy (running⇒conflict
even when Manual; Automatic⇒conflict when stopped; stopped+Manual is not an
active conflict; stopped+Disabled is no conflict; unknown is never "safe"),
privileged CLI (fixed command, missing/extra args, no service-name, no generic
command interface, absent no-op, stop+disable, mismatch refuses, stop failure
and disabled-but-running are not success, re-query/verify), elevation boundary
(only explicit UI action; not startup/restore/suspend/status/solar), UI surface
wiring, restore diagnostics (no second launch, service-conflict warning,
readiness gate unchanged), suspend contract (budget unchanged, no service
control in `_execute_suspend_actions`).

Plus 9 window-level UI tests in `tests/test_ui.py`
(`TestOpenRgbServiceConflictUi`) for pill/button state, confirmation, declined
UAC and successful repair reaching `trigger_resume()`.

**Existing test change (explained):** `test_openrgb_readiness.RestoreSequenceHarness`
now implements `_report_openrgb_service_owned_instance`, because the real
`RestoreEngineThread.run()` gained that read-only diagnostic on the
already-running path. Without the stub, `run()` raised `AttributeError` and
aborted before the readiness gate. `test_the_connector_dies_before_the_fan_out_and_the_controllers_after_it`
now stubs `end_openrgb_task`: on a machine where `OpenRGB.exe` is actually
running, the task-aware stop's verification polls and those sleeps are recorded
by the shared `time.sleep` patch. That behaviour is covered by the dedicated
budget/deadline tests; the ordering assertion is about connector/fan-out/controllers.

### Not done on purpose (this patch)

* No automatic service restore/re-enable on disable/exit/sleep/uninstall.
* No config-schema bump and no persistent machine-state for the original start
  type. A possible follow-up is to remember the original start type and offer an
  explicit "Restore OpenRGB service" action.
* No version bump to 1.0.1 yet (`app_metadata.py` stays at 1.0.0 under
  [Unreleased]).

## 4b. OpenRGB elevation model — zero-UAC wake (critical design)

OpenRGB needs administrator rights on some systems (RAM RGB and other kernel-driver devices). Launching it with `ShellExecuteW(..., "runas", ...)` works but shows a UAC prompt, and nobody is in front of the PC when it resumes. That is unacceptable for automatic sleep/wake restoration.

**The model:** a dedicated, user-approved, on-demand Windows scheduled task is used as the elevation broker.

| Item | Value |
| --- | --- |
| Task name | `YeelightPCCompanion-OpenRGB` (hardcoded, separate from the `YeelightPCCompanion` logon task) |
| Run level | `HighestAvailable` |
| Logon type | `InteractiveToken` (the current user's context; no stored password) |
| Action | the configured OpenRGB executable with the fixed arguments `--gui --startminimized --server` and an explicit working directory of `dirname(OpenRGB.exe)` |
| Triggers | **none** — never on a schedule, never at logon; it only runs when the app asks for it |

`windows_tasks.py` owns all of this. Nothing disables UAC, changes consent policy, edits the registry to suppress prompts, abuses auto-elevated binaries or uses any other bypass. The only privilege escalation is a normal, user-approved, one-time task definition.

### Launch decision (wake path)

```text
Need to launch OpenRGB
        |
        v
Is the main process elevated?
        |
        +-- YES --> launch OpenRGB directly as a child process
        |           (fixed arguments, SW_HIDE, cwd = dirname(OpenRGB.exe)) - no UAC,
        |           no Task Scheduler
        |
        +-- NO --> is YeelightPCCompanion-OpenRGB present and matching?
                    |
                    +-- YES --> run it (silent; starting an existing
                    |           highest-privilege task raises no UAC prompt)
                    |
                    +-- NO  --> log a warning, emit restore progress text,
                                SKIP OpenRGB for this restore and continue
                                with the remaining steps
```

The skip path is deliberate: a missing or stale task must never abort Yeelight, Razer or Artemis handling, and must never fall back to `"runas"`. The same skip happens when the configured OpenRGB executable sits in a location a privileged task may not use (below).

### When the task is created, updated and removed

- **First-run wizard**, after the configuration has been saved: if OpenRGB is enabled with a path, the user is asked once for the administrator approval and the task is created. A declined or failed approval only produces a warning ("OpenRGB was configured, but seamless elevated launch could not be enabled. You can repair this later from Settings.") — the saved configuration is never discarded.
- **Settings save**, and only when it is relevant: enabling OpenRGB or changing its OpenRGB executable path requests a create/update; disabling the integration requests removal. Unrelated saves never re-prompt (the action is computed from the *previous* saved state, see `windows_tasks.openrgb_task_action()`).
- **Disabling OpenRGB** attempts removal. If removal fails (for example because Windows denies it), the integration still disables and a warning is logged — disabling must never be blocked by cleanup.
- **Removal/creation never touches any other task.** Only the exact name `YeelightPCCompanion-OpenRGB` is queried, created or deleted.

### Deleting the task requires elevation (measured, Stage 7)

Creating the task needs administrator rights (§"One-time administrator
approval"), and so does **deleting** it. Measured on the real machine:

| Attempt | Result |
| --- | --- |
| `schtasks /delete /tn YeelightPCCompanion-OpenRGB /f` unelevated | `ERROR: Access is denied.` (exit 1); the task survives |
| the same deletion, elevated | exit 0; the task is genuinely gone |
| `schtasks /create /tn <anything> ...` unelevated | fails — an ordinary user cannot create a scheduled task at all |

The task was created by the *elevated* provisioning helper, so it carries a
security descriptor that does not let the unelevated user delete it again. This
has two consequences and both are handled explicitly rather than assumed:

- **In-app disable path (pre-existing, unchanged):** disabling the OpenRGB
  integration attempts removal, and if Windows refuses, the integration still
  disables and a warning is logged — disabling is never blocked by cleanup
  (§4b). The orphaned task is inert (it has no triggers) and is *skipped* by the
  wake path while the integration is disabled, so it is harmless rather than
  dangerous. It is removed by the uninstaller or by an explicit elevated delete.
- **Uninstaller (Stage 7):** the Inno Setup uninstaller tries the deletion
  unelevated, **verifies with a fresh `schtasks /query` rather than trusting the
  exit code**, and if the task is still present asks Windows for a single,
  narrowly scoped elevation that runs exactly that one `schtasks /delete`
  command. A *silent* uninstall never raises an invisible consent prompt — it
  reports the task as left in place instead. Success requires **both** the
  `Exec`/`ShellExec` launch to succeed **and** `ResultCode = 0`, and is then
  confirmed by re-querying. Failing to remove the task is reported, never fatal.

### Existing users / migration

An existing installation already has OpenRGB enabled with a configured path but no dedicated task. Startup **never** repairs this on its own and never raises a prompt from the tray:

- the app detects that provisioning is required,
- the Settings tab shows `Seamless elevated launch: Needs setup` (or `Needs repair` / `Task points to a different OpenRGB path`),
- the log records a warning naming only the task state,
- a **Set Up Seamless OpenRGB Launch** / **Repair OpenRGB Elevation** button performs the one-time approval on explicit request.

### Task validation

A task is only reported `Ready` when its definition actually matches the saved configuration **and** the executable it points at is one a privileged task may use:

- `RunLevel` is `HighestAvailable`, it is enabled, its `Command` equals the configured OpenRGB executable (case- and quote-insensitive) and its `Arguments` are exactly `--gui --startminimized --server`,
- its `WorkingDirectory` equals `dirname(configured executable)` — a missing or different working directory is `Needs repair`, because Windows otherwise chooses the directory the elevated OpenRGB would start in,
- a `LogonType`, when Task Scheduler states one, is `InteractiveToken`,
- a `UserId`, when Task Scheduler states one, is the current user (SID or `DOMAIN\user`),
- it has **no triggers** at all,
- the configured OpenRGB executable is provably protected from the current user (next section).

Task Scheduler omits fields that equal its own defaults (for example `<Enabled>`, `LogonType`, `UserId`), so an omitted `LogonType`/`UserId` is read as "not stated" instead of as a mismatch, while an omitted `<WorkingDirectory>` is a mismatch because there is no safe default for it. A task that exists but is disabled, stale, differently configured, differently owned or triggered is reported as needing repair instead of being trusted.

### Privileged-target security (where OpenRGB may live)

An approved highest-privilege task is a standing promise to run one fixed path with administrator rights. If the unelevated user can replace that file, the promise becomes a persistent escalation path: the replacement is launched elevated on the next wake. `windows_tasks.is_elevation_target_secure()` therefore has to prove, **before** the task is created, that the current user cannot replace or modify the executable.

- **Effective-rights check, not a path test.** The object's DACL is evaluated for the user's own *unelevated* access token with `AccessCheck()`, after `GetNamedSecurityInfoW()`. A `C:\Program Files` prefix test would be both too narrow (a copy can live anywhere) and meaningless (the protection comes from the ACL), and `os.access()` is not usable because on Windows it ignores token elevation, group membership, deny ACEs and ownership. Inside the elevated provisioning helper the check evaluates the *linked* (limited) token, because an elevated token may write into protected locations that the user's normal processes cannot.
- **Checked targets:** the executable itself, the folder that contains it (OpenRGB loads its own runtime files from there), and every folder above it. Dangerous rights are write data/add file, append, write attributes/extended attributes, delete, delete child, write DAC and write owner; a parent folder above the containing one is only checked for the rights that can reach the executable through it (delete, delete child, write DAC, write owner), because creating something further up cannot replace anything further down — replacing a folder in the chain requires deleting or renaming it first, and that needs delete/delete-child, which *are* checked. Create rights are deliberately excluded from the ancestor mask: some Windows volume roots grant users exactly that right for their own folders, so treating it as dangerous would reject protected installations without gaining anything.
- **Re-checked on every use:** the inspection runs before the task is created *and* on every status query, including the wake path, so a target whose permissions change later stops being reported as usable instead of being trusted forever.
- **Ownership matters:** an owner can always rewrite permissions, so a target owned by the current user counts as replaceable even when its DACL looks read-only.
- **Fail closed.** A missing token, an unreadable owner or a failed access check is reported as "could not be verified", and provisioning refuses to create the task for any target that is not provably protected. The app never moves files, takes ownership, edits ACLs or changes folder permissions — the user decides where OpenRGB lives.
- **Reporting:** an unsafe or unverified location is never `Ready`. Settings shows `Unsafe OpenRGB location` / `OpenRGB location could not be verified` with the reason "the executable can be modified by your normal user account — install or move OpenRGB to a protected location such as Program Files, then update the path in Settings". The wizard and the Settings repair button show that text instead of asking for an administrator approval that would be refused, and the wake path skips OpenRGB (with a warning log and progress text) while Yeelight, Razer and Artemis continue normally.
- **Configuration is untouched by a refusal:** a rejection never modifies `config.json`, never removes the integration and never loses the configured path; only the seamless elevated launch stays unconfigured until the user moves OpenRGB. OpenRGB can still be started manually.
- An executable that is not present yet is reported separately (`missing`): there is nothing to replace, Task Scheduler fails such a task immediately, the wake path skips it because the file is absent, and provisioning still refuses it through `validate_openrgb_executable()`.

### Task definition and quoting

The task is created from a full Task Scheduler XML definition (`schtasks /create /xml`), not from a `/tr` command string. That is what makes "no triggers" expressible at all — the plain `/sc` forms always attach a schedule or a logon trigger. `<Command>`, `<Arguments>` and `<WorkingDirectory>` are separate XML elements, so a path containing spaces (or non-ASCII characters) can never be split into the wrong executable/argument boundary, and the elevated process cannot inherit an arbitrary working directory. Windows otherwise defaults the working directory of a task action, so it is always written explicitly as `dirname(OpenRGB.exe)`.

`schtasks.exe` is always invoked with an **argument list** (`shell=True` is never used) and is resolved from `%SystemRoot%\System32`, never through `PATH`.

### One-time administrator approval

Creating a `HighestAvailable` task needs administrator rights. When the app is not elevated, it re-invokes *itself* through a single `ShellExecuteExW(..., "runas", ...)` call with one of two hardcoded modes (§7), requesting `SEE_MASK_NOCLOSEPROCESS` so the parent receives the elevated helper's **process handle**. That is the only `"runas"` in the project, it lives in `windows_tasks._shell_execute_ex_runas()`, and it is only reachable from explicit user actions (wizard finish, Settings button, Settings save that changed OpenRGB). Declining the prompt is reported, not raised.

The protected-location requirement above is verified **before** that approval is requested (in the unelevated process) and again inside the elevated helper, so an unsafe target never produces a UAC prompt and never creates a task. This ordering is the reason a refusal needs no cleanup: nothing was changed yet.

#### Parent/child provisioning handshake

The parent launches the helper, **waits for it**, reads its exit code and only then inspects the resulting task. It never polls for the task to appear, because polling cannot tell "still working" from "already failed":

```text
launch elevated helper (ShellExecuteExW + SEE_MASK_NOCLOSEPROCESS)
        |
        +-- launch failed / no handle  -> report immediately, nothing was changed
        +-- ERROR_CANCELLED            -> report the declined approval
        |
        v
WaitForSingleObject(handle, 30 s)   <- the helper's own lifetime is the signal
        |
        +-- timeout -> give the helper a short grace period, then terminate it
        |              (it only ever creates/deletes one fixed task), close the
        |              handle, and report the timeout - never "Needs setup"
        |
        v
GetExitCodeProcess(handle) -> close the process handle (always, exactly once)
        |
        v
read the helper's diagnostic result file, then query the task state
        |
        v
ok if exit code 0 AND the task is Ready
```

The timeout exists only to keep a broken elevated process from hanging the Settings UI; around 30 s is the guard, not the expected duration. A helper that finishes the task but fails to report back is still accepted (the task inspection decides). Giving up on a helper never leaves a privileged process behind.

#### Helper exit codes and diagnostics

The child writes its own user-safe reason to one fixed file, `%LOCALAPPDATA%\Yeelight PC Companion\openrgb_provision_result.json`, containing exactly `success`, `exit_code` and one short `message`. The parent shows that message verbatim, so a real failure is never reduced back to `Needs setup`:

| Exit code | Meaning | Report |
| --- | --- | --- |
| 0 | success | `OpenRGB seamless elevated launch is ready.` (only if the task is also `Ready`) |
| 1 | other provisioning failure | the helper's own message, else `PROVISION_EXIT_MESSAGES[1]` |
| 2 | malformed command line | the helper rejected its own command line |
| 3 | protected-location check refused or could not verify | the refusal text naming the reason |
| 4 | `schtasks.exe` refused the task definition | the `schtasks` error text |
| 5 | task created but not usable in its resulting state | `Provisioning helper completed, but the task is not ready: <specific status>` |

Result-file rules (this is diagnostics only - it is never read to decide which privileged command to run, and it offers no command/task/path interface):

- the name and location are fixed by `windows_tasks.py`; the path comes from `%LOCALAPPDATA%`, never a hardcoded user name,
- no configuration values, coordinates, device addresses or secrets are ever written,
- a stale result is **deleted before every attempt** and additionally rejected by its modification time, so an older attempt's outcome can never be mistaken for this one's,
- the parent reads it once and then removes it,
- every write/read is best effort: an unusable location degrades the *message* to the exit-code wording and never changes behaviour or crashes either process,
- a missing result file with a non-zero exit code still produces an honest, specific failure.

Minimal provisioning diagnostics also go to `%LOCALAPPDATA%\Yeelight PC Companion\openrgb_provisioning.log` (bounded, best effort, written before the normal application logging exists). It records the action, the resulting exit code, the OpenRGB executable *basename* and task-state wording only - never configuration contents, coordinates, device addresses or unrelated local paths.

The provisioning mode can do exactly one thing — create/update or remove the fixed task. `/opt`-style generic interfaces (`--run-command`, `--task-name`, `--arguments`) deliberately do not exist; the task name and OpenRGB arguments are hardcoded and only the executable path varies, validated as an absolute path to an existing `.exe` with no control characters.

#### The linked-token branch (elevated helper only)

Inside the elevated helper, `_open_standard_user_token()` must produce the user's **unelevated** token, because the helper's own token is the wrong identity for the protected-location check. Windows exposes the limited token as `TokenLinkedToken` of an elevated token.

That token is an **identification-level** token, and `DuplicateToken()` refuses to *raise* a token's impersonation level — the call fails with `ERROR_BAD_IMPERSONATION_LEVEL` (1346). The helper therefore tries `SecurityImpersonation` first (the level an unelevated token already carries) and falls back to `SecurityIdentification`, which is the level the linked token actually has. The identity used for the access decision - SID and group membership - is identical, and the check only ever queries; it never acts as the user, so the lower level is not a weaker check. This exact failure mode made the helper refuse *every* target and exit with a failure while the unelevated check passed.

## 5. Power and shutdown detection — critical design

Power-event reliability is one of the most important parts of this project. Do not simplify this subsystem casually.

There are currently **three complementary Windows mechanisms**:

1. **Primary suspend/resume detector:** `Win32PowerCallback`
   - Uses `PowerRegisterSuspendResumeNotification` from `PowrProf.dll` with `DEVICE_NOTIFY_CALLBACK`.
   - Suspend work can execute directly from the OS callback thread.
   - Resume is emitted back into Qt through a signal.

2. **Secondary detector:** `WinPowerEventFilter`
   - Qt native event filter for Windows power broadcasts.
   - Kept as a persistent attribute on the main window to prevent garbage collection.

3. **Shutdown/logoff receiver:** `Win32ShutdownWindow`
   - Hidden Win32 HWND.
   - Handles `WM_QUERYENDSESSION` / `WM_ENDSESSION` because ordinary Qt power handling may not receive all shutdown/logoff paths.

A watchdog runs every ~5 minutes and logs whether the native event filter and direct power callback still appear healthy.

### Suspend timing contract

**This is a hard constraint.** The suspend action is intentionally built to finish extremely quickly before Windows freezes user-space work during sleep.

`_execute_suspend_actions()` currently aims for roughly **<1.5 seconds** and assumes Windows may give only about a **2-second practical window**. The phases that carry an explicit worst-case budget are summed by `suspend_budgeted_seconds()` and must stay *strictly below* `SUSPEND_SEQUENCE_TARGET_SECONDS` (1.5 s); see §4a for the table and for why the OpenRGB task-aware stop owns a 0.4 s global deadline rather than a 30 s query plus a 5 s `/end` plus a 2 s poll.

Current critical order:

1. Deduplicate repeated suspend events (8-second window).
2. Mark sleep transition active and cancel an in-progress restore thread.
3. Read the configuration through **`ConfigManager.load_runtime()`** (see below) and keep the in-memory configuration if that read fails.
4. Kill **Yeelight Chroma Connector first** so it releases Yeelight music mode.
5. Wait only ~0.35 s.
6. Send one fire-and-forget raw `set_power: off` TCP command to **every enabled configured device at once** through the dedicated batch sender `fire_and_forget_off_devices(enabled_device_ips(config))` (see §8c).
7. Kill Artemis and OpenRGB — OpenRGB first through its elevation task under one 0.4 s global deadline (`end_openrgb_task()`), then the native same-integrity terminate for every controller.

**The Yeelight fan-out has ONE global network budget, not one timeout per device.** `SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS` (**0.35 s**) bounds the *entire* networking phase — every connect and every send, for every device together. All enabled addresses are attempted concurrently: one non-blocking socket per device (no DNS; the address family comes from the validated IP literal), one `select()` fan-out, one deadline, and the sockets are closed as soon as each one is done (or when the deadline expires). `1` device and `20` unreachable devices therefore cost the *same* 0.35 s.

This bound is what the earlier per-device loop could not provide: `for ip in device_ips: fire_and_forget_off(ip)` blocked up to **0.25 s per device** inside `socket.create_connection()`, which was acceptable for exactly two fixed devices (`2 × 0.25 s`) but not for an arbitrary configured list — 8 unreachable devices ≈ 2 s and 20 ≈ 5 s, both far past the ~1.5 s target, before the Connector wait and the process termination are even considered. **Do not reintroduce a serial blocking connect per device.** `fire_and_forget_off(ip)` still exists as a single-address convenience wrapper, but it goes through the same bounded batch sender.

Devices that do not answer inside the budget are reported in a single summary log record and simply skipped: every device remains independently best-effort, one dead bulb cannot delay or break the others, and the batch never raises.

**Configuration rule for this path:** the suspend callback must use `ConfigManager.load_runtime()` — a JSON read plus migration/normalization only. It performs **no validation**, so it never probes integration executable paths (`os.path.exists()` / `os.path.isfile()`), never writes files, never creates backups and never runs import/export logic. `ConfigManager.load()` and `validate_config()` (both of which check the external environment) must never be called from a timing-critical path.

**Device rule for this path:** the only thing the suspend callback reads about Yeelight devices is `enabled_device_ips(config)` — a list of address strings taken straight out of the already-loaded configuration. It must stay that way: **no discovery, no `yeelight.Bulb` construction, no DNS probing, no filesystem checks, no validation and no UI work** may be added to it. The device list is an ordinary list, so the number of devices changes neither the shape of the work nor the time it takes — the whole fan-out shares one fixed budget.

**Do not add slow retries, long sleeps, API verification round trips, UI work, or other blocking operations to this suspend path without explicitly reconsidering this timing requirement.** A theoretically more reliable command that cannot finish before Windows suspends the process is worse than the current fast path.

Suspend handling is also called from more than one thread/context, so preserve thread safety and event deduplication.

## 6. Resume/startup behavior

`trigger_resume()`:

- Rejects duplicate resume triggers while a restore thread is already running.
- Cleans up a finished previous thread.
- Reloads configuration. A failed reload logs the problem and **keeps the last known-good configuration already in memory** instead of falling back to defaults, so a transient read/JSON/validation error cannot silently disable integrations or blank device addresses.
- Clears sleeping/transition flags.
- Honors `restore_apps_on_wake` as a gate for starting the restore sequence.
- Starts `RestoreEngineThread` asynchronously.

The app also performs a restore sequence on normal startup unless `--no-autorestore` is supplied or automation is paused.

## 7. Command-line flags

Current entry-point flags in `yeelight_pc_companion.py`:

- `--tray` — start without showing the main window. **Exception:** when no usable configuration exists, the first-run wizard is shown regardless, because a hidden wizard would leave the user unable to configure the app. Once setup succeeds, normal `--tray` behaviour resumes.
- `--no-automation` — pause automatic reconciliation/startup behavior.
- `--no-autorestore` — skip the automatic restore sequence at startup.
- `--provision-openrgb-task <path>` — **elevated provisioning mode only** (§4b). Creates or updates `YeelightPCCompanion-OpenRGB`. Runs before the Qt application, the tray and automation are created, and does nothing else. Exit codes: `0` success, `1` other failure, `2` malformed command line, `3` protected-location check refused or could not verify, `4` `schtasks.exe` refused the task definition, `5` task created but not usable. The same reason is written to the narrow result file for the parent to display.
- `--remove-openrgb-task` — the elevated counterpart that removes the same fixed task. Takes no arguments.

The two provisioning flags must be the first argument and are rejected in any other position; there is no generic command/argument interface (§4b).

These flags are honoured by checking for the literal strings in `sys.argv`, so they work in any position.

`run_yeelight_pc_companion.bat` launches `dist\YeelightPCCompanion\YeelightPCCompanion.exe` with `--tray` when it exists and otherwise launches the Python source using `pythonw.exe` with `--tray`.

## 8. Configuration

`config_manager.py` is the **single source of truth** for the configuration schema, canonical defaults, validation, versioning/migration, storage location, atomic writing, backups and import/export. Configuration JSON remains the persistence format, but it is an implementation detail: a normal user configures the application through the **first-run wizard** and the **Settings UI**, never by editing JSON by hand.

### Where configuration lives

| Run mode | `config.json`, debug log, `crash.log`, backups |
| --- | --- |
| Source / development run | the repository directory — `<source>\config.json` (developer friendly) |
| Packaged EXE, normal install | `%LOCALAPPDATA%\Yeelight PC Companion\` |
| Packaged EXE with `portable.flag` beside it | the application directory (**portable mode**) |

Rules:

- A packaged application never writes personal runtime state under Program Files or beside the EXE unless portable mode is explicitly enabled.
- The data directory is created automatically when required.
- The location comes from Windows/environment APIs (`%LOCALAPPDATA%`, with a `SHGetFolderPathW` fallback) — never from a hardcoded username.
- Portable mode is opt-in and never the default: an **empty** file named `portable.flag` next to the executable switches config/log storage to the application directory.
- `yeelight_pc_companion_debug.log` and `crash.log` always live in the same directory as the configuration.

### Configuration versioning and migration

`config_version` is stored at the top level; `config_manager.CONFIG_VERSION` is the current schema version (**2**).

- A configuration without `config_version` is treated as **legacy version 0**.
- `migrate_config()` upgrades **v0 → v1 → v2** in order: v0 adds `config_version` plus the `integrations` enable flags and fills in missing known keys; v1 converts the two fixed Yeelight address slots into the `lights.devices` list (§8c). Every step **preserves every unknown key** (nothing is silently destroyed).
- Integration enablement is *inferred* from the already-configured executable paths (non-empty path → enabled, blank/absent → disabled), so an existing working setup keeps behaving exactly as it did before.
- Loading migrates only **in memory** and reports the source version via `ConfigManager.last_load_migrated_from`; the caller persists the migration explicitly, with one backup. Nothing is rewritten behind the user's back.
- A configuration with a *newer* `config_version` than the build supports is refused with a clear message instead of being guessed at.
- Future steps (`v2 → v3`, …) slot into `migrate_config()` beside the documented `v0 → v1` and `v1 → v2` steps.

### Schema (version 2)

```json
{
  "config_version": 2,
  "location": {
    "latitude": "0.0000",
    "longitude": "0.0000",
    "elevation": 0.0,
    "light_buffer_hours": 2.0
  },
  "lights": {
    "devices": [
      { "id": "yeelight:0x00000000037073d2", "name": "Desk Lamp", "ip": "192.168.1.50", "enabled": true },
      { "id": "manual:9f2c...", "name": "Hallway", "ip": "192.168.1.60", "enabled": false }
    ]
  },
  "paths": {
    "openrgb": "",
    "yeelight_connector": "",
    "razer_synapse": "",
    "artemis": ""
  },
  "integrations": {
    "openrgb": { "enabled": false },
    "yeelight_connector": { "enabled": false },
    "razer_synapse": { "enabled": false },
    "artemis": { "enabled": false }
  },
  "automation": {
    "close_apps_on_sleep": true,
    "turn_off_yeelight_on_sleep": true,
    "restore_apps_on_wake": true,
    "turn_on_yeelight_on_wake_night": true,
    "force_silent_launch": true,
    "launch_razer_synapse": false,
    "wait_for_razer_synapse": true,
    "razer_synapse_timeout_seconds": 45.0
  }
}
```

Every other section/key name is unchanged for compatibility. `paths` is deliberately kept alongside `integrations` (enablement is separate from the executable path). `config.example.json` is the safe fresh template (`"devices": []`, no address, no id) and is kept byte-identical in structure to `ConfigManager` defaults — a unit test asserts that.

### 8c. Yeelight device model and LAN discovery

**Device entry.** A device is a small object and nothing more: `id` (stable
identity), `name` (friendly label), `ip` (LAN address literal) and `enabled`
(boolean). `model`/`firmware` are optional and written **only** when discovery
provided them — a discovery response is never dumped into the configuration
(no `power`, `bright`, `support`, port, …).

**Stable identity.** The display name is never the identity:

| Identity | Meaning |
| --- | --- |
| `yeelight:<device-id>` | the id the device itself reports over SSDP (for example `yeelight:0x00000000037073d2`) |
| `manual:<uuid4>` | an identity assigned by this application: a manually added device, or a device migrated from a legacy v1 address slot |

An id is unique in the configuration, stable across reloads, and unaffected by
renaming. Because the identity is a device fact, a device whose address changed
is recognized from its id instead of being offered as a second device. A
`manual:` device whose address is later found by discovery can adopt its real
Yeelight identity with one explicit action in the discovery dialog (the state is
reported as `Already added - its device ID can be linked`).

**Migration from v1.** `bulb_ip` → a device named *Yeelight Bulb*, `lightstrip_ip`
→ *Yeelight Lightstrip* (both enabled, both with a generated `manual:` id). A
blank slot produces nothing; the same address in both slots produces **one**
device. The legacy keys are removed, so after migration the device list is the
only place a Yeelight address lives — there is deliberately no second runtime
model. The migration runs through the canonical `migrate_config()` path and the
caller persists it with one `config.json.bak` backup (startup does this
automatically: `[CONFIG] Migrated configuration v1 -> v2`).

**Validation** (`validate_device_entry`, shared by the wizard, Settings, import
and load): every entry must be a JSON object with a valid id, a non-empty name
(≤ 80 characters, no control characters), a valid address (using the project's
`ipaddress`-based address validation, now required rather than optional) and a
boolean `enabled`; ids must be unique and **normalized addresses** must be unique.
Optional metadata, when present, must be text. A present-but-malformed
`lights`/`devices` value is rejected, never repaired — an invalid import still
changes nothing. A device list where nothing is enabled produces a warning, not
an error.

**Runtime projection.** The only device API the runtime uses is
`config_manager.enabled_device_ips(config)` — the addresses of the enabled
devices, in configuration order, deduplicated, read defensively (a malformed
section or entry yields an empty list instead of an exception). It does no
filesystem or network access, no validation and no discovery. `enabled_configured_devices()`,
`configured_devices()` and `device_count_summary()` are the non-timing-critical
companions used by the UI.

**Fault isolation.** Every multi-device operation (`RestoreEngineThread.power_devices()`,
`YeelightPCCompanionWindow.turn_off_devices_immediately()`, the suspend fan-out and
the raw TCP sender) addresses devices one at a time and logs a per-device
failure instead of aborting: device A being offline never prevents B/C/D from
being switched. Zero devices is a completely valid state — RGB integrations,
suspend, restore, the solar engine and self-healing all keep working with an
empty target list.

**Suspend fan-out** (`fire_and_forget_off_devices()` in `yeelight_pc_companion.py`)
is the one place where devices are *not* addressed one at a time, because the
suspend path is time-bounded: all enabled addresses are attempted concurrently
under one global budget (§5). It still keeps per-device fault isolation — each
socket has its own outcome, and one dead device neither delays nor breaks the
rest — but the batch as a whole is bounded by `SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS`
rather than by `N × timeout`.

**Discovery** (`yeelight_devices.py`) uses the supported discovery function of
the installed `yeelight` package (`yeelight.discover_bulbs()` — one SSDP
`M-SEARCH` for `wifi_bulb`); no second protocol stack exists. Properties:

- **User-triggered only.** It runs from the **Discover Devices** button in Settings or in the first-run wizard, never at startup, never on a timer, never repeatedly in the background and **never during suspend**.
- **Off the GUI thread.** `yeelight_device_ui.DeviceDiscoveryThread` (a `QThread`) performs the search; the UI only waits for its result through a nested event loop with a bounded guard and shows `Searching your local network for Yeelight devices...`.
- **Bounded and quick.** The search lasts `DISCOVERY_TIMEOUT_SECONDS` (5 s, clamped to 0.5–30 s by `clamped_discovery_timeout()`) and returns whatever answered. No follow-up RPC is made per device: only what the SSDP response already contains is used (device id, address, port, model, firmware, reported name, power state).
- **The guard is clamped too.** `discovery_guard_milliseconds()` derives the modal guard from the *same* bounded value the search itself uses plus `DISCOVERY_UI_GRACE_SECONDS` (5 s), so a caller passing `999` can never open a ~1004-second modal dialog over a search that stops after 30 s. An unusable timeout falls back to the default instead of raising.
- **A guard timeout returns immediately.** When the guard expires, the progress dialog is closed, a timeout `DiscoveryReport` (*"The network search did not finish in time."*) is returned, and **nothing waits for the worker again** — the old `finally` block called `worker.wait(timeout + grace)` on the GUI thread, which could freeze the UI for another full timeout/grace period. `QThread.terminate()` is never called and a still-running `QThread` is never destroyed: the abandoned worker is retained in `LIVE_DISCOVERY_WORKERS` until its own `finished` signal releases it (deletion is posted with `deleteLater()`), and its result handler is detached so a late result can never reach the UI (it cannot overwrite a later discovery). Only explicit user actions create a worker and a search always ends by itself, so this list stays tiny.
- **Zero results and network failures are normal outcomes**, not exceptions: `discover_devices()` never raises and returns a `DiscoveryReport(devices, error)`. The UI shows *"No Yeelight devices were found. Make sure LAN Control is enabled in the Yeelight app and the devices are on the same network."*, adding the network error detail when there is one. Windows Firewall is never changed.
- **Duplicate responses are normalized** (same device id, or the same address, collapses into one entry).

**Matching a result against the configuration** (`plan_discovery()`), in the
order of preference:

| State | Meaning | Selectable |
| --- | --- | --- |
| `new` | not configured yet | yes (checked by default) |
| `ip_changed` | same device id, different address — "IP changed: old -> new" | yes (updates the existing entry) |
| `id_available` | already configured by address, and the device reports a stable Yeelight id that the entry can adopt | yes |
| `already_added` | already configured (by id, or by address) | no |

Nothing is added automatically: the dialog lists the results, the user unticks
what should not be added, and each new device can be renamed before it is
stored. Applying a selection can never produce a duplicate address; that case is
refused with an explanation instead.

### Integration enable flags

`integrations.<key>.enabled` is a plain boolean per integration. When an integration is **disabled**, *only* that integration's work is skipped:

| Disabled | Never happens |
| --- | --- |
| `openrgb` | kill, launch, wait for OpenRGB; its absence is not an error |
| `yeelight_connector` | kill, launch, status-based self-healing |
| `razer_synapse` | launch, wait/poll for Synapse |
| `artemis` | kill, launch, retry, publishing the Artemis JSON-module payload |

Direct Yeelight LAN power control stays independent of the connector. For **enabled** integrations the operation order and timing are unchanged; the suspend path reads its booleans from a lightweight runtime read (`ConfigManager.load_runtime()`: JSON read + migration/normalization, **no validation and no executable probing**), so no validation, filesystem probing, retries or UI work was added to the <1.5 s suspend contract. See §5.

An enabled integration with a **blank** path is a validation *error* (fix the path or disable it). An enabled integration whose executable is missing from the machine is only a *warning*: it is skipped at runtime, and the rest of the restore sequence continues instead of aborting.

### Missing / optional Yeelight devices

Any number of devices is valid, **including none at all**: a user may configure
only an RGB-application automation and add Yeelight devices later (or never).
Every device is optional and individually disableable; with the device list empty
no socket is ever opened and no Yeelight command is ever sent. See §8c.

### Validation

`config_manager.validate_config()` is the one validator shared by the wizard, the Settings UI, import and load. It returns user-friendly `errors` (blocking) and `warnings` (informative) covering: JSON object structure, `config_version`, latitude/longitude ranges, elevation, light buffer hours, every Yeelight device entry (id, name, address, enabled flag, optional metadata, unique ids and addresses — see §8c), boolean automation settings, the Razer timeout range, path types, and the integration structures. It never rejects a configuration merely because a third-party application is temporarily uninstalled (disabling that integration is sufficient).

Two rules keep this validator meaningful:

- **Structural validation is separate from external environment checks.** Structure/version/ranges/types are checked from the JSON alone; whether an enabled integration's executable exists is an external check (`os.path.isfile()`). Only the strict path runs the external checks — timing-critical consumers use `ConfigManager.load_runtime()`, which skips validation entirely.
- **A present-but-malformed known section is rejected, never repaired.** `normalize_config()` (and therefore `migrate_config()`) only fills in *missing* sections/keys. A known section that is present but not a JSON object (`"location": "garbage"`, `"lights": []`, `"paths": "bad"`, `"automation": []`, `"integrations": "bad"`, or a `integrations.<key>` entry that is not an object) is preserved exactly as written so validation reports a clear error, and `lights.devices` must be a list (a malformed device entry is rejected too). Because of this, an invalid imported file can never be silently "fixed up" into a valid configuration that then replaces the active one. Unknown keys are still preserved in both cases.

### Executable availability

Executable paths must point at **files**. Validation warnings, `integration_path_available()` and the launch helpers use `os.path.isfile()`, never `os.path.exists()`, so an existing *directory* is not accepted as an executable: it is reported exactly like a missing executable (validation warning, runtime skip). Deliberately no PE-header, extension or signature check is performed — a normal existing file is enough.

### Safe writes and backups

`write_config_file()` writes a temporary file in the destination directory, flushes/fsyncs it, verifies it parses back, and only then `os.replace()`s it over the target — a failed or interrupted write can never truncate a valid configuration. Backups are taken **before** a destructive replacement (import, migration) as a single latest `config.json.bak`; the app deliberately does not accumulate timestamped backups.

### Existing-user migration

On a packaged first launch, when the new-location config does not exist yet, the app looks for a legacy configuration:

1. `config.json` beside the executable (old packaged builds stored it there);
2. the development layout — EXE at `<repo>\dist\YeelightPCCompanion\YeelightPCCompanion.exe` with the personal config at `<repo>\config.json`. This is only accepted when the directory is safely identifiable as the source checkout (it contains `yeelight_pc_companion.py` *and* `config.example.json`).

The legacy file is loaded, migrated, validated and written to the new location; the original is **never** modified or deleted. The log records that a migration happened using file names only — never configuration values. If no valid legacy configuration is found, the first-run wizard is shown instead.

### Import / Export

Both use native file dialogs and are available in the Settings tab (and import also from wizard page 1):

- **Import**: pick a JSON file → parse → detect version → migrate → validate → only then back up the current config and install atomically. An invalid file changes nothing. Nothing but JSON data is ever read.
- **Export**: writes the complete current configuration (including `config_version`) to a chosen file, after warning that it contains coordinates, local addresses and local executable paths. Export is local only — nothing is ever uploaded. The active config file itself cannot be chosen as the export target.

### Known configuration mismatches

Still open (carried over from the baseline import, deliberately **not** guessed at in this change):

- `turn_on_yeelight_on_wake_night` is defaulted, saved and exposed in the UI, but the restore logic still does not consult it: the night path always turns the devices on.
- `force_silent_launch` exists in defaults/example configuration but has no functional consumer.

Handling rules established by this change:

- The keys are **preserved** (defaults, migration, save/load and export keep them).
- They are not presented as reliably functional controls: the Settings checkbox is labelled as a compatibility setting that is not currently acted on, and both keys are surfaced in the wizard's automation note and in `config_manager.LEGACY_UNCONSUMED_AUTOMATION_KEYS`.
- Do not silently remove them, and do not invent semantics for them in an unrelated change — resolve them in a dedicated task.

## 8b. First-run setup wizard

`first_run_wizard.py` (PyQt6 `QWizard`, ClassicStyle so the dark palette applies to the page titles) is shown whenever no usable configuration exists — **including when the app is started with `--tray` from the logon task**, because a hidden wizard would leave the user unable to configure anything. It is a local variable, so it does not stay alive after setup. Since the Stage 5 redesign it is styled from the shared presentation layer (`ui_theme.APP_QSS` + `WIZARD_QSS` via `apply_top_level_theme()`), so it cannot drift away from the main window's look; the workflow, page order, validation, import shortcut, zero-device path and elevation step are unchanged.

Pages:

1. **Welcome** — what the app does, plus *Set up step by step* / *Import an existing configuration* (import is validated through `ConfigManager.load_external()`, which never writes; a successful import jumps straight to the review page).
2. **Location** — latitude (−90..90), longitude (−180..180), elevation (m), sunrise/sunset buffer (0..24 h). Coordinates are used locally only; nothing is transmitted.
3. **Yeelight devices** — an optional device section with **Discover Devices** (the same user-triggered LAN search as Settings) and **Add Manually** (name + address). Any number of devices is allowed, including none, and the page cannot block setup: a user who only wants the RGB-application automation finishes setup with an empty list. The review page reports the real count.
4. **Integrations** — enable checkbox, path field and native Browse dialog per integration; an enabled integration with a blank path is blocked, a missing file asks for confirmation.
5. **Automation** — the meaningful existing settings (close apps on sleep, lights off on sleep, restore on wake, Razer launch/wait/timeout) plus the honest compatibility note.
6. **Review and finish** — concise summary (location, number of devices, enabled applications, main automation state) and any warnings; **Finish** validates, saves atomically, creates the data directory and lets the normal startup continue. When OpenRGB is enabled it then asks once for the administrator approval described in §4b and provisions the elevation task; a declined or failed approval is reported as a warning and never discards the saved configuration.

Cancelling at any point writes nothing at all: no partial configuration, no placeholder automation.

## 9. Logging

The app creates a rotating `yeelight_pc_companion_debug.log` in the **configuration data directory** (§8):

- source run → the repository directory (unchanged behaviour for developers),
- packaged install → `%LOCALAPPDATA%\Yeelight PC Companion\`,
- portable mode → the application directory.

Properties: 2 MB per file, 3 backups plus the active file (roughly 8 MB total). If the data directory cannot be written, the app falls back to a log beside the application. The top-level exception handler writes `crash.log` to the same directory (also with a fallback), and it never terminates silently.

The **in-app** log view (the Logs page) is a separate sink from the file log and is now bounded: it keeps the newest `UI_LOG_MAX_BLOCKS` (3 000) lines, trimmed in batches of `UI_LOG_TRIM_BATCH` (500) so that trimming never costs a full document relayout per line. See §19 for the measurements and for why Qt's own `maximumBlockCount` was rejected. File logging is unaffected, "Clear" still clears only the view, and the newest lines are always the ones retained.

Privacy: logs must not be committed, and configuration values are not logged. This change deliberately logs **file names and storage modes only** — no coordinates, device addresses or full local paths (verified by scanning a real session log). Existing operational log lines that were already there are unchanged.

The top-level exception handler can also create `crash.log`. This is local/sensitive runtime output and is now explicitly ignored by `.gitignore` (`crash.log`, `crash.log.*`).

### Provisioning diagnostics (`openrgb_provisioning.log`, `openrgb_provision_result.json`)

The elevated OpenRGB provisioning helper runs **before** the normal application logging exists, and the parent cannot see the helper's console output. Two narrowly scoped diagnostics files cover that gap, both in the configuration data directory (§4b):

- `openrgb_provisioning.log` — bounded, best-effort. Records the action, the helper's exit code, the OpenRGB executable **basename** and task-state wording only. Never configuration contents, coordinates, device addresses or unrelated local paths.
- `openrgb_provision_result.json` — transient, holds exactly `success`, `exit_code` and one short `message`. Deleted before every attempt and after the parent reads it, and additionally rejected by modification time so a stale result is ignored.

Both are **diagnostics only**: neither is ever read to decide which privileged command to run, and neither offers a command/task/path interface. A failure to write or read either one degrades the *message* (to the exit-code wording) and never changes behaviour or crashes either process. Like the other runtime files, they are local runtime output and are not committed.

## 10. Build and startup files

### `yeelight_pc_companion.spec`

PyInstaller specification for the `YeelightPCCompanion` executable/folder. It references:

- `yeelight_pc_companion.py`
- `yeelight_pc_companion.ico`
- `version_info.txt`
- `config.example.json` as packaged data (the only configuration file ever bundled)

**Packaging privacy contract:** the spec does **not** reference `config.json` at all. A build no longer requires, reads, or ships the maintainer's personal configuration; `config.example.json` (placeholder-only) is bundled as a safe template.

### `build_yeelight_pc_companion.bat`

**Development build helper only.** Installs PyInstaller from `requirements-build.txt` if needed, regenerates `version_info.txt` from `app_metadata.py`, builds from `yeelight_pc_companion.spec`, and produces `dist\YeelightPCCompanion\YeelightPCCompanion.exe`. It copies **no** local `config.json`. It fails if `config.json` **or** `portable.flag` appears in the output, then runs `tools\release_privacy_scan.py` against the output.

It is **not** part of the user installation path and must never be presented to end users as a setup step (§10c).

### `run_yeelight_pc_companion.bat`

Starts the packaged `dist\YeelightPCCompanion\YeelightPCCompanion.exe` when present, otherwise the source via `pythonw.exe`, always with `--tray`. Kept as a source-developer convenience.

### `register_startup.bat`

**Legacy / development migration helper only.** It no longer registers a scheduled task and needs no administrator rights of its own. Running it tries to remove the retired elevated startup tasks (`YeelightPCCompanion`, and the older `LuminaLightOrchestrator`) **unelevated** and then writes the current per-user `HKCU\...\Run` entry. It never touches `YeelightPCCompanion-OpenRGB`.

It **does not claim** it can always do that: the old tasks were created by an
elevated installer and may carry a security descriptor that denies an ordinary
user the right to delete them. Each deletion is followed by a fresh `schtasks
/query` rather than trusting the exit code, and a task that survives is reported
as still present, with the explanation that it needs administrator rights and
that the installer (or Task Scheduler "Run as administrator") is the way to
remove it. While it remains, signing in may start an elevated duplicate. The
batch script checks each exit code with `if errorlevel 1` instead of relying on
`&&`/`||` after a redirected command.

Public users get start-at-logon — and the legacy-task migration — from the
installer, not from this script (§10c).

### Release-security status

The former release-security issue — the spec/build flow bundling the developer's real `config.json` — has been **fixed**. The current build flow cannot package the maintainer's private config, the friendly first-run configuration experience exists, so a packaged build is usable without any hand-written `config.json`, and since Stage 7 the artifact privacy policy is enforced automatically per artifact kind (§13).

## 10a. Elevation requirement

See §4a. **The main application runs unelevated; only OpenRGB is elevated, through
its own task.** Startup is therefore a per-user `Run` entry, and no UAC appears at
boot.

## 10b. Version source of truth

`app_metadata.py` holds `VERSION = (1, 0, 0)` and the product identity strings.
Everything else is derived:

| Artifact | How it gets the version |
| --- | --- |
| `version_info.txt` (EXE metadata) | generated by `tools/write_version_info.py`; **committed** so a plain build works, and `--check`ed by CI and the release build |
| `installer/version.iss` (Inno include) | generated by `tools/write_installer_version.py`; **gitignored**, regenerated at build time |
| Installer/setup filename | `build_release.ps1` reads `app_metadata.APP_VERSION` |
| Portable ZIP filename | same |
| Windows EXE file/product version | from the generated `version_info.txt` |

There is **no hard-coded fallback version anywhere**. The `.iss` script
`#include`s the generated include, so a missing include is a compile error rather
than a mislabelled installer.

## 10c. Installation model (Stage 7)

**Technology: Inno Setup 6** (`installer\YeelightPCCompanion.iss`). Chosen because
it is the conventional Windows installer for a small PyInstaller application and
needs no Python-side installer dependency. It is installed on the build machine
with `winget install JRSoftware.InnoSetup` and located by `build_release.ps1`.

**Scope: per-user by default**, into `%LOCALAPPDATA%\Programs\Yeelight PC
Companion` (`PrivilegesRequired=lowest`, `DefaultDirName={autopf}`). This needs no
installer UAC, matches the per-user configuration model in
`%LOCALAPPDATA%\Yeelight PC Companion`, and avoids shared-machine ambiguity. The
wizard still allows a machine-wide install (`PrivilegesRequiredOverridesAllowed=dialog`),
which is the only case that elevates. True `Program Files` installation was
rejected as the default because it would force UAC for an ordinary install while
buying nothing: the app writes no state beside its executable, and OpenRGB
provisioning already requests its own elevation independently.

What the installer does:

- installs the complete PyInstaller onedir payload plus `LICENSE.txt`,
- creates a Start Menu shortcut and an optional Desktop shortcut,
- offers (unchecked) a **start automatically when I sign in** task that writes a
  single `HKCU\...\Run` value (`uninsdeletevalue`, so uninstall removes exactly
  that one value and nothing else in the key),
- offers **Launch Yeelight PC Companion** when setup completes,
- **migrates the retired elevated startup tasks at install/upgrade time** (below),
- supports normal uninstall.

#### One-time install/upgrade migration of the retired elevated startup tasks

The retired design registered highest-privilege **logon** tasks
(`YeelightPCCompanion`, and before the rename `LuminaLightOrchestrator`). Leaving
one behind would start an elevated duplicate of an application that is now
deliberately unelevated. The normal installer therefore performs the migration
itself, from `CurStepChanged(ssPostInstall)` →
`MigrateLegacyStartupTasks()`:

| Step | Behaviour |
| --- | --- |
| Scope | Exactly the two fixed names, held in one place: `LegacyStartupTaskCount()` plus `LegacyStartupTaskName(Index)`. Inno Setup's PascalScript has **no array constants** (a `const` array fails to compile — verified with ISCC), so the fixed list is an accessor pair rather than an array; every loop iterates `0 .. LegacyStartupTaskCount() - 1` and can only obtain a name from the accessor. There is **no** task-name parameter and no command interface. |
| `YeelightPCCompanion-OpenRGB` | **Never touched** by this migration. It is a different task owned by the OpenRGB integration; its removal is the separate uninstall flow (§4b), and the migration code never references it or the OpenRGB helpers. |
| Detection | Silent `schtasks /query` per fixed name. No prompt, no message box. |
| Unelevated first | `TryDeleteLegacyTask(name, False)` — it costs nothing and covers a task the current user may delete. |
| Verification | A fresh `TaskExists(name)` query after the attempt. The exit code is never the last word. |
| Elevation | Only if a fixed legacy task *verifiably survives*, and **at most once per installation**: one `RunOneElevatedCleanup(...)` call starts a single elevated helper (`ShellExec('runas', {cmd}, ...)`) that receives the remaining fixed names as `schtasks /delete` *arguments*, so however many tasks remain the user sees exactly **one** consent prompt. The names never come from user input — there is no command string assembled from anything but the fixed accessor. If the installer is already elevated (`IsAdminInstallMode()`), it deletes directly instead of re-escalating. |
| Fresh install | No legacy task detected → the procedure returns **before** any elevation. A machine that never had them sees **no UAC**. |
| Silent install | `WizardSilent` short-circuits before the approval, so a silent install never raises an invisible consent prompt; it reports the task as left in place. |
| Declined / failed | Reported (the task is named as still present, and the log explains that it may start an elevated duplicate at sign-in) and **never fatal** — the procedure only logs, never raises, never aborts, never shows a dialog. |
| Report wording | `left in place`, `Installation is not affected`, and an explicit note that while the task exists it may start an elevated duplicate of this application at sign-in. |

`[UninstallRun]` keeps its two `schtasks /delete` entries as a second,
independent net for uninstall.

**Verified on the real machine (Stage 7 hardening).** The migration ran during a
reversible silent per-user test install on a machine that genuinely had the retired
`YeelightPCCompanion` logon task. Setup's own log recorded: the task detected, the
unelevated `schtasks /delete` returning exit 1 (access denied) with
`launched=1`, a fresh `TaskExists` proving it survived, `LuminaLightOrchestrator`
absent, and then the silent-install guard — no consent prompt, task left in place,
installation succeeded (exit 0). The maintainer's task was byte-identical
afterwards (same XML SHA-256), and `YeelightPCCompanion-OpenRGB` was untouched. A
fresh install with neither legacy task therefore reaches `Remaining = 0` and
returns before any elevation, i.e. shows no UAC at all.

The application itself never performs this migration: it cannot elevate, and a
tray prompt is unacceptable. `config_manager.remove_legacy_startup_tasks()` is the
deliberately **unelevated best-effort** helper (query → delete → report what was
actually removed) that the installer's first step mirrors; the app does not call
it on the startup path.

What uninstall does:

- removes the application files, the shortcuts and the `HKCU` startup value,
- removes the `YeelightPCCompanion-OpenRGB` task it owns, using the measured
  escalation described in §4b, reporting rather than failing if it cannot,
- **leaves the user's configuration in `%LOCALAPPDATA%\Yeelight PC Companion`
  untouched**, and never removes OpenRGB, Artemis, the Yeelight Chroma Connector,
  Razer or any of their files.

**Two artifacts, differing only by `portable.flag`:**

| Artifact | `portable.flag` | Configuration lives |
| --- | --- | --- |
| `YeelightPCCompanion-<v>-setup.exe` | **must be absent** | `%LOCALAPPDATA%\Yeelight PC Companion` |
| `Yeelight-PC-Companion-<v>-portable.zip` | **must be present** | beside the executable |

## 10d. Build and release tooling

- `app_metadata.py` — the single version/product identity source (§10b).
- `tools/write_version_info.py` — generates/verifies `version_info.txt`.
- `tools/write_installer_version.py` — generates/verifies `installer/version.iss`.
- `tools/release_privacy_scan.py` — the artifact privacy scanner (§13).
- `build_release.ps1` — the deterministic release pipeline: version metadata →
  unit tests → syntax/import checks → PyInstaller → privacy scan of the dist
  payload → portable payload + privacy scan → Inno Setup compile → verification of
  the packaged payload. It **fails on the first problem** and never needs a
  personal `config.json`. Verification prefers extracting the installer payload
  with 7-Zip and falls back to a **reversible per-user test install** in a
  throwaway directory (`/NOICONS`, no startup task, no shortcuts), scanning what
  was actually installed and then uninstalling it again.
- `.github/workflows/ci.yml` — Windows CI on push/PR: dependencies, `compileall`,
  import checks, version-metadata freshness, the unit suite, and the repository
  privacy scan. It touches no real hardware, LAN, Task Scheduler or UAC.
- `.github/workflows/release.yml` — tag-triggered (`v*`) or manual. Runs the same
  pipeline on a Windows runner and uploads the installer, the portable ZIP and
  `SHA256SUMS.txt` **as workflow-run artifacts**. It deliberately does **not**
  create a GitHub Release, so a mis-tagged build cannot become public by accident.
  The checksum file is produced there rather than by `build_release.ps1`, because
  its consumer is a *downloader* verifying a published artifact, and only CI
  produces those.

  **The `skip_tests` decision is made by the workflow engine, never by
  PowerShell.** `inputs.skip_tests` is a GitHub Actions *boolean*, so the old
  `if (${{ inputs.skip_tests == true }})` rendered as `if (True)`/`if (False)` —
  not PowerShell syntax, and a silent trap. Two plain steps replace it:

  | Step | Condition | Command |
  | --- | --- | --- |
  | normal build | `github.event_name != 'workflow_dispatch' \|\| !inputs.skip_tests` | `./build_release.ps1` |
  | packaging iteration | `github.event_name == 'workflow_dispatch' && inputs.skip_tests` | `./build_release.ps1 -SkipTests` |

  The two conditions are mutually exclusive and cover every trigger, so **a
  tag-triggered build always runs the tests** — `workflow_dispatch` is false for
  `push`, so the first step is the one that runs. No `run:` block contains a
  `${{ }}` interpolation at all. A test evaluates both conditions against every
  (event, skip) combination, validates the file with a real YAML parser, and fails
  if an interpolation reappears.

  **Optional private-value source.** Both build steps pass
  `YPC_PRIVATE_VALUES: ${{ secrets.YPC_PRIVATE_VALUES }}`. When the maintainer
  configures that repository secret (one exact private value per line) the build's
  privacy scan rejects the real coordinates / LAN addresses / author e-mail /
  checkout path / user name if one ever reaches an artifact. Unset — the default —
  the scan still runs its generic rules and the build is unaffected (§13a).

**Code signing is absent on purpose** (§14): the installer and EXE are unsigned, so
SmartScreen/reputation warnings are expected. No self-signed certificate is used
as if it provided public trust, and no certificate or key may enter the repository
(`.gitignore` blocks `*.pfx`, `*.p12`, `*.pvk`, `*.snk`, `*.cer`, `*.key`).

**There is no updater.** The versioning, the stable installer `AppId`, per-user
installation and `SHA256SUMS.txt` are the pieces a later opt-in updater would
build on; nothing checks the network today.

## 11. Dependencies

Runtime (`requirements.txt`, pinned to the versions the Stage 7 build was verified
against):

- `PyQt6==6.11.0`
- `yeelight==0.7.14`
- `ephem==4.1.5`
- `requests==2.31.0`

Build (`requirements-build.txt`):

- `pyinstaller==6.21.0` — **the exact version used by the verified onedir build**,
  pinned rather than floating. The earlier assumption of 6.16.0 was wrong; 6.21.0
  is what is actually installed and what produced the tested artifacts. Bump it
  only together with a fresh build and test run.

Licences of the direct dependencies (all compatible with the project's
`GPL-3.0-only`):

| Dependency | Licence |
| --- | --- |
| PyQt6 | **GPL-3.0-only** (Riverbank's open-source terms; this is why the project is GPL-3.0-only) |
| yeelight | BSD |
| ephem / PyEphem 4.1.5 | **MIT** (verified in the installed distribution metadata: `License: MIT` plus the `License :: OSI Approved :: MIT License` classifier, and the bundled `licenses/LICENSE` file) |
| requests | Apache-2.0 |
| PyInstaller (build only) | GPL-2.0-or-later **with a special exception** permitting distribution of bundled non-free programs |

`PyQt6-Qt6` (the Qt runtime wheel) is `LGPL v3`. The PyInstaller bundle also
carries transitive code (for example `cryptography`, pulled in by `requests`)
whose own licences ship inside `_internal/*.dist-info/licenses/`.

## 12. Repository file map

### Core

- `yeelight_pc_companion.py` — main application implementation; power handling, orchestration, tray, the window/page UI, settings/import/export, logging, and entry point. Configuration is accessed through `ConfigManager` (no direct JSON writes remain). Every Yeelight operation iterates the enabled device list from `config_manager.enabled_device_ips()`; the suspend path sends its OFF commands through the globally bounded batch sender `fire_and_forget_off_devices()` (§5, §8c). Since Stage 5 the window is built from the shared presentation layer below; only presentation moved out of this module.
- `ui_theme.py` — **presentation layer: visual system**: palette (graphite/navy surfaces, one muted indigo accent, semantic green/amber/red), typography (Segoe UI / Cascadia Mono, no bundled fonts), the 8/12/16/24 px spacing rhythm and radii, the application stylesheet (`APP_QSS`) plus the wizard (`WIZARD_QSS`) and dialog (`DIALOG_QSS`) variants, `dark_palette()`/`apply_app_theme()` (sets Fusion + palette so a dark theme needs no image assets and stays DPI-correct) and the semantic tone helpers (`pill_qss`, `text_qss`) that keep status text and colour together. No configuration, power, process or network logic.
- `ui_components.py` — **presentation layer: reusable widgets**: `SectionCard`, `StatusPill`, `StatusRow`, `SidebarButton`, `IntegrationCard`, the label helpers and the transparent `scrollable()`/`page_body()` page scaffold. Layout and rendering only; callers keep their own data flow.
- `config_manager.py` — **configuration layer**: canonical defaults/schema, `CONFIG_VERSION`, storage-location rules, validation, migration, atomic writes, backups, legacy discovery, import/export, and the runtime device projection (`configured_devices`, `enabled_configured_devices`, `enabled_device_ips`, `device_count_summary`). Standard library only, so it is testable without a GUI.
- `yeelight_devices.py` — **Yeelight device model and LAN discovery** (§8c): the stable identity scheme (`yeelight:<device-id>` / `manual:<uuid>`), device-entry construction, list operations (add/update/remove, duplicate id/address detection, summary, enablement), the bounded discovery call over the installed `yeelight` package's SSDP discovery, result normalization, and the matching of discovery results against the configured list (`plan_discovery` / `apply_discovery_plan`). Standard library only at import time (the `yeelight` package is imported inside the discovery function), so the model is testable without a network.
- `yeelight_device_ui.py` — **device management UI**: the discovery `QThread` worker, the progress + results dialogs, the add/edit dialog and the reusable device list widget shared by the Devices page and the wizard. Search filesystem/dialog-only — no configuration logic lives here. Since Stage 5 it uses the shared theme (`ui_theme`, exposed as `apply_device_dialog_style()`) instead of a second private dark palette, and its rows show only facts the configuration already holds (name, address, stored model) — no reachability or online/offline state is probed or invented.
- `windows_tasks.py` — **Windows scheduled-task elevation broker** (§4b): process-elevation detection, the fixed `YeelightPCCompanion-OpenRGB` task definition and XML (including its explicit working directory), `schtasks.exe` invocation, task lifecycle (`provision` / `remove` / `run` / inspect), the task-identity validation of a queried task, the protected-location (ACL/effective-rights) check for the executable a privileged task may launch, the narrow elevated provisioning modes, the parent/child provisioning handshake (`ShellExecuteExW` + `SEE_MASK_NOCLOSEPROCESS`, wait, exit code, result file), and the configuration→task lifecycle policy. Standard library only, so it is testable without a GUI or the real Task Scheduler.
- `first_run_wizard.py` — the PyQt6 first-run setup wizard (six pages) plus `run_first_run_wizard()`.
- `config.example.json` — safe fresh v2 configuration template (`lights.devices` empty); generated from (and asserted equal to) the `ConfigManager` defaults, and bundled by the spec. It is **documentation/template data only** — never required.
- `app_metadata.py` — **single source of truth** for the product name, publisher, licence id and `VERSION` (§10b). Standard library only, so build tooling can import it without the GUI.
- `requirements.txt` — pinned Python runtime dependencies.
- `requirements-build.txt` — pinned PyInstaller for producing the release artifacts.
- `build_release.ps1` — the deterministic release pipeline (§10d).
- `installer/YeelightPCCompanion.iss` — **Inno Setup 6 installer** (§10c): per-user install, Start Menu/Desktop shortcuts, optional `HKCU` start-at-logon, optional post-install launch, install/upgrade migration of the two retired elevated startup tasks (fixed scope, silent detection, unelevated first, one narrow elevation at most, verified, never fatal, never touching `YeelightPCCompanion-OpenRGB`), and uninstall that removes app files/shortcuts/startup entry and the owned OpenRGB task while leaving user configuration alone.
- `installer/version.iss` — **generated** Inno include carrying the version (gitignored).
- `LICENSE` — the GNU GPL v3 text; the project is `GPL-3.0-only` (§11).
- `README.md` — public-facing documentation: what the app is, requirements, LAN Control, installation, first run, integrations and the one-time OpenRGB approval, sleep/wake, discovery, privacy statement, configuration location, uninstall, troubleshooting, development/build, releasing, code signing, updates, licence.
- `tools/write_version_info.py` — generates and `--check`s `version_info.txt`.
- `tools/write_installer_version.py` — generates and `--check`s `installer/version.iss`.
- `tools/release_privacy_scan.py` — artifact privacy enforcement (§13a). Stores no
  private value and no fingerprint of one; exact values come from
  `--private-value` / `--private-values` / `$YPC_PRIVATE_VALUES_FILE` (default
  `private_values.local.txt`, gitignored) / `$YPC_PRIVATE_VALUES`, all optional.
- `private_values.local.txt` — **gitignored, never committed.** The maintainer's
  optional local source of the exact private values the scanner rejects. Does not
  exist in a fresh checkout, and everything works without it.
- `.github/workflows/ci.yml`, `.github/workflows/release.yml` — Windows CI and the build-only tag-triggered release workflow (§10d).

### Tests

- `tests/test_config_manager.py` — `unittest` suite for the pure configuration logic (defaults, migration, validation, `v0 → v1 → v2`, integration inference, save/load round trip, atomic-write/backup behaviour, import/export, storage-location rules, legacy discovery, the runtime loader boundary, malformed-section rejection, and file-vs-directory executable checks). No GUI and no Windows power behaviour is faked.
- `tests/test_device_discovery.py` — `unittest` suite for the device model (§8c): identity generation and stability, list operations (add/edit/remove, duplicate ids and addresses, summary), the v2 device schema and its validation (any device count, malformed entries, optional metadata, import/export, malformed-import-changes-nothing), the runtime projection, the whole `v1 → v2` migration matrix (no address, each slot, both, duplicate address, v0 all the way through, persistence + backup, runtime loader, import), discovery normalization (multiple devices, duplicates, missing fields, `Location` fallback, unusable responses), matching and applying (new / already added / IP changed / id adoption / selected names / duplicate-address refusal), every discovery failure path (zero results, network error, missing library, bounded timeout), the discovery worker thread, the **discovery guard** (a reported result / zero results / a network error close promptly, a guard timeout returns the timeout report without waiting for the worker again, no synchronous `wait()` and no `terminate()`, an abandoned worker stays alive and referenced until its own `finished` releases it, a late result cannot reach the UI or a later discovery, and the guard is derived from the same clamped timeout as the search), the wizard's device page, and the shipped example template. `yeelight.discover_bulbs` is mocked — the suite never broadcasts on the real network; the guard tests run against an offscreen `QApplication` with a fake progress dialog and a search the test controls.
- `tests/test_runtime_config.py` — `unittest` suite for the runtime configuration boundary of `yeelight_pc_companion` (the suspend callback performs no filesystem probing and never runs discovery, it iterates every enabled device, no devices still completes, a failed reload keeps the last known-good config, an executable path that is a directory is not usable, and the **suspend OFF fan-out** is bounded: one global budget for 1/2/20 unreachable devices, one `select()` wait, IPv4/IPv6 addressed by literal without DNS or `getaddrinfo`, no `yeelight.Bulb`, reachable devices still switch off when a peer is dead or refuses, zero devices returns immediately, every socket is closed on every path, disabled devices are excluded, the Connector still dies before the fan-out and OpenRGB/Artemis after it with the 0.35 s release wait kept). It drives the real window/thread methods through minimal stub objects: no window is created and no power event is triggered. The fan-out tests substitute a fake socket layer and a fake clock, so the timing assertions never sleep for real. `TestSuspendSequenceStaysInsideItsBudget` then pins the whole-sequence contract: the budgeted phases (`suspend_budgeted_seconds()`) must stay **strictly below** `SUSPEND_SEQUENCE_TARGET_SECONDS`, the task stop must be under a third of the target, and the **real** `_execute_suspend_actions()` with OpenRGB enabled runs against a fake clock and a faked clock-controlled task stop — proving the sequence's only waits are the Connector release plus the task stop's own verification interval, that every later step (controller terminate, completion log) is still reached, and that a hung `schtasks`, a missing task and an accepted-but-ineffective `/end` can no longer stretch the sequence. A regression test asserts the suspend callback performs **no filesystem probes and no task-existence query**. It is skipped only if `yeelight_pc_companion` cannot be imported at all.
- `tests/test_openrgb_readiness.py` — `unittest` suite for the OpenRGB **detection-complete** readiness gate (the restore wait): the SDK connection negotiates protocol 6 (and 0, 4 and a too-new 99 correctly), the controller-count request is byte-identical to the documented `NET_PACKET_ID_REQUEST_CONTROLLER_COUNT` packet with no body and no device id and is one of only two packets ever sent (the other being the version request), and the count reply is parsed *per negotiated protocol* (`4` bytes below protocol 6, `4 + 4 × count` from protocol 6 up — a body of the wrong shape is rejected). A disconnect, a short read, a truncated push, a wrong magic, an impossible payload size and a foreign `pkt_dev_id` end the stream instead of being trusted, an unasked-for 20 KB push is consumed in full without desynchronising the stream, and the socket is closed on every path. **The regression test is the point**: `TestTheMeasuredPlateauIsNeverReady` replays the real cold-start timeline (one controller from 1.1 s to 11.9 s, four at 11.9 s, detection complete at 12.3 s) through the real gate, the patchable `socket.create_connection` and a fake clock, and it fails against the pre-fix implementation with `readiness was declared at 4.0s, but detection only completed at 12.337s`. The gate paths are pinned separately: a launched instance is accepted only by `DETECTION_COMPLETE` (the started, progress and device-list events are reported and ignored, and a percentage of 100 never accepts), the device list is asked for again after completion and the readiness line names that reply, the settle delay precedes the readiness line, a protocol-4 server and a server that never completes time out non-fatally within the documented budget, a lost connection is reported and retried, an already-running instance is accepted by `DETECTION_COMPLETE` *or* by a positive count that stays unchanged and event-free for the quiet window (a detection event voids it, zero never counts, a changing count restarts it), a connection made after detection completed is accepted on the already-running path and refused on the launched one, and every path times out on a legacy server. Cancellation is covered at both levels (prompt, during the settle, and already-cancelled), the thread wrapper is asserted to use `msleep` rather than `time.sleep` (which is patched to raise) and to publish every state change, and a burst of progress events is asserted to be logged as a bounded heartbeat. Finally the real `RestoreEngineThread.run()` is exercised through a sequence stub: the gate runs exactly once between the OpenRGB launch and Artemis, the sequence tells it whether it launched OpenRGB or found it running, Artemis starts only after it succeeds (**never inside the plateau**), Artemis still starts after a readiness *timeout*, an already-running OpenRGB is gated as well, a disabled or skipped OpenRGB never opens the gate, the old fixed 8 s sleep is gone, one OpenRGB process and the `--gui --startminimized --server` arguments are untouched, and none of the gate's four functions contains an elevation, `runas`, `ShellExecute` or provisioning call. No real OpenRGB instance, socket, clock or sleep is used, and `socket.create_connection` is patched in every test that reaches the transport.
- `tests/test_windows_tasks.py` — `unittest` suite for the seamless OpenRGB elevation model (§4b): elevation detection, the wake-time launch decision (direct / scheduled task / skip), the working directory and DLL-search hygiene of every launch in the restore sequence (the connector pinned to its own folder, OpenRGB pinned to its own, Razer and Artemis keeping the inherited one, the frozen bundle removed from the child `PATH`, the DLL directory cleared for the spawn and restored afterwards - also when the spawn raises - and the lock-protected spawn window), the whole restore sequence continuing when the task is missing or the location is unsafe, which Yeelight devices a night/day restore addresses (only enabled ones, all of them, failures isolated, zero devices fine, never a discovery), the fixed task definition (exact path, fixed arguments, no triggers, highest privileges), paths containing spaces and non-ASCII characters, XML escaping surviving the round trip, the task-identity validation (`WorkingDirectory`, `LogonType`, principal, triggers), the protected-location check (effective rights, ownership, fail-closed paths, refusal before any approval), stale-task/repair reporting, the configuration-driven lifecycle (`openrgb_task_action`), the narrow provisioning command line, and the guarantee that the automatic restore path contains no interactive elevation call. `TestStartupImportCost` pins the startup-path property that `xml.sax.saxutils` (and with it `urllib.request`) is **not** imported by importing `windows_tasks` in a fresh interpreter and asserting neither module is in `sys.modules`, which is the reason `escape` is imported inside `build_openrgb_task_xml()` rather than at module level (§19). All Task Scheduler access is mocked — the suite never creates a real task, never elevates and never triggers a power event. The ACL checks that are exercised for real only read security descriptors and never modify them. Its `RestoreFlowStub` replaces the readiness gate with a recorded stand-in and asserts the sleeps around it, because the real gate would talk to a live SDK server.
- `tests/test_ui.py` — `unittest` suite for the **redesigned interface** (Stage 5, presentation only). It constructs the real widgets against an isolated temporary configuration, with the read-only elevation inspection faked (so no real Task Scheduler query happens), the process listing controlled, and `run_discovery()` replaced by a recorder or a hard failure so no test can ever broadcast on the LAN. It covers: window construction, the sidebar holding exactly the five pages, navigation selecting the matching page (and rejecting an unknown one), the action bar appearing only where configuration is edited, usability at the minimum window size, the Overview content (coordinates, device count, four service rows, solar updates, the manual actions reaching the real `trigger_resume` / suspend sequence with their process and network boundaries patched), `Running`/`Stopped` being carried by text with the tone only reinforcing it, the suspend status hook and the 8-second suspend deduplication, the device page using the existing device model (and inventing no online/offline state), Discover / Add Manually reaching the real discovery flow and device editor, all four integration cards, enable→path/Browse enablement, the native Browse dialog, the OpenRGB elevation states (ready, unsafe, and "unavailable is not fatal"), the automation fields, a semantic check of `_collect_settings_config()` (including unknown-key preservation) plus the save validation/atomic-write path, import refreshing every redesigned control (and changing nothing on failure), export keeping the privacy warning and the config format, the log page and its Clear button, the device editor and discovery-results dialogs, the wizard's six pages, the import shortcut, zero-device completion and the location validation, and three explicit guards that constructing the UI starts no discovery, requests no elevation/provisioning and triggers no restore/suspend work. `TestPresentationLayerIsPresentationOnly` additionally asserts that `ui_theme.py` and `ui_components.py` contain no `subprocess`/`socket`/`ctypes`/`requests`/`windows_tasks`/`config_manager` orchestration token and import nothing from the application.

  Stage 6 added three more classes: `TestStatusPollingCadence` (the tray-only cadence, the switch in both directions on show/hide, exactly one refresh when the window becomes visible and none when it already was, the fast cadence being a documented constant, and `check_system_statuses()` still reporting nothing but real process state), `TestUiLogIsBounded` (the bound and its batch being configured, the configured bound holding a real 3 540-line fill down, the newest lines being the ones kept, an effective trim running at most once per batch, Clear still working, and a real `RotatingFileHandler` still receiving every record while the view drops old ones) and `TestStatusPillUpdates` (repeating a state does not restyle, a changed state or tone still is applied).
- `tests/test_release_foundation.py` — **Stage 7 release foundation**: the version has one source of truth and both generated files agree with it (`--check` both succeeds on the current files *and* fails for a stale/missing one), no second version literal exists in the `.iss` or the spec, `portable.flag` is rejected in an installer payload **and** required in a portable payload (with the same directory proving clean as portable and dirty as installer), every forbidden filename variant is detected, binaries are never content-scanned; the private-value layer is a **run-time exact-value** check fed from outside tracked source — the scanner is asserted to store no value, no fingerprint and no length (and to make no one-way/collision-free claim), a build with nothing configured passes, values load from the environment, from a gitignored file, from `$YPC_PRIVATE_VALUES_FILE` and from a JSON array, a missing file is not fatal, unreadable/binary files are skipped, a synthetic fake value is detected and reported without ever printing it, `find_private_values` returns indices, values below the minimum length are ignored even when passed straight in, the default values file is proven gitignored and outside tracked source, and the generic substrings the policy refuses match nothing; the installer configuration is per-user, creates the shortcuts/startup/launch entries, contains no private path, checks **both** the launch and `ResultCode`, has exactly two `ShellExec('runas')` points (one per flow) and no `${{ }}` interpolation, tries unelevated first, never prompts during a silent uninstall and verifies the task is actually gone; the **install-time legacy migration** is driven from `CurStepChanged(ssPostInstall)`, is scoped to exactly the two fixed names in one `array[0..1]`, never references the OpenRGB task inside its own code, deletes only via names read from that array, verifies with fresh queries instead of exit codes, returns before any elevation on a fresh install, cannot prompt during a silent install, never raises/aborts/dialogs on a declined cleanup, and leaves the `[UninstallRun]` entries in place; a fresh installed-style run selects `%LOCALAPPDATA%` while `portable.flag` selects the application directory; the shipped template equals the application defaults and carries placeholder coordinates only; the build script runs tests before building, runs both privacy scans and verifies artifacts; and the CI/release workflows are Windows-based, pin actions to major versions, and the release workflow never publishes a GitHub Release. `TestReleaseWorkflowBooleanHandling` additionally evaluates the release workflow's two build conditions against every (event, `skip_tests`) combination, proves a tag push always runs the tests, parses the file with a real YAML parser, and fails if any `run:` block regains a `${{ }}` interpolation.
- `tests/test_startup_and_uninstall.py` — **per-user startup and narrow task removal** (§10c, §4b): the logon entry lives only in `HKCU\...\Run`, quotes a path containing spaces exactly once, starts with `--tray`, is idempotent, updates a moved installation, and **never touches another program's value** on enable *or* disable (an in-memory `winreg` fake, so the real registry is never read or written); `remove_legacy_startup_tasks` only ever queries/deletes the two fixed legacy names and never the OpenRGB task; the task-removal CLI accepts no arguments, rejects extras, exposes no generic `--task-name`/`--run-command` interface, and `remove_openrgb_task` takes no task-name parameter; and the task-aware stop is pinned to **one global deadline**: it has four distinct outcomes, targets only the fixed task, has no task-name parameter and no caller-supplied timing, is bounded by `END_TASK_STOP_BUDGET_SECONDS`, never queries the task first and contains no second/third timeout constant. `TestTaskAwareStopGlobalDeadline` runs the real `end_openrgb_task()` against a **fake monotonic clock and a fake `schtasks`** (`os.name` patched to `nt`, `_openrgb_process_ids` and `_run_schtasks` replaced, so nothing real is touched and the machine never sleeps) and proves that a missing task, a hung `schtasks.exe`, an accepted `/end` whose process never disappears and an unreadable process list are all globally bounded, that normal success works, that a success exit code never bypasses the verification, and that the only timeout handed to `schtasks` is the remaining deadline.
- **Integration coverage note:** the `HKCU` Run key and the scheduled task are only ever touched through injected fakes. A real install/uninstall is exercised manually (§17) and must never be run by the suite.
- Run with `python -m unittest discover -s tests -t .` from the repository root (**712 tests**: 231 + 94 + 83 + 78 + 74 + 69 + 44 + 36, i.e. `test_windows_tasks` 231, `test_release_foundation` 94, `test_device_discovery` 83, `test_openrgb_readiness` 78, `test_ui` 74, `test_config_manager` 69, `test_startup_and_uninstall` 44, `test_runtime_config` 36).

### Windows packaging / launch

- `yeelight_pc_companion.spec` — PyInstaller spec for the renamed app; bundles only `config.example.json` and never the real `config.json`, and excludes the unused `tkinter`/Tcl-Tk runtime (§10). The Windows version metadata comes from the generated `version_info.txt`, so the version has one source (§10b).
- `build_yeelight_pc_companion.bat` — **development** PyInstaller build helper; regenerates `version_info.txt`, builds `dist\YeelightPCCompanion\YeelightPCCompanion.exe`, copies no personal config, and fails if `config.json` **or** `portable.flag` appears in the output before running the privacy scanner.
- `build_release.ps1` — the deterministic release pipeline (§10d).
- `run_yeelight_pc_companion.bat` — source-developer convenience: starts the packaged `YeelightPCCompanion.exe` if present, otherwise the source via `pythonw.exe`; uses `--tray`.
- `register_startup.bat` — **legacy/development migration helper only**; needs no administrator rights, removes the retired elevated logon tasks and writes the current per-user `HKCU` Run entry. It never touches `YeelightPCCompanion-OpenRGB`.
- `installer/YeelightPCCompanion.iss` — the Inno Setup 6 installer (§10c).
- `tools/release_privacy_scan.py` — artifact privacy enforcement (§13a).
- `version_info.txt` — Windows executable metadata; product/description `Yeelight PC Companion`, internal/original names `YeelightPCCompanion`, company `Yeelight PC Companion Contributors`, version `1.0.0`.
- `yeelight_pc_companion.ico` — binary icon asset (renamed from `lumina.ico`; artwork not redesigned).

### Repository/process documentation

- `project_memory.md` — **this file**; canonical coding-agent context. Update it when assumptions or architecture materially change.
- `changelog.md` — human-readable running change history. Add noteworthy changes as work progresses.
- `.gitignore` — prevents local config, logs, build output, caches, and agent/editor noise from being committed.

## 13. Sensitive/private data rules

Before committing or publishing anything:

- Never add real `config.json`.
- Never add runtime logs or crash logs.
- Never add `build/` or `dist/` output unless there is a deliberate release-artifact workflow separate from source history.
- Never replace placeholders in `config.example.json` with the maintainer's actual IPs, coordinates, or paths.
- Never commit an exported Task Scheduler XML for `YeelightPCCompanion-OpenRGB`: it contains a personal executable path (`schtasks /query /tn YeelightPCCompanion-OpenRGB /xml > file` is a debugging aid, not a repository artifact). The application writes its XML to a temporary file and deletes it immediately.
- Treat any LAN address, geographic coordinate, username-containing Windows path, email address, or machine-specific path found in local-only files as potentially private.
- New runtime artifacts follow the same rule and are ignored by `.gitignore`: `config.json.bak` (an exact copy of a private config), the atomic-write temp files `.config-*.tmp`, exported configurations (`yeelight-pc-companion-config.json`), and the machine-local `portable.flag`.
- **UI screenshots are temporary verification output, not repository artifacts.** The visual checks in §17 render real widgets to PNG files in a temporary directory; they contain the maintainer's device addresses, coordinates and local paths, so they must be inspected in place and never committed.
- Inspect changes before commit, especially generated files.

**Test sandbox rule (learned the hard way):** automated tests must never touch the real user profile. Any test that exercises a frozen/packaged code path must patch `LOCALAPPDATA` (or inject an explicit config path); a test that forgets this writes a real `config.json` into `%LOCALAPPDATA%\Yeelight PC Companion\`.

The initial repository import was specifically sanitized so the private config and logs did not enter Git history. Preserve that property.

### 13a. Artifact privacy policy (Stage 7) — enforced, not aspirational

`tools/release_privacy_scan.py` is run by CI, by the release build, and by the
development build script. Its policy is deliberately **artifact-specific**, in
three independent layers:

1. **Forbidden filenames** (exact): `config.json`, `config.json.bak` and its
   numbered variants, `yeelight_pc_companion_debug.log(.N)`, `lumina_debug.log(.N)`,
   `crash.log(.N)`, `openrgb_provision_result.json`, `openrgb_provisioning.log`,
   the atomic-write temp files `.config-*.tmp`, and any `__pycache__` subtree.
   These are user-machine files that must never ship.
2. **`portable.flag`, in both directions.** It is **FORBIDDEN** in a normal
   dist/installer payload, where its presence would silently relocate the user's
   configuration into the application folder. It is **REQUIRED** in the portable
   payload, where its absence would silently send a portable user's data to
   `%LOCALAPPDATA%`. Each direction fails the build for its own artifact.
3. **Exact private values, supplied from OUTSIDE tracked source.** The scanner
   stores **no** maintainer value and **no fingerprint of one**. It checks the
   exact literals it is handed at run time, in text files only (never in
   third-party binaries):

   | Source | How |
   | --- | --- |
   | command line | `--private-value VALUE` (repeatable), for a one-off check |
   | local file | `--private-values FILE`, or `$YPC_PRIVATE_VALUES_FILE`, else `private_values.local.txt` in the repository root when it exists |
   | environment | `$YPC_PRIVATE_VALUES`, one value per line — how CI receives repository secrets |

   `private_values.local.txt` is **gitignored** (`.gitignore`). The file format is
   one value per line with `#` comments and blank lines ignored, an optional UTF-8
   BOM tolerated, and a JSON array of strings also accepted. Values shorter than
   `MIN_PRIVATE_VALUE_LENGTH` (6) are dropped, in `normalize_private_values()` *and*
   again in `find_private_values()`, so no caller can turn the layer into the
   generic substring rule the policy refuses.

   **Why fingerprints were removed.** The first version of this layer recorded
   each value as `sha256(value)[:8]` plus its exact length. That is not a privacy
   mechanism: 8 hex characters are a **32-bit truncated digest**, and a low-entropy
   value such as a private IPv4 address or a short user name is brute-forceable
   even with a full unsalted hash — the recorded length makes the search cheaper
   still. The fingerprints and lengths were therefore deleted from tracked source,
   together with the tests that were derived from the real values. See §13b.

   Properties this layer must keep:
   - **Builds work with no source configured** — the layer simply has nothing to
     match, which is the normal state for a contributor, for CI without the
     secret, and for a public release run. The scanner prints which state it is in.
   - **Maintainer/local builds can supply the exact values**, so the check is as
     strong as before; only the *storage* moved out of tracked source.
   - **The values never enter tracked files**, and neither do tests derived from
     them: the suite uses synthetic fake secrets only.
   - **A matched value is never printed.** A violation names the file and the
     number of matched values; `find_private_values()` returns 1-based *indices*.

**Explicitly rejected rules.** Scanning bundled dependency binaries for generic
substrings such as `"44.8"`, `"20.5"` or `"@gmail.com"` was considered and
**refused**: two-decimal numbers and a mail domain collide with ordinary content
in third-party DLLs, wheels and translation data, and a check that cries wolf is
worse than no check. The scanner tests assert that no such generic rule exists (and
that a short literal cannot be smuggled in), so a later contributor cannot quietly
reintroduce one.

The scan runs against the **tracked files** for the repository (`--repo`, so build
output and local config are excluded by construction) and against the **built
artifact directory** for payloads. It never scans arbitrary user files outside the
repo or the build output.

### 13b. Public-history baseline

Before the first public release, the private development history was
intentionally replaced with a clean public baseline to remove historical
maintainer-specific fixture data and author metadata. The repository now has a
single, parentless root commit, and no commit identifier from the earlier private
history is referenced anywhere in the tracked tree.

The tracked tree is clean: every confirmed private value was checked with
`git grep` against the working tree and is **absent**. The scanner compares
**exact values supplied from outside tracked source** and stores neither a value
nor a digest of one (§13a). The lesson that produced that policy is part of the
design of the scanner rather than of the project's history:

- A digest is not a privacy mechanism. A truncated unsalted digest of a
  low-entropy value such as a private IPv4 address or a short user name is
  brute-forceable, so the scanner stores neither a value nor a fingerprint of one.
- Values are described by **category, never by content** — and never by a digest
  either. Writing real coordinates, LAN addresses or an author address into a
  tracked file would be the exact leak the policy exists to prevent.
- The values live only in a private-value source outside tracked Git (a
  gitignored local file and/or `$YPC_PRIVATE_VALUES`), and the suite uses
  synthetic fake secrets only.

This brief note replaces the earlier historical audit. It is retained because the
**policy** it explains still governs `tools/release_privacy_scan.py` (§13a);
forensic detail about the replaced history is deliberately not part of the public
tree.

## 14. Naming migration (completed)

The `Lumina` → **Yeelight PC Companion** rename has been performed as one coherent migration. What changed:

| Area | Now |
| --- | --- |
| Python module/file | `yeelight_pc_companion.py` |
| Main window class | `YeelightPCCompanionWindow` |
| Main window title | `Yeelight PC Companion` (Stage 5: the long `- Intelligent Light Orchestrator` suffix is gone) |
| UI header | the current page title (the old `YEELIGHT PC COMPANION` banner was replaced by the sidebar wordmark) |
| Tray tooltip / exit item | `Yeelight PC Companion - Light Orchestrator` / `Exit Yeelight PC Companion` |
| Log/session messages | Rebranded (`YEELIGHT PC COMPANION STARTING`, `Shutting down Yeelight PC Companion...`, crash header, etc.) |
| `APP_USER_MODEL_ID` | `YeelightPCCompanion.App` |
| Hidden shutdown window | class `YeelightPCCompanionShutdownReceiverWindow`, title `Yeelight PC Companion Shutdown Receiver` |
| Generated debug log | `yeelight_pc_companion_debug.log` |
| PyInstaller spec / EXE / folder | `yeelight_pc_companion.spec`, `YeelightPCCompanion.exe`, `dist\YeelightPCCompanion` |
| Build / run scripts | `build_yeelight_pc_companion.bat`, `run_yeelight_pc_companion.bat` |
| Icon | `yeelight_pc_companion.ico` (renamed only; artwork unchanged) |
| `version_info.txt` | product `Yeelight PC Companion`, internal/original `YeelightPCCompanion`, company `Yeelight PC Companion Contributors` (no `Slon Inc`) |
| Scheduled task | `YeelightPCCompanion-OpenRGB` (OpenRGB elevation only). Startup is **no longer a task**: it is a per-user `HKCU\...\Run` value named `YeelightPCCompanion` (§10c). The retired logon task `YeelightPCCompanion` and the older `LuminaLightOrchestrator` are removed by the installer's install/upgrade migration, by `[UninstallRun]`, and (best effort, unelevated) by `register_startup.bat` (§10c). |

Deliberate remaining `Lumina` mentions are historical/migration-only: the changelog history, the historical note in §1, the legacy debug-log ignore rules in `.gitignore`, and the legacy-task removal logic in `register_startup.bat`. No active runtime or build path references the legacy names.

## 15. Open-source preparation status / known work

High-priority items before making the repository public or publishing binaries:

- ~~Complete the `Lumina` → `Yeelight PC Companion` branding migration.~~ **Done** — see §14.
- ~~Fix packaging so a maintainer's real `config.json` can never be bundled in a public build.~~ **Done** — see §10.
- ~~Add `crash.log` (and any renamed equivalent) to ignore rules.~~ **Done** — see §9.
- ~~Add safe first-run configuration handling instead of crashing when `config.json` is absent.~~ **Done** — ConfigManager + first-run wizard (§8, §8b).
- ~~Consider tests for pure/non-Windows logic~~ **Done** — `tests/test_config_manager.py` (§12).
- ~~Audit the UI and setup process so a user who does not share the original maintainer's exact OpenRGB/Razer/Artemis setup can understand what is optional/required.~~ **Done** — integrations have explicit enable flags, are individually optional, and disabled ones are skipped; the wizard, the Integrations page and now `README.md` tell the story publicly.
- ~~Add a proper `README.md` with screenshots/features/setup/use/troubleshooting.~~ **Done (Stage 7)** — `README.md` covers what the app is, features, Windows scope, requirements, installer and portable installation, first run, the Yeelight LAN Control requirement, integrations, the one-time OpenRGB UAC explanation, sleep/wake behaviour, discovery, the privacy/local-network statement, configuration location, uninstall behaviour, troubleshooting, development/build, releasing, code signing, updates and the licence. **Screenshots are still missing** and must be repo-safe (no private desktop content).
- ~~Add an explicit open-source `LICENSE` chosen by the maintainer.~~ **Done (Stage 7)** — `GPL-3.0-only`, consistent with PyQt6's own licence (§11). Per-file SPDX headers are deliberately **not** required.
- Resolve the two configuration mismatches (`turn_on_yeelight_on_wake_night`, `force_silent_launch`) in a dedicated task; they are preserved but not implemented.
- ~~**Known working-directory issue with the Yeelight Chroma Connector.**~~ **Fixed** — the connector is launched with its own executable's folder as the working directory (`cwd=os.path.dirname(connector_path)`) *and* every external launch now clears the frozen bundle from the child's DLL search environment (DLL-directory reset around the spawn, bundle-free `PATH`). The second half was required: a live connector still had `dist\...\_internal` on its `PATH` after the working directory alone was corrected. See §3.
- ~~Yeelight LAN discovery and arbitrary-length device lists (the schema currently has exactly two optional device slots).~~ **Done** — the fixed slots became the `lights.devices` list with stable ids, friendly names, discover/manual-add/edit/remove and per-device enablement, plus user-triggered LAN discovery. See §8c.
- ~~A full visual redesign of the dashboard/settings window (deliberately not part of the configuration task).~~ **Done (Stage 5)** — the tabs became a navigation sidebar with Overview / Devices / Integrations / Automation / Logs pages, the presentation layer was centralized in `ui_theme.py` + `ui_components.py`, and the device/discovery dialogs and the wizard now share that one theme. Presentation only: the automation engine, schema and tests were not changed. See §4 and §12.
- ~~An installer / update-checking work stream; the app currently runs from a folder.~~ **Installer done (Stage 7)** — Inno Setup per-user installer plus a portable ZIP, one version source, CI and a build-only tag-triggered release workflow (§10b–§10d). **Update checking stays deliberately absent**; the pieces a later opt-in updater would need are in place (§10d).
- Consider regenerating the icon artwork for the new brand (the rename only renamed the existing icon file).
- **Clean public-history baseline is in place (§13b).** The pre-release private development history was intentionally replaced with a single parentless root commit before publication, so no old development commit identifier is referenced by the tracked tree.
- **Code signing is absent.** SmartScreen/reputation warnings are expected on the first downloads; only a trusted Authenticode certificate changes that (§10d).

## 16. Coding-agent working rules

When starting a task:

1. Read this file.
2. Read `changelog.md`.
3. Inspect `git status` and the relevant source/build/config files before editing.
4. Preserve existing working behavior unless the task explicitly changes it.
5. Pay special attention to the suspend timing contract and multi-detector deduplication.
6. Never commit local secrets/config/logs/build output.
7. Prefer focused, understandable changes over unrelated cleanup.
8. Validate syntax/build/tests where practical. For Windows-specific behavior, state exactly what was and was not actually tested.
9. If behavior/config/architecture changed materially, update this file.
10. Add a concise entry to `changelog.md` for noteworthy changes.
11. Before finishing, inspect the diff for accidental personal data, stale `Lumina` references when performing naming work, and mismatched build paths.

## 17. Verification expectations

The pure configuration logic now has an automated test suite; everything else is still verified manually:

```powershell
python -m unittest discover -s tests -t .   # configuration logic, no GUI
```

For changes that can be checked locally, useful verification may include:

- Python syntax/compile checks.
- Import/startup checks in an appropriate Windows Python environment.
- The `unittest` suite above for configuration changes (defaults, migration, validation, atomic writes, import/export).
- PyInstaller build success when packaging files change, plus a check that `dist` contains no `config.json` and no private values.
- Manual PyQt UI launch for UI/settings changes (an isolated temp install directory with its own `config.json` — never the real one). Driving real widgets from a script with `WA_DontShowOnScreen` + `QWidget.grab()` renders and verifies a page without putting a window on the desktop.
- For the redesigned UI (Stage 5), the rendering check is `tests/test_ui.py` plus a temporary render script that builds the real window, the wizard and both device dialogs against an isolated config, switches through every page and saves `grab()` PNGs at 900 × 650, 1120 × 720 and 1280 × 800, at 100 % and 150 % (`QT_SCALE_FACTOR=1.5`). **Use the `windows` platform plugin with `WA_DontShowOnScreen` for this, not `QT_QPA_PLATFORM=offscreen`:** the offscreen plugin has no real font database (it substitutes a wide synthetic "Sans Serif", inflating every text measurement by ~1.6× and turning the check into a false alarm). The renders are temporary output and are never committed (§13).
- For device/discovery changes: the unit suite (with `yeelight.discover_bulbs` mocked) plus one **read-only** discovery run against the real LAN — never change device power/state to verify discovery, and never add/remove devices in the maintainer's own configuration without a reason.
- For elevation changes: the unit suite above (all Task Scheduler access mocked), plus manual inspection of the real task (`schtasks /query /tn YeelightPCCompanion-OpenRGB /xml`) and a real sleep/wake cycle. **Never** disable UAC, suppress prompts, or automate a sleep/reboot to test this.
- Manual sleep/wake and shutdown/logoff tests for power-detection changes.
- Physical Yeelight verification for LAN-light behavior.
- Verification that OpenRGB / Razer Synapse / Artemis / Yeelight Connector processes behave as expected on the target PC.

Practical notes learned from the configuration work:

- Tests that exercise a frozen/packaged path **must** redirect `LOCALAPPDATA` (or pass an explicit config path) so they cannot write into the real user profile.- When driving Qt widgets from a script, never let an exception escape a slot/QTimer callback: PyQt6 aborts the whole process (fastfail) instead of printing a traceback. That is also why the app's dialog handlers are wrapped.
- `QWizard`'s `ModernStyle` ignores a dark palette for the page title (fixed light-theme colour); `ClassicStyle` follows the palette.
- The suspend path is covered by `tests/test_runtime_config.py` **statically** (stub objects + patched `os.path`), never by actually sleeping the machine. The stub records filesystem probes instead of raising on them, because the suspend callback deliberately swallows config-read errors.
- The suspend fan-out is covered there too, with a fake socket layer and a fake clock: the clock only advances when the fan-out *waits*, so "20 unreachable devices cost one 0.35 s budget, with a single `select()` wait" is asserted deterministically instead of by timing a real network. The real-socket mechanics (non-blocking `connect_ex` + `select` + `SO_ERROR`, IPv4/IPv6 family from the literal, no `getaddrinfo`) were verified separately against a local listener on Windows, not by the suite.
- The OpenRGB SDK readiness gate is covered by `tests/test_openrgb_readiness.py` against an in-memory SDK server (which decodes the request independently, replays the measured startup as scripted pushes and never opens a socket) plus a fake clock — never against a real OpenRGB instance. The one exception is `TestTheMeasuredPlateauIsNeverReady`, which is written so it can also run against the pre-fix gate: it drives the **real** gate through the patchable `socket.create_connection` and the fake clock, and it fails there with `readiness was declared at 4.0s, but detection only completed at 12.337s`. The gate was **additionally exercised against the live SDK server** on the maintainer's PC (protocol 6 negotiated in 18 ms, the four controller ids of the protocol-6 reply parsed, already-running path ready after 12 s + settle), and OpenRGB's own startup log is the source of the measured timelines in §4.

**Verification of the detection-complete readiness gate (2026-09-19, cold start).** The previous fix's gap was closed by a real cold-start run: OpenRGB and Artemis were stopped by hand (the running `OpenRGB.exe` is elevated and cannot be terminated from an unelevated context — `Access is denied`, verified again; no privilege path was added, and the maintainer stopped them), then the normal restore was started from the packaged application. Measured:

| Step | Evidence |
| --- | --- |
| OpenRGB launched | `dist\YeelightPCCompanion\YeelightPCCompanion.exe --tray` → `[RESTORE] OpenRGB scheduled task YeelightPCCompanion-OpenRGB started.` at 00:21:54, process start 00:21:54.845, **no UAC prompt** |
| SDK connection + negotiation | protocol 6 negotiated on the first attempt; an independent second client confirmed protocol 6 and the same count |
| detection events | `OpenRGB is detecting controllers...`, `OpenRGB detection progress: …` and `OpenRGB SDK detected 1 controller(s).` at 00:21:57 |
| the plateau | the count stayed at **1** from 1.26 s to 11.91 s of the OpenRGB process (OpenRGB's own log: controllers at 1 264 / 11 907 / 12 033 / 12 161 ms) — 10.5 s of a 12.7 s startup, i.e. exactly the window the old rule accepted |
| DETECTION_COMPLETE | `OpenRGB detection completed (13.1s).` at 00:22:07 (OpenRGB's own log: detection completed 12 659 ms after process start) |
| final controller count | `OpenRGB SDK ready: 4 controller(s), detection completed (14.7s).` at 00:22:09 — the **only** readiness line of the run, after the completion event and the 1.5 s settle |
| Artemis | launched at 00:22:09.689, i.e. after the readiness line and ~2.2 s after detection completed; the Artemis OpenRGB plugin showed all four controllers on that first launch, with no plugin restart |
| no second OpenRGB instance, no rescan, no RGB mutation | one `OpenRGB.exe`; the only SDK requests the gate ever sent were `40` and `0` |

The run also exposed a defect that the unit tests had not covered: the gate logged **one line per detection-progress event**, and a real detection emits one per detector it walks through — 991 progress lines out of 1 024 restore-log lines for that single restore. `OPENRGB_READINESS_PROGRESS_LOG_SECONDS` (2 s) now rate-limits that line to a percentage change or one heartbeat per interval, with `test_a_progress_burst_is_logged_as_a_bounded_heartbeat` covering it.

Do not claim hardware, sleep/wake, UI, or executable behavior was tested if it was only inspected statically. Suspend/resume behavior must never be tested by actually sleeping the machine during development.

**Measuring a runtime-cost change (Stage 6 method, in §19).** Idle cost claims must be measured, not argued:

- Run the real window in an **isolated copy of the source tree** with its own `config.json` (the source tree *is* the data directory when not frozen), so the maintainer's real configuration and `%LOCALAPPDATA%` are never touched. The copy's `config.json` can be all-integrations-enabled with paths pointing at throwaway files, which exercises the validation probes.
- Count the recurring work deterministically rather than inferring it: wrap `get_running_processes_win32`, `ConfigManager.load`/`load_runtime`, `check_system_statuses`, `reconcile_solar_state`, `on_solar_update` and `append_log` in recording wrappers that forward to the real function, run the Qt event loop for several minutes, and report **calls per minute** plus the process's own CPU time from `GetProcessTimes` and its memory from `GetProcessMemoryInfo` (`psapi`), sampled every 30 s. Startup spikes are excluded by taking the slope between the first and last samples.
- **Neutralise the actions, never the decisions.** A profiling run that keeps automation enabled must replace `trigger_resume`, `trigger_suspend`, `kill_process_immediate`, `turn_off_devices_immediately` and `_execute_suspend_actions` with recorders, so the reconciliation logic runs for real while nothing is done to the machine. Never profile by triggering sleep, discovery, provisioning or elevation.
- Beware two measurement traps found the hard way: `_refresh_openrgb_elevation_status()` shells out to `schtasks /query` on every window construction, which varies by hundreds of milliseconds and swamps construction timings (stub it, or compare only phases that exclude it); and the **first run of a freshly built or freshly copied packaged output** pays for faulting its DLLs in from disk (measured 4.85 s versus 2.3–2.9 s warm). Report the first run separately and compare warm runs.
- Compare like with like: source against source, packaged against packaged, shown against shown, and use an **interleaved** order (A, B, A, B, …) with several rounds and `min`/median rather than a single pair of runs. When a difference is smaller than the spread within one variant, say so instead of reporting it.
- Packaged idle profiling: copy `dist\YeelightPCCompanion` to a scratch directory and add a `portable.flag` plus a `config.json` **there** (§8) — never in `dist`, and never run the packaged build against the real `%LOCALAPPDATA%` configuration. Use `--tray --no-automation --no-autorestore` for the long runs.
- Temporary instrumentation and its output (probes, CSVs, rendered images) stay outside the repository (§13).

## 18. Source of truth and drift

This memory is intended to save future agents from rediscovering the project, but the **current code is the final source of truth for implemented behavior**. If this document and the code disagree:

1. Inspect the relevant implementation and recent Git history.
2. Determine whether the code changed without the memory being updated or whether the code contains a bug.
3. Do not blindly change working code just to match stale documentation.
4. Update this file once the correct state is established.

## 19. Runtime cost profile and idle behaviour (Stage 6)

This is a long-running tray utility, so what it costs *while nothing is happening* is a feature. This section is the measured reference for that, plus the timer/thread lifecycle rules that keep it true. Re-measure with the method in §17 before claiming a change here.

### Polling cadence and timers

| Recurring work | Cadence | Notes |
| --- | --- | --- |
| `check_system_statuses()` (one native Toolhelp32 listing) | **3 s while the dashboard is visible, 15 s while it is hidden** | `STATUS_POLL_INTERVAL_VISIBLE_MS` / `STATUS_POLL_INTERVAL_HIDDEN_MS`; `showEvent` refreshes once and switches back to 3 s, `hideEvent` drops back |
| `SolarEngineThread` loop | 60 s (`msleep(1000)` × 60, so `stop()` returns within ~1 s) | recalculates the solar state, publishes to Artemis when enabled, emits `solar_update` |
| `reconcile_solar_state()` | driven by the solar update, i.e. 60 s | runs one process listing of its own for the self-healing decision |
| `_watchdog_check()` | 300 s | logs at DEBUG when healthy |
| `RestoreEngineThread` | only during a restore | never runs on a timer |

Exactly two `QTimer`s exist for the window's whole lifetime (`status_timer`, `_watchdog_timer`), both parented to the window; `exit_app()` stops both. Showing/hiding the window, saving, importing and restarting the solar engine do not create timers or duplicate their connections (asserted by `tests/test_ui.py`).

**The window's visibility is the only thing that changes a cadence.** Showing it always refreshes the service rows once (~16 ms on the development machine) so a result that is stale from the hidden period is never displayed; that refresh is tied to the cadence being slow, so an already-visible window is not re-scanned.

### Measured profile (development machine, 176 processes, all four integrations enabled, two enabled devices)

Reproduced with an isolated copy of the source tree and an isolated configuration; nothing in the maintainer's real configuration or `%LOCALAPPDATA%` is touched.

| Metric | Before Stage 6 | After Stage 6 |
| --- | --- | --- |
| `get_running_processes_win32()` | 13.0 ms min / **14.75 ms median** | unchanged (same call) |
| Idle, tray-hidden — process listings | **21.0 /min** | **5.25 /min** |
| Idle, tray-hidden — CPU (source, offscreen) | **0.599 %** of one core | **0.156 %** of one core |
| Idle, tray-hidden — CPU (packaged, portable) | 0.764 % | 0.341 % |
| Idle, window visible — process listings | 21.3 /min | 21.7 /min (fast cadence kept) |
| Idle, window visible — CPU (source, offscreen) | 0.586 % | 0.560 % (unchanged within noise) |
| Validated configuration loads (`ConfigManager.load()`, 1.05 ms median) | 2 /min (solar thread + reconciliation) | 2 /min (unchanged, see below) |
| Cold import of `yeelight_pc_companion` | 432 ms | **344 ms** |
| QApplication + theme ready (source) | 580 ms | 501 ms |
| Window + tray ready (source) | 844 ms | 711 ms |
| Packaged start → tray ready (warm) | 2.82 s median | 2.42 s median |
| In-app log view | unbounded, ~8 KB per line | bounded to `UI_LOG_MAX_BLOCKS` (+ one `UI_LOG_TRIM_BATCH`) |
| Working set, idle (source / packaged) | ~66 MB / ~145 MB, flat | ~66 MB / ~145 MB, flat |

Process listing is by far the largest recurring idle cost — at the old three-second cadence it was roughly 85 % of the tray-hidden CPU time — which is why the visibility-dependent cadence is the main optimization.

### The in-app log view is bounded in batches

`append_log()` keeps the view to the newest `UI_LOG_MAX_BLOCKS` (3 000) lines by removing the oldest blocks once the document has grown a whole `UI_LOG_TRIM_BATCH` (500) past the bound, in one edit. **Qt's own `QTextDocument.maximumBlockCount` was measured first and rejected**: it drops one block per append, and each drop invalidates the whole document layout, costing 2.8–4.6 ms *per appended line* once the limit is reached (against 0.19–0.27 ms with the batch trim and 0.17–0.25 ms with no bound at all). At the observed real-world rate of ~2 lines/minute the limit is reached within a day of uptime, so that per-line cost would apply for the rest of the session — including to the logging inside the sleep path. Measured over 40 000 appended lines from a warmed document:

| Variant | Growth | Per appended line |
| --- | --- | --- |
| unbounded (before Stage 6) | 311.6 MB | 0.252 ms |
| `maximumBlockCount` | 22.7 MB | **4.602 ms** |
| batch trim (adopted) | 30.7 MB | **0.267 ms** |

A residual, **sublinear** per-edit cost remains (≈1 350 bytes/line over the first 20 000 lines, ≈805 bytes/line over 40 000) and is Qt-internal: `maximumBlockCount` shows the same order (1 315 → 596 bytes/line) and disabling the document's undo stack changes it by <20 %. Disk logging is untouched (the rotating handler has its own size cap), "Clear" still works, and the newest lines are always the ones kept.

### Investigated and deliberately rejected

- **The duplicate validated configuration load per 60 s cycle.** `SolarEngineThread` loads the configuration for its own latitude/longitude, and `reconcile_solar_state()` loads it again immediately afterwards for the GUI's `self.config`: 2 full `ConfigManager.load()` calls (file read + migration + validation + one `os.path.isfile()` per enabled integration) ~1 ms apart. Measured cost of the redundant one: **1.05 ms**, i.e. 0.002 % of one core, plus one file read and four `stat` calls per minute. Removing it would either stop the GUI from picking up external edits or require handing a configuration dictionary from the solar thread to the GUI thread — shared cross-thread configuration state, which this stage explicitly deprioritises. The solar thread's own per-cycle reload is load-bearing (it is what picks up a latitude/longitude edit made outside the app), so it stays.
- **`maximumBlockCount` for the log bound** — see above.
- **A `QPlainTextEdit`/model-view log view**, which would avoid the rich-text document entirely: out of scope, and it would invalidate the `QTextEdit#log_display` stylesheet contract the theme and its tests rely on.
- **WMI / `psutil` / PowerShell / shell process polling.** The native Toolhelp32 listing needs no dependency and no process handles, and no measurement justified replacing it.
- **Virtualising or model/view-ing `DeviceListWidget`.** Rows are rebuilt only when the configured device list changes, and the app manages single-digit device counts.
- **Lowering repeated INFO records to DEBUG.** The healthy idle path emits **no** recurring records at all (measured: four lines at startup, then zero per minute for four minutes). The only fixed-interval record is `[SOLAR ENGINE] Reconciliation skipped because automation is paused.` once per minute, which exists **only** under `--no-automation` and is the intended signal there; lowering it to DEBUG would not even remove it from the in-app view, because `QtLogHandler` carries no level filter.
- **"Fixes" that the measurements did not reproduce:** repeated show/hide (working set plateaus at 75.4 MB by 40 cycles and is flat through 200), repeated configuration saves restarting the solar thread (1 live `QThread` after 30 restarts, 4–20 ms each, no accumulation, `terminate()` never called), and timers duplicated by save/import (still exactly two). These are covered by regression tests rather than by speculative changes.

### Timer and thread lifecycle rules

- `_restart_solar_thread()` calls `SolarEngineThread.stop()` (sets `running = False`, then `wait()`; the loop's 1 s sleeps bound that at ~1 s) before replacing the instance, so there is never more than one live solar thread. It is only called from the settings-save and import paths.
- The solar thread reads its configuration with the validated `ConfigManager.load()`; the **suspend** path keeps using the lightweight `ConfigManager.load_runtime()` (§5) and neither gained nor lost any work in this stage.
- An abandoned discovery worker is still retained in `LIVE_DISCOVERY_WORKERS` until its own `finished` signal releases it (§8c); `QThread.terminate()` is not used anywhere.
- The suspend path (`_execute_suspend_actions()`, `fire_and_forget_off_devices()`) and the whole restore path are untouched by Stage 6, and no logging or timing cost was added to either (the log batch trim was chosen specifically to avoid adding per-line cost there).

---

**Baseline captured:** 2026-09-17, at the initial private repository import plus this documentation addition.

**Updated by the *Rename & Packaging Foundation* change:** the `Lumina` → `Yeelight PC Companion` rename, the packaging privacy fix (no real `config.json` in builds), the startup-task migration to `YeelightPCCompanion`, the new log filename, and the expanded ignore rules are all reflected above. See `changelog.md` under `[Unreleased]`.

**Updated by the *Configuration & First-Run Foundation* change:** `config_manager.py` (defaults, `config_version` 1, `v0 → v1` migration, centralized validation, atomic writes + single backup, legacy discovery, import/export, LocalAppData/portable/source storage rules), `first_run_wizard.py` (six-page setup wizard), the integration enable flags with disabled-integration skipping, optional/blank Yeelight devices, the rebuilt Settings UI with Import/Export, the first `unittest` suite under `tests/`, and the expanded ignore rules are all reflected above. See §8, §8b, §12 and §17. See `changelog.md` under `[Unreleased]`.

**Updated by the *Configuration Hardening* change (post-review fixes):** the suspend callback now reads configuration through the lightweight, validation-free `ConfigManager.load_runtime()`; a failed runtime reload keeps the last known-good in-memory configuration; present-but-malformed known sections are rejected by validation instead of being silently normalized; and executable availability uses `os.path.isfile()`. Reflected in §5, §6, §8 and §12, and in `tests/test_runtime_config.py` (78 tests total). See `changelog.md` under `[Unreleased]`.

**Updated by the *OpenRGB Elevation Hardening* change:** the elevation task now carries an explicit `<WorkingDirectory>` of `dirname(OpenRGB.exe)` and the direct launch passes `cwd=dirname(exe)`; a privileged task is only created for an executable the current user provably cannot replace (effective-rights ACL check with `AccessCheck()`, fail-closed, refusal before any UAC request); an unsafe or unverifiable location is never `Ready` and is skipped by the wake path; and the queried task identity is validated further (`WorkingDirectory`, `LogonType`, principal, no triggers). The Yeelight Chroma Connector's own working-directory issue was closed separately by the *Yeelight Connector working directory* change below. Reflected in §4b and §15, and in `tests/test_windows_tasks.py` (125 tests). Nothing was provisioned on the maintainer's machine. See `changelog.md` under `[Unreleased]`.

**Updated by the *Yeelight Connector working directory* change:** the Yeelight Chroma Connector is now launched with its own executable's folder as the working directory (`cwd=os.path.dirname(connector_path)`), and every launch of an installed external program clears the frozen build from the child's DLL search environment (the process DLL directory is reset around the spawn and restored afterwards, and the child's `PATH` no longer lists the bundle). Both were needed: with only the working directory corrected, a live connector still resolved `VCRUNTIME140.dll` from `dist\...\_internal` because the bundle was on its inherited `PATH`. The restore order and every wait are unchanged, and OpenRGB, Razer Synapse and Artemis launch exactly as before. Reflected in §3. The suite then had 224 tests (302 across all three files). See `changelog.md` under `[Unreleased]`.

**Updated by the *Yeelight discovery & arbitrary device management* change (Stage 4):** `config_version` is now **2** and the two fixed address slots became the `lights.devices` list (§8, §8c) with stable ids, friendly names and per-device enablement; `v1 → v2` migration preserves every configured address and is backed up automatically. New modules `yeelight_devices.py` (device model, identity, discovery, matching) and `yeelight_device_ui.py` (discovery worker, dialogs, device list widget) were added, and the Settings tab, first-run wizard and Dashboard were updated to the arbitrary device model. Suspend, wake, daytime shutdown, self-healing and restore now iterate every enabled device with per-device fault isolation, and the suspend path reads only `enabled_device_ips(config)` so its timing contract is unchanged (§5). Discovery is user-triggered, off the GUI thread, bounded, never raises and is never part of suspend/restore. Reflected in §2, §5, §8, §8b, §8c, §12, §15 and §17; `tests/test_device_discovery.py` was added and the suite is now 388 tests (69 + 73 + 15 + 231). Nothing in the OpenRGB elevation model, the connector integration or the child-process DLL/PATH sanitation changed. See `changelog.md` under `[Unreleased]`.

> **Correction (see the hardening note below):** this note's claim that the suspend path's *"timing contract is unchanged"* was wrong. Reading only `enabled_device_ips(config)` kept the *configuration* read lightweight, but the per-device `fire_and_forget_off()` loop it fed was `O(N × 0.25 s)` and therefore broke the ~1.5 s bound as soon as the device count stopped being fixed at two. The hardening change replaces that loop with a globally bounded fan-out; §5 now states the corrected contract.

**Updated by the *Suspend & discovery hardening* change (Stage 4 review fixes):** two defects found by review of the Stage 4 work are fixed, and neither claim in the Stage 4 note above survives unchanged.

1. **The suspend fan-out is now globally bounded.** "Each device's own 0.25 s socket timeout bounds its cost" was true per device but false for the sequence: with an arbitrary device count the old `for ip in device_ips: fire_and_forget_off(ip)` loop was `O(N × 0.25 s)` — 8 unreachable devices ≈ 2 s, 20 ≈ 5 s — which breaks the ~1.5 s contract before the Connector wait and the process termination are added. OFF is now sent by `fire_and_forget_off_devices()`: one non-blocking socket per validated IP literal (address family taken from the literal — **no DNS, no `getaddrinfo()`**), one `select()` fan-out in chunks of `SUSPEND_YEELIGHT_SELECT_CHUNK`, and **one** global deadline, `SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS = 0.35 s`, covering every connect and send of the whole batch. Sockets are closed as each one finishes or when the deadline expires; `1`, `2` and `20` unreachable devices all cost the same 0.35 s. The suspend order is unchanged (dedupe → sleep flags → cancel restore → runtime config → kill Connector → 0.35 s release wait → bounded OFF fan-out → kill OpenRGB/Artemis) and the Connector still dies first. `fire_and_forget_off(ip)` remains only as a single-address wrapper over the same batch sender. §5 and §8c describe the contract, §12/§17 the tests.
2. **A timed-out discovery no longer freezes the GUI.** `run_discovery()` computed its guard from the raw `float(timeout)` (a caller passing `999` could open a ~1004-second modal guard over a 30-second search) and its `finally` then called `worker.wait(timeout + grace)` on the GUI thread, so a guard timeout blocked the UI for another full timeout/grace period and could leave a still-running `QThread` to be destroyed. The guard is now derived by `discovery_guard_milliseconds()` from the same value `clamped_discovery_timeout()` gives the search, the function returns immediately when the guard fires, and the abandoned worker is retained in `LIVE_DISCOVERY_WORKERS` until its own `finished` signal releases it (deletion posted via `deleteLater()`); `QThread.terminate()` is never called, a still-running `QThread` is never destroyed, and the stale result handler is detached so a late result cannot reach the UI or a later discovery. See §8c.

Reflected in §5, §8c, §12 and §17. `tests/test_runtime_config.py` grew from 15 to 31 tests and `tests/test_device_discovery.py` from 73 to 83; the suite is now **414 tests (69 + 83 + 31 + 231)**, and the new timing tests were confirmed to fail against the pre-fix behaviour. `yeelight_devices.py` only gained the public `clamped_discovery_timeout()` wrapper. Nothing else changed: schema/migration, Settings and wizard device management, discovery matching/adoption, the OpenRGB task, Connector launch/DLL sanitation, restore sequencing, solar behaviour and normal wake operations are untouched. See `changelog.md` under `[Unreleased]`.

> **Superseded — see the *OpenRGB detection-complete readiness gate* note below.** The readiness rule described in this note (a positive controller count that stays identical for three consecutive probes) was itself a false positive: the count is unchanged at *one* for 10.5 s of the measured 12.3 s startup, so that rule declared readiness while three of the four controllers did not exist. The measurement, the protocol facts and the OpenRGB source locations in this note are still correct and are the basis of the fix that replaced it; the *rule*, the probe helper (`probe_openrgb_controller_count` is gone), the constants (`OPENRGB_READINESS_STABLE_OBSERVATIONS` is gone) and the log lines it names are not.

**Updated by the *OpenRGB → Artemis readiness race* change (Stage 4 reliability fix):** the restore sequence no longer treats "OpenRGB was started" as "OpenRGB is usable", and the fixed `self.sleep(8)` between the OpenRGB launch and the rest of the restore is gone. The sequence now waits on a read-only OpenRGB SDK readiness gate — a positive controller count that stays identical for several consecutive probes, then a short settling delay — and Artemis is started only after that gate has finished. See §4 (restore sequence steps 4 and the new *OpenRGB SDK readiness gate* subsection) for the protocol packets, the constants, the failure/cancellation behaviour and the measured timeline.

The measurement is the point: OpenRGB's own log shows the SDK server answering requests from **31 ms** while the four controllers only registered between **1 136 ms** and **11 883 ms** and detection completed at **12 267 ms** — ~11.8 s of a 12.3 s startup in which an early client sees an incomplete list, which is exactly the reported symptom. The gate was also exercised against the live SDK server on the machine (probe 3–18 ms per call, ready after 4.2 s wall clock). Nothing about the elevation model changed: the same `YeelightPCCompanion-OpenRGB` task, the same `--gui --startminimized --server` arguments, the same one-time provisioning, one OpenRGB process, and no `--list-devices` second instance. `tests/test_openrgb_readiness.py` was added (45 tests, no real OpenRGB/socket/clock) and `RestoreFlowStub` in `tests/test_windows_tasks.py` now stands in for the gate; the suite was then **459 tests (69 + 83 + 31 + 231 + 45)**. See `changelog.md` under `[Unreleased]`.

**Updated by the *OpenRGB detection-complete readiness gate* change (2026-09-19, independent review):** the count-stability rule above was shown to be mathematically contradicted by the measured startup timeline — the controller count does not change for 10.5 s in the middle of a 12.3 s detection, so "the count stopped moving" was never a definition of "detection finished". It was reproduced against the earlier count-stability rule (ready at 4.0 s, ~8 s before controllers 2–4 existed).

**Readiness is now OpenRGB's own `DETECTION_COMPLETE` event** (SDK protocol 6), verified in the source of the installed `release_1.0` build (commit `81bbe18a`): the three detection events `101`/`102`/`103` are introduced with protocol 6, the server broadcasts `103` to **every** client whose negotiated version is ≥ 6 (no client flag, no subscription), `NET_PACKET_ID_REQUEST_PROTOCOL_VERSION` (`40`) is what negotiates that version, and protocol 6 has **no** packet that queries the current detection state — which is exactly why the gate treats "OpenRGB this restore launched" and "OpenRGB that was already running" differently. `probe_openrgb_controller_count()` and `OPENRGB_READINESS_STABLE_OBSERVATIONS` are gone; `OpenRgbSdkConnection` / `open_openrgb_sdk_connection()` negotiate, hold the connection and record the events, the count reply is parsed per negotiated protocol, and the device list is re-read the moment completion arrives so the readiness line names the list detection actually produced. The thread wrapper now sleeps through `QThread.msleep` (the gate polls at 0.5 s and settles for 1.5 s, which `QThread.sleep`'s whole seconds cannot express) instead of falling back to `time.sleep`, the timeout warning is `… could not be confirmed within 25s. Continuing.`, and the progress line is rate-limited after a real cold start showed 991 progress lines in one restore log.

Reflected in §4 (restore sequence step 4 and the rewritten *OpenRGB detection-complete readiness gate* subsection), §12 and §17. `tests/test_openrgb_readiness.py` was rewritten and grew to **78 tests**; `RestoreFlowStub` in `tests/test_windows_tasks.py` mirrors the wrapper's new signature; the suite is **492 tests (69 + 83 + 31 + 231 + 78)**. The previous change's verification gap is **closed**: a real cold start was measured end to end (OpenRGB launched at 00:21:54.845, the count at 1 for 10.5 s, `DETECTION_COMPLETE` 12 659 ms later, the only readiness line `4 controller(s), detection completed (14.7s)`, Artemis at 00:22:09.689 with all four controllers on its first launch and no UAC) — see §17. The elevation model, the task definition, the restore order, the Yeelight steps and the Razer handling are untouched. See `changelog.md` under `[Unreleased]`.

**Updated by the *UI redesign* change (Stage 5):** the interface became a **navigation sidebar + stacked pages** layout — **Overview** (day/night state, expected action, device count, coordinates as secondary detail, the four service rows from the unchanged `check_system_statuses()`, and the manual sync/sleep actions), **Devices** (the existing `DeviceListWidget` with `Discover` / `Add Manually`, per-device enable, Edit and Remove), **Integrations** (one card per integration: enable toggle, executable path, native Browse, and OpenRGB's seamless-elevation state with its Set Up/Repair action), **Automation** (Sleep & wake, Solar & location, Razer Synapse behaviour, and a Compatibility area for the retained-but-unconsumed keys) and **Logs**. The single long Settings form, the `QTabWidget`, the hero header and the glassmorphic `GlassCard` are gone, replaced by one restrained dark theme: the new **`ui_theme.py`** owns the palette, typography (Segoe UI), spacing/radii, the application/dialog/wizard stylesheets and the semantic tones, and the new **`ui_components.py`** holds the small reusable widgets (`SectionCard`, `StatusPill`, `StatusRow`, `SidebarButton`, `IntegrationCard`). The device/discovery dialogs and the first-run wizard consume that same theme instead of their own near-duplicate dark palettes, and a single shared action area (Save Changes + Import/Export) replaced the per-page button rows.

**It is presentation only.** `RestoreEngineThread`, `SolarEngineThread`, power detection, the OpenRGB detection-complete readiness gate, the suspend fan-out, the elevation model, the Connector launch/DLL hygiene, process management, the configuration schema/migration and the discovery protocol are unchanged, and no backend code was relocated; the only new modules are the two presentation ones. The semantic widget attributes the runtime and the handlers rely on (`lbl_system_status`, `lbl_sun_state`, `lbl_action_req`, `lbl_device_summary`, `service_badges`, `device_list`, `integration_widgets`, the location/automation fields, `log_display`, the OpenRGB elevation labels) were kept, so the sleep/wake handlers, the save path and the import path needed no rewrite. The window title is now `Yeelight PC Companion`, the page title and status pill replaced the banner header (§14), and user-facing strings that pointed at the old "Settings" page now name the page that actually holds the control (the wizard's elevation warning included, whose two assertions in `tests/test_windows_tasks.py` were updated accordingly).

Reflected in §4 (window structure and page ownership), §8b, §12 (the two new modules, `tests/test_ui.py`, and the suite now **551 tests: 69 + 83 + 31 + 231 + 78 + 59**) and §17 (how the pages were rendered and inspected). The packaged build was rebuilt and re-checked: `dist` contains no `config.json`, the EXE contains the new UI modules, and an isolated `LOCALAPPDATA` run shows the redesigned main window (`Yeelight PC Companion`) with no UAC and no provisioning, while a run with an unusable configuration shows the wizard (`Yeelight PC Companion - Setup`). See `changelog.md` under `[Unreleased]`.

**Updated by the *Lightweight optimization* change (Stage 6):** the application's real recurring costs were measured first and only three were changed, each because a number justified it. **The process-status cadence now follows the window's visibility** — 3 s while the dashboard is on screen, 15 s while the app is only in the tray, with a single immediate refresh whenever it becomes visible so a stale hidden-period result is never displayed. This is the stage's main win: the native Toolhelp32 listing (13–15 ms for ~176 processes) was ~85 % of tray-hidden CPU time, and the change took tray-hidden idle CPU from **0.599 % to 0.156 %** of one core (listed processes **21.0 → 5.25 per minute**) and the packaged equivalent from 0.764 % to 0.341 %, with the visible-window cost unchanged. **The in-app log view is now bounded** to the newest 3 000 lines, trimmed in batches of 500: the view previously grew by ~8 KB per line for the whole session (unbounded, measured 8 310 bytes/line), and the batch trim holds the same bound as Qt's own `maximumBlockCount` while being ~17× cheaper per appended line (0.267 ms vs 4.602 ms), which is why Qt's per-append limit was measured and rejected. **`xml.sax.saxutils` is imported where it is used** instead of at module level: it exists for one `escape()` call in the OpenRGB task XML and drags in `urllib.request`/`ssl`/`http.client`/`email`, worth ~90 ms of cold import (432 → 344 ms source) and ~0.4 s of warm packaged startup (2.82 → 2.42 s median). Also: `StatusPill.set_status` is a no-op when neither text nor tone changed (an identical `setStyleSheet` still costs a full style re-application). Everything else was **measured and left alone** — the duplicate validated configuration load once per 60 s cycle (1.05 ms, 0.002 % of one core), WMI/`psutil` process polling, `DeviceListWidget` virtualisation, lowering the one fixed-interval INFO record, and three suspected leaks (show/hide, solar-thread restarts, timer duplication) that did not reproduce; §19 records each with its numbers. The suspend path, the restore sequence, the readiness gate, the elevation model, the device/discovery code, the configuration schema and every timing constant are untouched, and no dependency was added. Reflected in §9, §12 (now **569 tests: 69 + 83 + 31 + 231 + 78 + 59 + 18**), §17 (the measurement method) and the new §19. See `changelog.md` under `[Unreleased]`.
