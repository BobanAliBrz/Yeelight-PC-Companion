# Changelog

All noteworthy changes to **Yeelight PC Companion** are tracked here.

This project follows a simple public changelog from its first open-source release onward.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), with sections used only when they are useful.

## [Unreleased]

### Fixed — OpenRGB Windows service conflicts (v1.0.1 bugfix)

OpenRGB 1.0 installations may include an **"OpenRGB" Windows service** (the
"OpenRGB SDK Server"). When that service is set to **Automatic**, it starts
OpenRGB at boot even with OpenRGB's own "Start at login" option off and with no
startup entry present. That service then owns OpenRGB's lifecycle and hardware
detection, which conflicts with Yeelight PC Companion's own launch/restore
model: some RGB hardware comes up incorrectly, YPC's stale-process cleanup
cannot reliably terminate the service-owned (often elevated) OpenRGB, and
sleep/wake control becomes unreliable. The working manual fix is to stop the
service and set it to Disabled.

YPC now **detects** that configuration automatically whenever the OpenRGB
integration is enabled:

* read-only inspection of the fixed Windows service named `OpenRGB` through the
  Service Control Manager (unelevated, minimum access rights),
* a clear Integrations-page status row (`Windows service`) that distinguishes
  absent, stopped+Disabled, stopped+Manual, running, Automatic, query failure
  and binary-path mismatch,
* one explicit **Disable conflicting service** action, enabled only when the
  service binary path matches the OpenRGB executable configured in YPC,
* a confirmation dialog explaining exactly what will happen, then a single UAC
  prompt, then stop + set startup type to Disabled, then verification of both
  facts,
* after a successful repair, an offer to run the normal Force System Sync /
  restore sequence (`trigger_resume()`) so OpenRGB is relaunched through YPC.

**There is no automatic service mutation.** Detection is automatic; the
service is only changed after explicit user approval. Startup, sleep, wake,
status polling and solar reconciliation never elevate and never change the
service. A binary-path mismatch is reported for manual review and is never
stopped or disabled automatically.

**The normal YPC scheduled-task launch/readiness architecture remains intact:**
`YeelightPCCompanion-OpenRGB`, the protocol-6 `DETECTION_COMPLETE` readiness
gate, Artemis ordering after OpenRGB readiness, and the ~1.5 s suspend budget
are unchanged. When a conflicting service-owned OpenRGB is reused at restore
time, YPC logs that it is using an externally managed instance instead of
claiming full control.

YPC does **not** restore/re-enable the OpenRGB Windows service when the
integration is disabled, on exit, on sleep, or on uninstall. A service that was
explicitly left disabled stays disabled; restoring another program's service is
more dangerous than leaving a confirmed conflict disabled.

## [1.0.0] - 2026-09-21

### Added — release foundation and the Windows installation experience (Stage 7)

The project became installable and release-ready **in structure**. Nothing was
published: the repository is still private, there is no GitHub Release, no tag, no
history rewrite and no updater.

**The application no longer runs elevated.** The open question "does the main app
need administrator rights?" was answered by measurement rather than assumption,
and the answer is no. A normal unelevated run registers both power detectors and
the shutdown receiver, lists processes, and stops the Chroma Connector and Artemis
— none of that needs elevation. The one thing that genuinely did was stopping the
*elevated* OpenRGB, and that is solved through Task Scheduler instead of privilege:
`windows_tasks.end_openrgb_task()` runs `schtasks /end /tn
YeelightPCCompanion-OpenRGB`, which was measured from an **unelevated** process
against a genuinely elevated OpenRGB — exit code **0**, the OpenRGB PID really
exited, the task returned to `Ready`, and it costs **85–108 ms**, comfortably
inside the ~1.5 s suspend budget. The suspend path now calls that before the
existing native terminate (which still handles a directly-launched, same-integrity
OpenRGB) and **verifies** afterwards, logging a warning that names the privilege
boundary if an OpenRGB somehow survives. "OpenRGB is stopped on sleep" is therefore
**not** regressed, and no privilege was loosened to achieve it: no UAC change, no
consent-policy edit, no registry prompt suppression, no new `runas` in the sleep
path.

**Start-at-logon is now a per-user `HKCU` Run entry.** The retired design
registered a highest-privilege scheduled task (`YeelightPCCompanion` →
`run_yeelight_pc_companion.bat`), which started an elevated tray process at every
logon for no functional gain. The installer now owns a single
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value, created with
`uninsdeletevalue` so uninstall removes exactly that value and nothing else in the
key. `config_manager` gained `enable_startup` / `disable_startup` /
`is_startup_enabled` / `startup_entry` / `remove_legacy_startup_tasks`.
`YeelightPCCompanion-OpenRGB` is untouched and still separate.

**A Windows installer (Inno Setup 6).** `installer/YeelightPCCompanion.iss`
installs the complete PyInstaller onedir payload **per user** into
`%LOCALAPPDATA%\Programs\Yeelight PC Companion` (`PrivilegesRequired=lowest`, so an
ordinary install needs no UAC — a machine-wide install remains selectable and is
the only path that elevates). It creates a Start Menu shortcut, offers an optional
Desktop shortcut, offers an optional start-at-logon entry, and offers **Launch
Yeelight PC Companion** when setup completes.

Uninstall removes the application files, the shortcuts, the `HKCU` startup value
and the `YeelightPCCompanion-OpenRGB` task it owns, and **leaves the user's
configuration in `%LOCALAPPDATA%\Yeelight PC Companion` untouched** — deleting it
silently would be data loss. It never removes OpenRGB, Artemis, the Yeelight Chroma
Connector, Razer or any of their files.

**Task removal during uninstall needed real handling, because deletion genuinely
requires elevation.** Measured: `schtasks /delete /tn
YeelightPCCompanion-OpenRGB /f` unelevated fails with `Access is denied` (exit 1)
and the task survives; the same deletion elevated succeeds. The uninstaller
therefore (1) tries it unelevated, (2) **verifies with a fresh `schtasks /query`
instead of trusting the exit code**, and (3) if it is still there, asks Windows for
a single narrowly scoped elevation that runs exactly that one delete. Success
requires **both** the launch to succeed **and** `ResultCode = 0`, then is confirmed
by re-querying. A **silent** uninstall never raises an invisible consent prompt — it
reports the task as left in place. Failing to remove it is reported, never fatal.

**Two release artifacts that differ by exactly one file.** The installer payload
must **not** contain `portable.flag` (its presence would silently relocate a user's
configuration into the application folder); the
`Yeelight-PC-Companion-<version>-portable.zip` **must** contain it (its absence
would silently send a portable user's data to `%LOCALAPPDATA%`). Both directions
are enforced by the privacy scanner.

**One version source of truth.** `app_metadata.py` holds `VERSION = (1, 0, 0)` and
the product identity. `version_info.txt` (EXE metadata) is **generated** by
`tools/write_version_info.py` and `installer/version.iss` by
`tools/write_installer_version.py`; artifact names come from the same value. The
`.iss` script `#include`s the generated file and has **no hard-coded fallback**, so
a missing include is a compile error rather than a mislabelled installer. Both
generated files are `--check`ed in CI and by the release build.

**An artifact-specific privacy scanner** (`tools/release_privacy_scan.py`) replaces
the build script's `config.json`-only guard. Three layers: exact forbidden
filenames (`config.json`, its backups, runtime logs, provisioning diagnostics,
atomic-write temp files, `__pycache__`), the two-directional `portable.flag` rule,
and **exact private values supplied from outside tracked source**, matched in text
files. Generic substring rules such as a bare mail domain or a two-decimal number
were considered and **deliberately refused** because they collide with dependency
binaries; a test asserts no such rule can be reintroduced. (The private-value layer
was redesigned during Stage 7 hardening — see the *Fixed* section below.)

**A deterministic release pipeline.** `build_release.ps1` runs version metadata →
unit tests → syntax/import checks → PyInstaller → privacy scan of the dist payload
→ portable payload + privacy scan → Inno Setup compile → verification of the
packaged payload, failing on the first problem and never requiring a personal
`config.json`. Verification prefers extracting the installer payload and falls back
to a **reversible per-user test install** in a throwaway directory.

**CI and a prepared release workflow.** `.github/workflows/ci.yml` runs on a
Windows runner with actions pinned to major versions: dependencies, `compileall`,
import checks, version-metadata freshness, the full suite and the repository
privacy scan. `.github/workflows/release.yml` is tag-triggered (`v*`) or manual,
runs the same pipeline, and uploads the installer, portable ZIP and
`SHA256SUMS.txt` **as workflow-run artifacts only** — it deliberately does **not**
create a GitHub Release, so a mis-tagged build cannot become public by accident.
Neither workflow touches real hardware, LAN discovery, Task Scheduler or UAC.

**`GPL-3.0-only`.** `LICENSE` carries the GNU GPL v3 text and `README.md`
references it. PyQt6 is the reason for the choice: it is itself `GPL-3.0-only`
under Riverbank's open-source terms. All direct dependencies were verified
compatible — PyQt6 `GPL-3.0-only`, yeelight BSD, **ephem 4.1.5 MIT** (confirmed in
the installed distribution metadata and the bundled `licenses/LICENSE`), requests
Apache-2.0, and PyInstaller `GPL-2.0-or-later` with its bundling exception
(`PyQt6-Qt6` is `LGPL v3`). Per-file SPDX headers were deliberately not added.

**Reproducibility.** `PyInstaller` moved out of the build BAT into a new
`requirements-build.txt`, pinned to **6.21.0** — the version actually used by the
verified build (the earlier assumption of 6.16.0 was wrong). `requirements.txt`
pins `PyQt6==6.11.0` instead of `>=`. No transitive dump was added.

**An unused Tcl/Tk runtime was removed from the bundle.** Nothing in the project
imports `tkinter`, yet PyInstaller bundled `_tcl_data`, `_tk_data`, `tcl86t.dll`,
`tk86t.dll` and `_tkinter.pyd` (~7.4 MB). The spec now excludes it; the installed
payload was confirmed free of all five.

**A public `README.md`** with: what the app is; features; the Windows-only scope;
requirements; installer and portable installation; first run; the **Yeelight LAN
Control** requirement; integrations; the one-time OpenRGB UAC explanation and why
UAC is never bypassed; sleep/wake behaviour; discovery; a privacy/local-network
statement (no telemetry, coordinates used locally only, the exact LAN/localhost
traffic); where configuration lives per build kind; uninstall behaviour;
troubleshooting; development/build instructions; the repository layout; versioning;
releasing; **code signing** (absent, so SmartScreen warnings are expected, and no
self-signed certificate is passed off as public trust); **updates** (none, by
design); and the licence with a dependency table. It exposes no coordinates, LAN
IPs, executable paths or device IDs.

**Stale instructions were corrected.** `build_yeelight_pc_companion.bat` no longer
tells users to hand-copy `config.example.json` and is now labelled a development
helper; `register_startup.bat` no longer creates an elevated task, no longer
requires administrator rights, and is labelled a legacy migration helper; the
README tells public users to use the installer and never a BAT file.
`config.example.json` is still shipped as template data but is not required.

**Code signing and updates.** Both are deliberately absent. The installer and EXE
are unsigned, so SmartScreen/reputation warnings are expected on first download;
signing remains possible later as an optional step, and no certificate or key may
enter the repository (`.gitignore` blocks `*.pfx`, `*.p12`, `*.pvk`, `*.snk`,
`*.cer`, `*.key`). No updater exists and nothing checks the network; the single
version source, stable installer `AppId`, per-user install and `SHA256SUMS.txt` are
the pieces a later opt-in updater would build on.

**Historical privacy status.** The tracked tree is clean — every confirmed private
value is absent from it. Before the first public release, the private development
history was intentionally replaced with a clean public baseline to remove
historical maintainer-specific fixture data and author metadata: the repository now
carries a single, parentless root commit, and no identifier from the earlier
private history is referenced anywhere in the tracked tree. No real `config.json`,
backup or runtime log ever entered history, and no secrets, keys or certificates
were found. The scanner policy that produced this baseline is described in
`project_memory.md` §13a/§13b.

### Fixed — Stage 7 hardening (independent review findings)

Independent review accepted Stage 7 architecturally and found four specific
defects. Only those were fixed; no new roadmap stage was started.

**The suspend hot path no longer has a multi-second failure budget.**
`end_openrgb_task()` had grown three independent per-call guards: a 30 s
`schtasks /query` to decide whether the task existed, a 5 s `END_TASK_TIMEOUT_SECONDS`
for `/end`, and a further 2 s of post-stop polling — on a step inside a sequence
whose whole target is ~1.5 s. The measured 85–108 ms happy path does not justify a
multi-second worst case. The stop is now built around **one global monotonic
deadline**, `END_TASK_STOP_BUDGET_SECONDS = 0.4 s`, that covers the `schtasks /end`
invocation, its process-exit verification and every wait in between:

- **no task-existence query before the stop** — an absent or idle task is
  classified from the `/end` wording inside the same budget, and the existing
  native same-integrity terminate still runs afterwards either way;
- the timeout handed to `schtasks.exe` is whatever is *left* of the deadline,
  never a fixed constant, and there are no retries beyond it;
- verification still happens (exit code 0 is never trusted on its own; an
  unreadable process list is never read as "nothing is running"), and the
  post-stop honesty logging is kept, now including the measured duration;
- on expiry the stop reports failure immediately and the suspend sequence
  continues to its remaining steps;
- no UAC, filesystem probing, configuration validation, DNS or discovery was
  added to the path.

The 0.4 s is derived against the whole sequence, not the step alone: the
Connector release wait (0.35 s) + Yeelight network budget (0.35 s) + OpenRGB task
stop (0.40 s) + native controller terminate (0.35 s) = **1.45 s** of explicitly
budgeted phases against the 1.5 s target, and a test asserts that sum stays
strictly below the target. The Connector release wait and the Yeelight global
network budget are unchanged.

New regression tests use a **fake monotonic clock and a fake `schtasks`** (with
`os.name` patched), so they are deterministic and nothing sleeps: a missing task,
a hung `schtasks.exe`, an accepted `/end` whose process does not disappear, an
unreadable process list and the normal success path are each proven globally
bounded, and the *real* `_execute_suspend_actions()` is run with OpenRGB enabled
to prove the sequence's only waits fit the budget and that every later step is
still reached.

**The retired elevated startup tasks are now actually migrated on install and
upgrade.** The documentation and the release report claimed they were removed
"during migration", but nothing did it on a normal install: `remove_legacy_startup_tasks()`
existed without a caller, the installer only attempted removal in `[UninstallRun]`,
and `register_startup.bat` attempted an elevated deletion unelevated. The installer
now runs a one-time migration from `CurStepChanged(ssPostInstall)`:

- scope is exactly the two fixed names `YeelightPCCompanion` and
  `LuminaLightOrchestrator`, held in one place (`LegacyStartupTaskCount()` +
  `LegacyStartupTaskName(Index)` — Inno Setup's PascalScript has no array
  constants, verified with the real compiler) — no task-name parameter and no
  command interface;
- **`YeelightPCCompanion-OpenRGB` is never touched** by the migration (its removal
  stays a separate uninstall concern);
- detection is silent, unelevated deletion is attempted first, and success is
  verified with a fresh `schtasks /query` rather than trusted from an exit code;
- only if a fixed legacy task *verifiably survives* is **one** narrowly scoped
  elevation requested: a single elevated helper receives the remaining fixed
  names as `schtasks /delete` arguments, so the user sees **exactly one** consent
  prompt no matter how many tasks remain — and a fresh install with neither task
  returns before any elevation, so it raises **no UAC at all**;
- a silent install never raises an invisible consent prompt;
- a declined or failed cleanup is reported clearly (the task is named as left in
  place, with the warning that it may start an elevated duplicate at sign-in) and
  **never fails or blocks the installation**;
- `register_startup.bat` no longer claims it can always remove the old tasks: it
  checks each exit code, re-queries to verify, and explains that a task it could
  not delete needs administrator rights (the installer, or Task Scheduler "Run as
  administrator").

**The release workflow no longer interpolates a GitHub boolean into PowerShell.**
`if (${{ inputs.skip_tests == true }})` rendered as `if (True)`/`if (False)`, which
is not PowerShell syntax. The decision now belongs to the workflow engine, as two
plain steps with mutually exclusive GitHub-level conditions: the normal build runs
for `github.event_name != 'workflow_dispatch' || !inputs.skip_tests`, and the
packaging-iteration build runs only for `github.event_name == 'workflow_dispatch' && inputs.skip_tests`.
**A tag-triggered build therefore always runs the tests.** Static tests evaluate
both conditions against every (event, `skip_tests`) combination, parse the workflow
with a real YAML parser, and fail if any `run:` block regains a `${{ }}`
interpolation.

**No private-value fingerprint is stored in tracked source any more.** The privacy
scanner recorded each maintainer value as `sha256(value)[:8]` plus its exact
length. That is not a privacy mechanism — 8 hex characters are a 32-bit truncated
digest, and a low-entropy value such as a private IPv4 address or a short user name
is brute-forceable even from a full unsalted hash, with the recorded length making
the search cheaper still. Those fingerprints, lengths and the tests derived from
the real values were removed. The scanner now compares **exact values supplied from
outside tracked Git**:

- `--private-value VALUE` (repeatable),
- `--private-values FILE`, or `$YPC_PRIVATE_VALUES_FILE`, or the gitignored
  `private_values.local.txt` in the repository root,
- `$YPC_PRIVATE_VALUES` (one value per line), which the release workflow passes
  from an optional `YPC_PRIVATE_VALUES` repository secret.

Builds work with **no** source configured — the layer simply has nothing to match,
which is the normal state for contributors, for CI without the secret, and for a
public release run — and maintainer/local builds can supply the exact values so
the check stays as strong as before. The values, their fingerprints and any test
written from them never enter tracked files; the suite uses synthetic fake secrets
only, and a matched value is never printed (violations name the file and the
number of matches). The permanent tracked checks — forbidden filenames, runtime
logs and backups, the `portable.flag` direction, and the other generic release
rules — are unchanged. The values never enter tracked files, and the clean
public-history baseline (`project_memory.md` §13b) removes the earlier
fingerprint-bearing revisions from the published tree.

**Documentation corrections.** `project_memory.md` described an earlier commit as a
"previous public commit", but the repository has never been public; the wording now
says "previous pushed commit". The scanner policy (§13a), the public-history
baseline note (§13b), the installer model (§10c), the release workflow notes (§10d)
and the test inventory were updated for the changes above, including the note that
a truncated digest is not a privacy mechanism.

**Tests and verification.** The suite grew from **661 to 712 tests**, and the full
`build_release.ps1` pipeline (tests, syntax/import checks, PyInstaller, both
privacy scans, Inno Setup compile and installed-payload verification) was re-run
end to end and passed. The installer was validated against the real compiler
(ISCC 6.7.3) and by a reversible silent per-user test install into a throwaway
directory, which exercised the **new install-time legacy migration on a machine
that genuinely had the retired `YeelightPCCompanion` logon task** — its own log
recorded the whole sequence:

```text
Legacy startup task YeelightPCCompanion is present; attempting removal.
Legacy task deletion failed (task=YeelightPCCompanion, elevated=0, launched=1, exit=1).
Legacy startup task YeelightPCCompanion survived the unelevated removal.
Legacy startup task LuminaLightOrchestrator is not present.
A retired elevated startup task needs administrator rights and this is a silent
installation, so no consent prompt is shown. The task was left in place; ...
```

So detection, the unelevated-first attempt, the access-denied outcome, the fresh
re-query that refused to trust the exit code, the second fixed name and the
**silent-install no-prompt guard** are all confirmed against real Task Scheduler
state. The maintainer's task was verified byte-identical afterwards (identical
XML SHA-256 before and after), no UAC prompt appeared, installation succeeded
(exit 0), and the `YeelightPCCompanion-OpenRGB` task was untouched. The measured
real `schtasks /end` this run was **0.109–0.138 s** for the complete
`end_openrgb_task()` operation (including verification), inside its 0.4 s budget,
with the whole suspend sequence finishing in **0.472–0.502 s**.
Nothing was published: no tag, no GitHub Release, no change to repository
visibility, no history rewrite and no force-push.

**Tests.** **51 test methods added and 3 replaced** (suite now **712** = 231 + 94 +
83 + 78 + 74 + 69 + 44 + 36: `test_windows_tasks` 231, `test_release_foundation`
94, `test_device_discovery` 83, `test_openrgb_readiness` 78, `test_ui` 74,
`test_config_manager` 69, `test_startup_and_uninstall` 44, `test_runtime_config`
36). The hardening added:
deterministic fake-clock/fake-`schtasks` tests for the task-aware stop and for the
real suspend sequence with OpenRGB enabled; installer tests for the install-time
legacy migration (fixed scope, no OpenRGB crossover, verification instead of exit
codes, no prompt on a fresh or silent install, never fatal); workflow tests that
evaluate both build conditions across every trigger and parse the YAML; and
privacy-scanner tests driven entirely by synthetic fake secrets. `test_release_foundation.py` covers the single version source and both
generated files (including that `--check` fails when stale), the absence of any
second version literal, **both** directions of the `portable.flag` policy (with one
directory proving clean as portable and dirty as installer), every forbidden
filename variant, that binaries are never content-scanned and no generic substring
rule exists, the installer configuration (per-user, shortcuts, startup, launch, no
private path, uninstall checking both launch and `ResultCode`, exactly two narrow
`ShellExec('runas')` points — one per flow — unelevated-first, no prompt when
silent, verification that the task is gone), installed-style vs portable storage
selection, the shipped template matching the application defaults, the build
script's ordering and artifact checks, and the CI/release workflows.
`tests/test_startup_and_uninstall.py` covers the
per-user startup entry through an in-memory `winreg` fake (quoting, `--tray`,
idempotence, updating a moved install, and **never touching another program's
value** on enable or disable), legacy task migration touching only the two fixed
names, the narrow removal CLI (no extra arguments, no generic task-name interface,
no task-name parameter on `remove_openrgb_task`), and the task-aware stop's four
distinct outcomes, fixed target, single global deadline and non-raising behaviour —
driven by a fake monotonic clock and a fake `schtasks`, so nothing sleeps. The
real registry and the real Task Scheduler are never touched by the suite.

**Verification.** The suite passes (**712**), every module compiles, the full
`build_release.ps1` pipeline was run, and the installer was exercised on a real
machine. A **reversible per-user test install** into a throwaway directory
(silent, `/NOICONS`, no startup task) confirmed the full payload lands correctly —
`YeelightPCCompanion.exe`, `config.example.json`, `yeelight_pc_companion.ico`,
`LICENSE.txt` and the uninstaller — with **no `portable.flag`**, and the privacy
scan of the installed payload was clean. The installed executable was launched from
the installed directory and logged to `%LOCALAPPDATA%\Yeelight PC Companion`,
registering both power detectors and the shutdown receiver. The uninstaller was then
tested in **both** modes: **silent** (app files and the `HKCU` Run value removed,
the OpenRGB task correctly reported as left in place without any invisible consent
prompt, user configuration preserved) and **interactive** (the elevated removal ran
and the task was genuinely removed, with `User configuration ... was left
untouched` in the uninstall log). The maintainer's own configuration was backed up
by hash before testing and verified byte-identical afterwards, and the
`YeelightPCCompanion-OpenRGB` task was restored from its export and re-verified
(`Ready`, same command, same working directory). Nothing was published, nothing was
made public, and the PC was never sent to sleep.

**Verification.** The suite passes (**661**), every module compiles, the full
`build_release.ps1` pipeline was run, and the installer was exercised on a real
machine. A **reversible per-user test install** into a throwaway directory
(silent, `/NOICONS`, no startup task) confirmed the full payload lands correctly —
`YeelightPCCompanion.exe`, `config.example.json`, `yeelight_pc_companion.ico`,
`LICENSE.txt` and the uninstaller — with **no `portable.flag`**, and the privacy
scan of the installed payload was clean. The installed executable was launched from
the installed directory and logged to `%LOCALAPPDATA%\Yeelight PC Companion`,
registering both power detectors and the shutdown receiver. The uninstaller was then
tested in **both** modes: **silent** (app files and the `HKCU` Run value removed,
the OpenRGB task correctly reported as left in place without any invisible consent
prompt, user configuration preserved) and **interactive** (the elevated removal ran
and the task was genuinely removed, with `User configuration ... was left
untouched` in the uninstall log). The maintainer's own configuration was backed up
by hash before testing and verified byte-identical afterwards, and the
`YeelightPCCompanion-OpenRGB` task was restored from its export and re-verified
(`Ready`, same command, same working directory). Nothing was published, nothing was
made public, and the PC was never sent to sleep.

### Changed — the app's real background costs were measured and reduced (Stage 6)

This is a long-running tray utility, so what it costs while nothing is happening is a feature. Every change below started from a measurement, and everything that was measured but *not* changed is listed at the end so the next reader does not re-investigate it. Nothing was taken out of the sleep path or the restore path, and no dependency was added.

- **The service-status check now follows the window's visibility: every 3 s while the dashboard is on screen, every 15 s while the app is only in the tray.** The Overview's four service rows are refreshed from a native Toolhelp32 process listing, which measures **13.0 ms minimum / 14.75 ms median** for the 176 processes on this machine — cheap per call, but at the old unconditional three-second cadence it was roughly **85 % of the application's tray-hidden CPU time**. While the window is only in the tray nobody can see those rows, and the check now costs four listings a minute instead of twenty. Measured on an isolated copy of the source tree against a real configuration (all four integrations enabled, two enabled devices), four minutes per mode:

  | Idle metric (tray-hidden) | Before | After | Change |
  | --- | --- | --- | --- |
  | Process listings | 21.0 / min | 5.25 / min | **−75 %** |
  | CPU (source, offscreen) | 0.599 % of one core | **0.156 %** | **−74 %** |
  | CPU (packaged, portable, `--tray --no-automation --no-autorestore`) | 0.764 % | **0.341 %** | **−55 %** |
  | Working set / private | 66.2 MB / 39.4 MB, flat | 66.3 MB / 39.6 MB, flat | unchanged |

  **Showing the window always refreshes the rows once** (~16 ms — one listing) so a result that is stale from the hidden period is never displayed, and the window then runs at the original three-second cadence: a real-platform run measured 20.6 listings/minute while visible (21.3 before) and 4.3–5.1/minute while hidden, with exactly one refresh per hidden→visible transition across ten show/hide cycles. That refresh is tied to the cadence having been slow, so an already-visible window is not re-scanned. Nothing else about the check changed — same native call, same rows, same wording — and the Overview hint now states the new behaviour.
- **The in-app log view is bounded.** The Logs page's `QTextEdit` grew without limit for the lifetime of the process — measured at **8 310 bytes per appended line**, which at the application's real log rate (~2 lines/minute over a 13-hour real log) is memory that only ever went up. It now keeps the newest **3 000 lines**, trimmed in batches of 500. Qt's own `QTextDocument.maximumBlockCount` was measured first and **rejected**: it drops one block per append, and every drop invalidates the whole document layout, costing **4.602 ms per appended line** once the limit is reached (against 0.267 ms with the batch trim and 0.252 ms with no bound at all) — and since the limit is reached within a day of uptime, that cost would have applied for the rest of every session, *including to the logging inside the sleep path*. Over 40 000 appended lines from a warmed document:

  | Variant | Growth | Per appended line |
  | --- | --- | --- |
  | unbounded (before) | 311.6 MB | 0.252 ms |
  | `maximumBlockCount` | 22.7 MB | 4.602 ms |
  | batch trim (adopted) | 30.7 MB | **0.267 ms** |

  A residual, sublinear Qt-internal per-edit cost remains (≈1 350 bytes/line over the first 20 000 lines, ≈805 bytes/line over 40 000) and is the same order as Qt's own limit shows (1 315 → 596 bytes/line). Disk logging is untouched — the rotating handler keeps its own 2 MB × 3 cap and, in a test with a real `RotatingFileHandler`, still receives every one of 3 025 records while the view keeps only the newest 3 000 — "Clear" still clears just the view, and the newest lines are always the ones retained.
- **`xml.sax.saxutils` is no longer imported at startup.** It exists for a single `escape()` call in the OpenRGB elevation-task XML, but importing it also pulls in `urllib.request`, and with it `ssl`, `http.client` and `email` — worth **~90 ms of cold import (432 → 344 ms, −20 %)** from source and, measured warm on the packaged build, **2.82 s → 2.42 s median to tray-ready (−0.40 s, −14 %)**. The import moved into `build_openrgb_task_xml()`, the one function that needs it and a path that never runs at startup; the escaping is byte-for-byte unchanged, which a task-definition test requiring the document to stay well-formed XML that round-trips a path containing `&` still asserts. A test now imports `windows_tasks` in a fresh interpreter and fails if either `xml.sax.saxutils` or `urllib.request` is back in `sys.modules`.
- **A status pill that did not change is no longer restyled.** `StatusPill.set_status()` is now a no-op when neither the text nor the tone changed. Qt does not do this by itself: twenty identical calls on one pill produced twenty `StyleChange`, forty `PaletteChange` and forty `FontChange` events. The service rows were the one place setting status on a repeating timer, where nearly every tick repeated the previous state.
- **Measured and deliberately left alone** (each with the number that decided it): the duplicate validated configuration load once per 60 s cycle — `SolarEngineThread` loads the file for its latitude/longitude and `reconcile_solar_state()` loads it again ~1 ms later for the GUI's copy, and the redundant one costs **1.05 ms**, 0.002 % of one core plus one file read and four `stat` calls per minute, which is not worth handing a configuration dictionary across threads; the solar thread's own reload, which is what picks up a latitude/longitude edited outside the app and therefore stays; process polling via WMI/`psutil`/PowerShell, since the native listing needs no dependency and no process handles; virtualising `DeviceListWidget`, which rebuilds rows only when the configured device list changes; converting the one fixed-interval INFO record (`[SOLAR ENGINE] Reconciliation skipped because automation is paused.`, which exists only under `--no-automation`) to DEBUG, which would not even remove it from the in-app view because `QtLogHandler` carries no level filter — and the healthy idle path was measured to emit **no** recurring records at all (four lines at startup, then zero per minute for four minutes); and three suspected leaks that did not reproduce — repeated show/hide (working set plateaus at 75.4 MB and is flat from 40 to 200 cycles), repeated configuration saves restarting the solar engine (one live `QThread` after 30 restarts, 4–20 ms each, `terminate()` never called) and duplicated timers (still exactly two for the window's lifetime).
- **Untouched on purpose:** the suspend path (`_execute_suspend_actions()`, `fire_and_forget_off_devices()` and its 0.35 s budget), the whole restore sequence and its conservative waits, the OpenRGB detection-complete readiness gate, the elevation model, the device list and LAN discovery, the configuration schema/migration/atomic writes, and every existing timer and `QThread` ownership rule. The batch log trim was chosen over Qt's per-append limit specifically so the sleep path's logging does not get more expensive.
- **Tests.** **18 tests added** (suite now **569: 69 + 83 + 31 + 231 + 78 + 59 + 18**). `tests/test_ui.py` gained `TestStatusPollingCadence` (tray-only cadence, the switch in both directions on show/hide, exactly one refresh when the window becomes visible and none when it already was, the fast cadence being a documented constant, no timer added by repeated show/hide, and the check still reporting nothing but real process state), `TestUiLogIsBounded` (the bound and its batch being configured, the configured bound holding a real 3 540-line fill down, the newest lines kept, at most one effective trim per batch, Clear still working, and a real `RotatingFileHandler` still receiving every record) and `TestStatusPillUpdates` (a repeated state is not restyled, a changed text or tone still is). `tests/test_windows_tasks.py` gained `TestStartupImportCost` plus an XML-escaping assertion. All 551 pre-existing tests pass unchanged.
- **Verification.** Every module compiles and the suite passes. The packaged build was rebuilt with `build_yeelight_pc_companion.bat` (privacy guard passed; `dist\YeelightPCCompanion` contains no `config.json`, and the analysis still bundles `xml.sax.saxutils`, so provisioning is unaffected). It was then run from an isolated copy of the build output in portable mode — never against the real `%LOCALAPPDATA%` configuration — for a five-minute `--tray --no-automation --no-autorestore` idle profile and for a 25-second visible smoke run: both start cleanly, register both power detectors and the shutdown receiver, log normally and leave the machine untouched. The cadence behaviour was also verified on the **real Windows platform plugin** against a live machine (all four services actually running), where the four Overview badges were confirmed to agree with an independent process listing, all five pages were switched, and `WA_DontShowOnScreen` kept every window off the desktop. Nothing here was verified by putting the PC to sleep, running a discovery, provisioning a task or raising a UAC prompt.

### Changed — the interface was redesigned (Stage 5)

- **The window is no longer a tabbed form.** The `QTabWidget` with a Dashboard, one very long Settings page and a System Logs tab became a **left navigation sidebar plus a content stack**: **Overview**, **Devices**, **Integrations**, **Automation**, **Logs**. Each page answers one question, the selected page is obvious, and the window stays usable from about 820 × 540 up to a full desktop size (pages that can outgrow their viewport scroll).
- **Settings were split by subject.** The single form is gone: Yeelight devices have their own page (`Discover` / `Add Manually` / per-device enable / Edit / Remove, with the `X enabled / Y configured` count above the list), each integration has its own card (enable toggle, executable path, native **Browse**, and for OpenRGB its seamless-elevation state with the **Set Up / Repair** action), and the automation settings are grouped into **Sleep & wake**, **Solar & location**, **Razer Synapse behaviour** and a small **Compatibility** area for the retained-but-unused keys.
- **One shared action area instead of repeated button rows.** **Save Changes** (primary) plus **Import Configuration** and **Export Configuration** (secondary) sit in a single bar at the bottom of the pages that edit configuration. Save still collects every setting, validates everything, writes atomically, synchronises the OpenRGB elevation task exactly where it did before, refreshes the dashboard and restarts the solar engine; there is no autosave.
- **The look is now a restrained dark Windows utility** instead of a glassmorphic dashboard: graphite/dark-navy surfaces, one muted indigo accent, green for running, amber for night/warning, red reserved for destructive states, Segoe UI, an 8/12/16/24 px spacing rhythm and 6–12 px radii. All of it lives in the new `ui_theme.py` (palette, typography, spacing, application/dialog/wizard stylesheets, semantic tones) with the small reusable widgets in the new `ui_components.py` (`SectionCard`, `StatusPill`, `StatusRow`, `SidebarButton`, `IntegrationCard`). No fonts, images, gradients, blur effects or third-party UI dependencies were added; the log view uses a system monospace font.
- **The tabs' hero header is gone.** The `YEELIGHT PC COMPANION | LIGHT ORCHESTRATOR` banner became a page title with a one-line subtitle, and the live system status is a pill on the right (`System active` / `Suspending` / `Restoring...`) that the existing sleep/wake handlers keep updating through the same `lbl_system_status` attribute.
- **The Overview answers the five questions it is for**: the current day/night state and the expected action (amber at night, accent by day), the `X enabled / Y configured` device count, the coordinates as *secondary* detail, the four services as compact rows (`Running` / `Stopped`, reported by the process-status check — nothing new is probed; since Stage 6 that check runs every three seconds while this window is open and every fifteen seconds while the app is only in the tray, see the Stage 6 entry), and the two manual actions. **Run Sleep Actions** now says what it does: it runs this app's sleep steps (close the light-control applications, switch the Yeelight devices off) and explicitly **does not** put Windows to sleep.
- **Dialogs, device UI and the first-run wizard share that one theme.** The almost-duplicate dark palettes of the device dialogs and the wizard are gone (`apply_device_dialog_style()` and the wizard now consume `ui_theme`), so the device editor, the discovery progress/results dialogs, `QMessageBox` context, the wizard and the main window cannot drift apart. Native Windows file dialogs stay native, and the wizard keeps its six pages, its workflow, its validation, its import shortcut, its zero-device path and its one-time OpenRGB elevation step.
- **Presentation only — the automation engine was not touched.** `RestoreEngineThread`, `SolarEngineThread`, power detection, the OpenRGB SDK readiness gate, the suspend fan-out, the elevation model, the connector launch/DLL hygiene, process management, the configuration schema/migration and the discovery protocol are unchanged; no backend code was relocated. What moved out of `yeelight_pc_companion.py` is presentation only (`ui_theme.py`, `ui_components.py`), and the semantic widget attributes the runtime relies on (`lbl_system_status`, `lbl_sun_state`, `lbl_action_req`, `lbl_device_summary`, `service_badges`, `device_list`, `integration_widgets`, the location/automation fields, `log_display`, the OpenRGB elevation labels) were kept, so no handler needed rewriting. User-facing strings that pointed at the old "Settings" page now name the page that actually holds the control (the wizard's banner warning included, with its two assertions updated).
- **Tests.** Added `tests/test_ui.py` (**59 tests**) covering the build and wiring of the redesigned window: construction, the sidebar and page navigation, the Overview content (solar/device/service state, the manual actions reaching the real handlers, the suspend/resume status updates, the suspend deduplication), the device page's use of the existing device model and its Discover/Add wiring, all four integration cards including enable→path/Browse enablement and the OpenRGB elevation states (including the "unavailable is not fatal" path), the automation fields and a byte-for-byte semantic check of the collected configuration (plus unknown-key preservation), the save validation/atomic-write path, import refreshing every redesigned control, the log page and its Clear button, the device editor and discovery-results dialogs, the wizard's six pages, and three explicit guards that UI construction starts no discovery, requests no elevation/provisioning and triggers no restore/suspend work. The suite is now **551 tests (69 + 83 + 31 + 231 + 78 + 59)**; all 492 pre-existing tests still pass unchanged.
- **Verification.** Every module compiles; the suite passes; the packaged build was rebuilt and checked for the missing `config.json` privacy guarantee. The redesigned pages were rendered with real Windows fonts (`WA_DontShowOnScreen` + `grab()`, never a window on the desktop) at 900 × 650, 1120 × 720 and 1280 × 800, at 100 % and 150 % scaling, together with the wizard and both dialogs — which is how a stale "no devices configured" state (a missing `_populate_settings_widgets()` call), the right-aligned location fields and a wrapped action-bar hint were caught and fixed before this entry. The screenshots themselves are temporary verification output and are not committed.

### Fixed — the OpenRGB readiness rule was still a false positive (detection-complete gate)

- **The controller-count stability rule was mathematically contradicted by the real startup timeline.** It declared Artemis ready after three identical positive count observations (0.5 s apart) plus a 1.5 s settle, but OpenRGB's own measured cold start holds the count at exactly **one** for 10.5 of its 12.3 seconds: the SDK server answers requests from 31 ms, the first controller registers at 1 136 ms and controllers 2–4 only at 11 684 / 11 755 / 11 883 ms, with detection completing at 12 267 ms. The rule therefore went ready at **4.0 s** — roughly eight seconds before three of the four controllers existed — which is the original race, unchanged. Reproduced before touching the implementation: `TestTheMeasuredPlateauIsNeverReady` replays exactly that timeline through the real gate and fails against the earlier count-stability rule with `readiness was declared at 4.0s, but detection only completed at 12.337s`.
- **Readiness is now OpenRGB's own completion signal.** SDK protocol 6 defines `NET_PACKET_ID_DETECTION_STARTED` (`101`), `NET_PACKET_ID_DETECTION_PROGRESS_CHANGED` (`102`) and `NET_PACKET_ID_DETECTION_COMPLETE` (`103`), and the installed build implements them. Verified in the source of the installed `release_1.0` build (commit `81bbe18a`, the commit the installed `OpenRGB.exe` reports in its own log): `NetworkProtocol.h:28/125-127` (protocol 6 introduces the events), `NetworkServer.cpp:873` (`SignalDetectionCompleted()` broadcasts to every `ServerClients` entry), `NetworkServer.cpp:3945` (`103` goes to clients whose negotiated version is ≥ 6 — **no client flag and no subscription**, unlike the ProfileManager), `NetworkServer.cpp:1668`/`:3704` (packet `40` negotiates `min(client, server)` and answers with the server's maximum), `DetectionManager.cpp:867` (detection's last act signals completion) and `Documentation/OpenRGBSDK.md`. A client that connects during detection is still connected when `103` arrives; and protocol 6 has **no** packet that queries the current detection state, which is what the two paths below handle.
- **The gate now negotiates the protocol and holds one read-only connection.** `open_openrgb_sdk_connection()` / `OpenRgbSdkConnection` replace the unnegotiated single-shot probe: the connection sends `NET_PACKET_ID_REQUEST_PROTOCOL_VERSION` (`40`) once (its reply is the server's maximum, so this client's version is `min(6, reply)`, and a server that answers nothing is protocol `0`), then only `NET_PACKET_ID_REQUEST_CONTROLLER_COUNT` (`0`) with an empty body and `pkt_dev_id = 0`, while the detection events arrive because the server pushes them. Nothing is written: no RGB data, no rescan, no profile or configuration command, one OpenRGB process, no elevation, no second `OpenRGB.exe`.
- **The controller-count reply is parsed per negotiated protocol** (`probe_openrgb_controller_count()` is gone, and with it the "leave the connection unnegotiated so the reply is always 4 bytes" trick). Below protocol 6 the body is the 4-byte count; from protocol 6 up it is the count plus one 4-byte id per controller, and a body that does not match the negotiated shape exactly is rejected instead of being read as its first four bytes. A real protocol-6 server was measured answering 4 controllers with `4 + 4 × 4` bytes.
- **Two situations, treated differently — this is what makes the plateau impossible.** With `launched=True` (this restore started that OpenRGB process, so its detection is still ahead of us) readiness is `DETECTION_COMPLETE` and *nothing else*: no count, and no amount of waiting without the event, is accepted. With `launched=False` (OpenRGB was already running, so `103` may have been sent before we connected) readiness is `103` **or** a positive controller count that has been unchanged *and* free of any detection event for `OPENRGB_READINESS_ALREADY_RUNNING_SECONDS` (12 s — longer than the widest silence between two detection events in the measured cold start, the 10.6 s HID stage, so a count that holds across it cannot be inside a detection stage). The device list is asked for again the moment `103` arrives, so the readiness line names the list detection actually produced rather than a pre-completion reply that could still be one controller short.
- **Backward compatible and still never fatal.** A server too old to have detection events (protocol < 6) cannot confirm anything, so the budget runs out and the restore continues — the timeout is unchanged at 25 s and the warning is now `WARNING: OpenRGB detection readiness could not be confirmed within 25s. Continuing.` An OpenRGB that is already settled does not miss out either: the already-running path accepts it after its quiet window instead of waiting for a completion event that will never arrive on that connection.
- **Every packet read stays bounded and validated.** Connect and read timeouts everywhere; `magic`, packet id, `pkt_dev_id` (where one is meaningful), payload size (capped at `OPENRGB_SDK_MAX_PAYLOAD_SIZE`, faithful to OpenRGB's own `OPENRGB_SDK_MAX_PACKET_SIZE`), short reads and disconnects all end the stream rather than being trusted, a zero timeout can never put the socket into non-blocking mode, packets the gate did not ask for (acknowledgements, device-list updates, a 20 KB controller update) are consumed in full so the stream cannot desynchronise, and a lost connection is reported and retried inside the budget.
- **The sleep binding is fixed.** The thread wrapper now waits through `QThread.msleep` instead of falling back to the module-level `time.sleep` while its own documentation claimed otherwise: the gate polls at 0.5 s and settles for 1.5 s, which `QThread.sleep`'s whole seconds cannot express. A test patches `time.sleep` to raise and asserts the gate never uses it.
- **The detection-progress line is rate-limited.** A real cold start exposed that the gate logged one line per `102` event, and OpenRGB emits one per detector it walks through: **991 of 1 024** restore-log lines of that run were progress lines. A progress line is now published on a percentage change or at most once per `OPENRGB_READINESS_PROGRESS_LOG_SECONDS` (2 s).
- **Verified with a real cold start, which the previous change could not do.** OpenRGB (elevated) and Artemis were stopped by hand — no privilege path was added, and no UAC prompt appeared (`[RESTORE] OpenRGB scheduled task YeelightPCCompanion-OpenRGB started.`, the silent pre-authorised task) — and the normal restore was run from the packaged application: OpenRGB launched at 00:21:54.845, the count sat at **1** from 1.26 s to 11.91 s (OpenRGB's own log: 1 264 / 11 907 / 12 033 / 12 161 ms), `OpenRGB detection completed (13.1s).` at 00:22:07, the only readiness line of the whole run was `OpenRGB SDK ready: 4 controller(s), detection completed (14.7s).` at 00:22:09, and Artemis started at 00:22:09.689 with all four controllers visible in its OpenRGB plugin on that first launch — no plugin restart and no UAC. The plateau that used to be accepted was never accepted.
- **Tests.** `tests/test_openrgb_readiness.py` was rewritten for the new contract (**78 tests**, up from 45) against an in-memory SDK server that decodes the requests independently, replays the measured startup as scripted server pushes and never opens a socket, on a clock that only moves when something waits. It covers the measured plateau regression (which fails against the earlier count-stability rule), the started/progress/completed events, a client connecting while detection runs, a client connecting after detection completed (accepted on the already-running path, refused on the launched one), protocol-6 negotiation and protocol 0/4/99 servers, the per-protocol count-reply shape, malformed events, disconnects, the bounded timeout, cancellation, Artemis strictly after confirmed completion, timeouts still allowing Artemis, no rescan and no second OpenRGB process, and the elevation/task model being untouched. `RestoreFlowStub` in `tests/test_windows_tasks.py` mirrors the wrapper's new signature; the suite is now **492 tests (69 + 83 + 31 + 231 + 78)**. The packaged build was rebuilt and the privacy checks re-run (`dist` contains no `config.json` and none of the maintainer's addresses, coordinates or paths).

### Fixed — the OpenRGB → Artemis restore race (device discovery is not instant)

> **Superseded by the entry above.** The rule described here — a positive controller count that stays identical for three consecutive probes, then a settle — was itself a false positive (the count is unchanged at one for 10.5 s of the measured 12.3 s startup). The measurement quoted below is still correct and is the basis of the replacement; the probe helper, the `OPENRGB_READINESS_STABLE_OBSERVATIONS` constant and the log lines it names are gone. Kept for history.

- **Starting OpenRGB is not the same as OpenRGB being usable, and the restore sequence no longer pretends otherwise.** After requesting the OpenRGB launch it slept a fixed `self.sleep(8)` and then continued, so Artemis and its OpenRGB plugin could connect while OpenRGB was still enumerating controllers. The plugin then showed none — or only some — of the devices, and only a restart of Artemis (or of the plugin) fixed it, which is exactly what a real sleep/wake test showed. Measured from OpenRGB's own log on the maintainer's PC, the SDK server answers requests from **31 ms** after process start while the four controllers only registered between **1 136 ms** and **11 883 ms** and detection completed at **12 267 ms**: roughly 11.8 s of a 12.3 s startup in which an early client sees an incomplete device list. The fixed 8 s wait was therefore a race by construction, and simply making it longer would not have fixed it either.
- **Added a read-only OpenRGB SDK readiness probe (`probe_openrgb_controller_count()`).** It connects to `127.0.0.1:6742` with a bounded timeout, sends exactly one documented packet — `NET_PACKET_ID_REQUEST_CONTROLLER_COUNT` (`0`) with an empty body — validates the reply (`"ORGB"` magic, packet id, device id, 4-byte payload) and returns the le 32-bit controller count, or `None`. It never sends RGB data, a rescan, a profile command or any other mutating request, it always closes its socket, it never raises, and it uses the standard library only. Protocol-version negotiation is deliberately not performed: leaving the connection unnegotiated keeps the controller-count reply exactly 4 bytes on every OpenRGB release (a connection that negotiated protocol 6+ gets extra controller ids appended).
- **Added the readiness gate (`wait_for_openrgb_ready()` / `RestoreEngineThread.wait_for_openrgb_ready()`)** which replaces the fixed sleep: poll the probe every `0.5 s` and require the *same positive* count for `3` consecutive observations, then wait `1.5 s` for the list to settle, with a total budget of `25 s` (≈2× the measured detection time). A count of `0` never counts as ready, and any change — including a decrease — restarts the stability run. The sequence logs real transitions only (`OpenRGB launched. Waiting for SDK device discovery...`, `OpenRGB SDK detected N controller(s).`, `OpenRGB SDK controller count changed: A -> B. Waiting for it to settle...`, `OpenRGB SDK ready: N controller(s), device list stable (Xs).`) instead of one line per poll.
- **Artemis is now gated on readiness, and nothing else.** Artemis starts only after the gate has finished. The gate depends on OpenRGB alone — not on Yeelight discovery, the Connector, Razer Synapse or the solar state — and Razer handling still runs between the two as before. An OpenRGB that is *already running* is gated too, because a live process says nothing about whether its detection finished.
- **Timeout degrades gracefully, cancellation still wins.** On timeout the restore logs `WARNING: OpenRGB SDK device list did not stabilize within 25s. Continuing.` and carries on with Yeelight, Razer and Artemis — a broken OpenRGB can never hold the restore hostage. A readiness timeout is explicitly *not* treated as cancellation (only `RestoreEngineThread.running` decides that), while a suspend during the wait stops it at the next poll (≤ 0.5 s) rather than after the full budget.
- **The elevation model is untouched.** Same `YeelightPCCompanion-OpenRGB` task, same `--gui --startminimized --server` arguments, same one-time approval and protected-path validation, same wake-time zero-UAC behavior, one OpenRGB process — and no `OpenRGB.exe --list-devices` second instance as a readiness mechanism. The probe is an ordinary unprivileged localhost SDK client.
- **Tests.** `tests/test_openrgb_readiness.py` was added (**45 tests**) and `RestoreFlowStub` in `tests/test_windows_tasks.py` now stands in for the gate (the real gate would talk to a live SDK server) while asserting the ordering around it; the suite is now **459 tests (69 + 83 + 31 + 231 + 45)**. The fake SDK server decodes the request independently of the application, so the probe's framing is asserted byte for byte; refusals, a stalled server, a mid-header disconnect, a wrong magic, a wrong packet id, a foreign device id, a short payload and an oversized header are all "not ready" with the socket closed on every path; and the timing cases (`0 → 2 → 4 → 4 → 4`, a single positive observation, growing and declining counts, permanent zero, a never-appearing SDK, malformed replies, the bounded timeout, settling-delay ordering, no per-poll log spam) run on a fake clock that only advances when the code sleeps. No real OpenRGB instance, socket, clock or sleep is used. `tests/test_windows_tasks.py`'s sleep assertion was updated for the removed 8 s wait.
- **Verified on the machine, with one gap.** The probe and the gate were exercised against the live OpenRGB SDK server (8 probes returning 4 controllers in 3–18 ms each; gate ready after 4.2 s wall clock), and the startup timeline above comes from OpenRGB's own log rather than from estimation. The full "kill OpenRGB + Artemis → restore → confirm the plugin lists every controller immediately" run was **not** performed: the running OpenRGB is elevated and cannot be terminated from a non-elevated context (`Access is denied`), so the app's cleanup finds it still running and takes the reuse path. The remaining manual check is recorded in `project_memory.md` §17.

### Fixed — the suspend OFF fan-out and the discovery guard (Stage 4 hardening)

- **The suspend sequence no longer scales with the number of Yeelight devices.** Sending the OFF command used to be `for ip in device_ips: fire_and_forget_off(ip)`, where each `fire_and_forget_off()` could block for its own **0.25 s** inside `socket.create_connection()`. That was acceptable for exactly two fixed devices (`2 × 0.25 s`), but `lights.devices` can hold any number of them: **8** unreachable devices already cost ~2 s and **20** ~5 s in the networking phase alone — before the ~0.35 s Connector release wait, the process termination and the Python overhead — which violates the documented *"complete within roughly 1.5 seconds"* requirement and risks Windows freezing the process mid-sequence. The existing tests did not catch it because they mocked all sockets and time.
- **OFF is now sent by a dedicated suspend-time batch sender, `fire_and_forget_off_devices(device_ips)`.** Every enabled address is attempted **concurrently** — one non-blocking socket per device, one `select()` fan-out (in chunks of `SUSPEND_YEELIGHT_SELECT_CHUNK`, because Windows' `select()` takes only a limited number of sockets per call), and **one** global deadline for the whole batch. It does no discovery, constructs no `yeelight.Bulb`, performs no retries, no response verification and no filesystem work, and needs no reply from the bulb: once connected, the same raw `set_power off sudden` payload as before is sent and the socket is closed. Every device stays independently best-effort, each socket is closed on every path (as it finishes, or when the deadline expires), and the batch never raises — a socket failure cannot stop the suspend sequence from reaching its next step.
- **The network budget is an explicit, documented constant: `SUSPEND_YEELIGHT_NETWORK_BUDGET_SECONDS = 0.35 s`.** It covers *every* connect and *every* send of the whole batch, so `1`, `2` and `20` unreachable devices all cost the same 0.35 s (`20 × 0.25 s = 5 s` before). It is deliberately a fraction of the ~1.5 s target rather than a 1.5-second networking timeout, because the Connector wait and the process termination still need their share. Measured with the real socket stack: 20 unreachable hosts finished in **0.362 s**.
- **No DNS anywhere on this path.** The address family is taken from the validated IP literal itself (`ipaddress.ip_address()`), so IPv4 and IPv6 are both handled without a single `getaddrinfo()` call — verified by patching `getaddrinfo`/`gethostbyname`/`create_connection` to raise and still sending successfully, and against a real local listener for both families. An entry that is not an IP literal cannot be a configured Yeelight address, so it is skipped (with one summary warning) instead of being handed to the resolver.
- **The suspend order is unchanged:** dedupe → mark sleep state → cancel active restore → lightweight runtime config → **kill the Yeelight Chroma Connector first** → ~0.35 s release wait → bounded parallel raw OFF fan-out → kill Artemis/OpenRGB. The Connector still dies before the Yeelight commands, because it holds the music-mode lock on the bulbs. `fire_and_forget_off(ip)` remains as a single-address convenience wrapper over the same bounded sender; nothing calls a serial blocking connect per device any more.
- **A timed-out discovery no longer blocks the GUI for a second full timeout.** `run_discovery()` ran its nested event loop under a guard, but its `finally` block then called `worker.wait(timeout + grace)` **on the GUI thread**: a guard timeout could therefore freeze the UI for another full timeout plus grace period (5.5 s by default), and a worker that was still wedged after that could then be destroyed while still running. The `wait()` is gone: when the guard fires, the progress dialog is closed, a timeout `DiscoveryReport` is returned immediately, and the search is simply abandoned.
- **`QThread.terminate()` is never used and a running `QThread` is never destroyed.** An abandoned worker is retained in the module-level `LIVE_DISCOVERY_WORKERS` list until its own `finished` signal releases it (deletion is posted with `deleteLater()`, never performed inside the worker's own `finished` emission), and its result handler is disconnected so a stale late result cannot reach the UI or overwrite a subsequent discovery. It keeps finishing naturally in the background; only explicit user actions ever create a worker, and a search always ends by itself, so the list stays tiny.
- **The UI guard is clamped exactly like the search.** `run_discovery()` derived its guard from the raw `float(timeout)` while `discover_devices()` clamps to 0.5–30 s, so a caller passing `999` opened a ~1004-second modal guard over a 30-second search. Both now come from the same bounded value (`clamped_discovery_timeout()`, new public wrapper in `yeelight_devices.py`; `discovery_guard_milliseconds()` = that value + `DISCOVERY_UI_GRACE_SECONDS`), and an invalid value falls back to the default instead of raising from the GUI helper.
- **Tests.** `tests/test_runtime_config.py` grew from 15 to 31 tests and `tests/test_device_discovery.py` from 73 to 83 (suite: **414**, 69 + 83 + 31 + 231). The fan-out is tested against a fake socket layer with a fake clock that only advances when the code *waits*, so the timing property is asserted deterministically: 1/2/20 unreachable devices all cost exactly one 0.35 s budget with a single `select()` wait, one dead or refusing peer does not stop the others, every socket is closed on every path, zero devices returns immediately without opening anything, disabled devices are excluded, IPv4/IPv6 pick their family without `getaddrinfo`, the batch runs no discovery and constructs no `Yeelight.Bulb`, and the Connector kill → 0.35 s sleep → fan-out → controller kill order is asserted. The discovery tests cover the prompt-close path, zero results, a network error, a guard timeout returning the timeout report, no synchronous `wait()`/`terminate()`, the still-running worker staying alive and referenced, its cleanup after `finished`, a late result not reaching the UI, and the clamped/invalid/huge/tiny timeout cases. The old behaviour was reproduced to confirm the new tests fail against it: the discovery guard test measured a 5.0 s GUI freeze with the pre-fix `wait()`, and a faithful model of the old suspend loop pays 0.25/0.50/2.00/5.00 s for 1/2/8/20 unreachable devices.
- **Nothing else changed.** No config schema or migration change, no Settings/wizard device-management change, no discovery matching/adoption change, no OpenRGB task, Connector launch/DLL sanitation, restore-sequencing, solar or wake change — this is a hardening patch around the two defects above. The maintainer's real configuration, logs and build output are untouched (and still never committed).

### Added — Yeelight LAN discovery and arbitrary device management

- **The two fixed Yeelight address slots are gone.** `lights.bulb_ip` / `lights.lightstrip_ip` were replaced by a `lights.devices` list of any length: each device has a stable id, a friendly name, an address and its own enable flag. A device can be enabled/disabled, edited and removed independently, and any number of devices is kept — zero included.
- **Added `yeelight_devices.py`**: the device model (stable identity, list operations, duplicate detection) plus LAN discovery. Discovery uses the installed `yeelight` package's supported discovery function (`yeelight.discover_bulbs()`, one SSDP `M-SEARCH` for `wifi_bulb`) — no second protocol stack. It is bounded (5 s), never raises (a firewall or network failure becomes a `DiscoveryReport` with a user-safe message, and **zero devices is a normal outcome**), and normalizes duplicate responses. Only what the SSDP answer already contains is used (device id, address, port, model, firmware, reported name, power) — no follow-up RPC per device.
- **Added stable device identities.** `yeelight:<device-id>` for an id the device reports itself, `manual:<uuid4>` for an identity this application assigns (manual add, or a device migrated from a legacy address slot). Identity is unique in the configuration, stable across reloads, and unaffected by renaming — the display name is never the identity. Because the id is the identity, a device whose address changed is recognized instead of being offered as a second device, and a manually added or migrated device can adopt its real Yeelight id from the discovery results with one explicit action.
- **Added device management to Settings**: a **Yeelight Devices** section with **Discover Devices** and **Add Manually**, one row per device (enable checkbox, name, address, **Edit**, **Remove**) and a factual `X enabled / Y configured` summary. Editing never changes a device's stable id; an edit that would produce a duplicate address is refused with a clear message instead of being saved.
- **Added a discovery flow with a real result dialog**: a modal `Searching your local network for Yeelight devices...` progress dialog, then a list that distinguishes `New`, `IP changed: old -> new`, `Already added - its Yeelight device ID can be linked` and `Already added`. Nothing is added automatically — the user ticks what to add (new rows are pre-checked and can be renamed inline), and an address change updates the existing device rather than creating a duplicate. When nothing answers, the app says so in plain language (`No Yeelight devices were found. Make sure LAN Control is enabled in the Yeelight app and the devices are on the same network.`), including the network error detail when there is one. Windows Firewall is never changed.
- **Added `yeelight_device_ui.py`**: the shared device UI — the discovery `QThread` worker (so the search never blocks the GUI thread; the guard that bounds the wait was only *made* to bound it correctly by the hardening entry above), the results dialog, the add/edit dialog and the reusable device list widget used by both Settings and the first-run wizard.
- **Added `tests/test_device_discovery.py`** (73 tests) covering the identity scheme, the device-list operations (add/edit/remove/duplicates), the v2 schema and its validation, the runtime projection, the complete `v1 -> v2` migration matrix, discovery normalization (multiple devices, duplicates, missing fields, `Location` fallback), matching (new / already added / IP changed / id adoption) and every failure path (zero results, network error, missing library, bounded timeout), the discovery worker, the wizard's device page and the shipped example template.

### Changed — configuration schema is now version 2

- **`config_version` is 2.** `migrate_config()` now runs `v0 -> v1 -> v2` in order and still preserves every unknown key. A configuration with a newer version is still refused instead of being guessed at.
- **`v1 -> v2` migration preserves every configured device.** `bulb_ip` becomes a device named *Yeelight Bulb* and `lightstrip_ip` one named *Yeelight Lightstrip* (both enabled, each with a durable generated id); a blank slot produces nothing, and the same address in both slots produces **one** device. The legacy keys are removed, so the device list is the only place a Yeelight address lives — there is no second runtime model. The migration runs through the canonical `migrate_config()` path, is backed up as `config.json.bak` and is written automatically at startup (`[CONFIG] Migrated configuration v1 -> v2`). Nothing else in the configuration is touched: location, integrations, automation, paths and the OpenRGB setup are preserved exactly.
- **Device validation is centralized and never silently repairs.** Every entry must be a JSON object with a valid id, a non-empty name (≤ 80 characters, no control characters), a valid address (the project's existing address validation, now required for a configured device) and a boolean `enabled`; ids must be unique and normalized addresses must be unique; optional `model`/`firmware` metadata is accepted but never required. A device list where nothing is enabled is a warning, not an error — and a present-but-malformed device entry or device list is rejected, so an invalid import still changes nothing.
- **`config.example.json` is a v2 template** (`"lights": { "devices": [] }`) with no address, no device id and no real data; a unit test asserts it stays that way.
- **`package`/`runtime` behaviour is unchanged for integrations.** The Yeelight Chroma Connector never sees the device list: its launch, working directory, DLL/PATH sanitation, the suspend order and every restore wait are exactly as before.

### Changed — every fixed two-device assumption is gone

- Suspend, wake, daytime shutdown, solar self-healing and the restore sequence now iterate **all enabled configured devices**, each one isolated: a device that is offline is logged and skipped, never aborting the remaining devices. Zero devices is a fully supported state — RGB integrations, suspend, restore, the solar engine and self-healing all keep working with an empty target list.
- The suspend path reads its device targets from the new lightweight projection `config_manager.enabled_device_ips(config)`: addresses only, straight out of the already-loaded configuration. No discovery, no `yeelight.Bulb` construction, no DNS probing, no filesystem checks, no validation and no UI work were added to the suspend contract.
  - **Superseded — see *Fixed — the suspend OFF fan-out and the discovery guard* at the top of this section.** This bullet originally also claimed the suspend path's *timing* was unchanged. It was not: the per-device `fire_and_forget_off()` loop that consumed this projection was `O(N × 0.25 s)`, so an arbitrary device list broke the ~1.5 s bound. The fan-out has since been replaced by a globally bounded batch sender.
- The first-run wizard's Yeelight page is now an optional device section with the same **Discover Devices** / **Add Manually** flow: a user can finish setup with zero, one or many devices and add more later. The review page reports the real count.
- The Dashboard's factual summary is `Yeelight devices: N enabled / M configured` instead of implying exactly two lights, and the two automation checkboxes now say *the Yeelight devices* rather than *Room Bulbs*.
- `tests/test_runtime_config.py` and `tests/test_windows_tasks.py` were updated to the v2 schema, and the multi-device paths gained dedicated tests: the suspend callback iterating enabled devices (and never running discovery), per-device failure isolation in the suspend, restore and daytime paths, zero devices not failing a restore, daytime/nighttime restores hitting every enabled device, and the discovery/restore paths never reaching each other.

### Notes on this change

- Nothing in the OpenRGB elevation model, the connector integration, the child-process DLL/PATH sanitation or the restore ordering/timing was touched: those tests pass unchanged. The existing `YeelightPCCompanion` logon task and the `YeelightPCCompanion-OpenRGB` task are untouched.
- Discovery is **user-triggered only**: it never runs at startup, never on a timer, never repeatedly in the background and never during suspend.
- The maintainer's real configuration was migrated locally (`config_version` 2, previous file kept as `config.json.bak`) and never committed.
- Real-device verification: a read-only discovery run on the maintainer's LAN found both configured devices with their stable Yeelight ids and addresses reported over SSDP, matching the existing configured addresses. No device state was changed.
- **Privacy fix found while working on this change:** the pre-existing test fixture in `tests/test_config_manager.py` used the maintainer's *real* device addresses as its "legacy v1" example. Those values are now RFC 5737 documentation addresses (`192.0.2.26` / `192.0.2.27`), which can never be a real LAN device. Test behaviour is unchanged.

### Fixed — Yeelight Chroma Connector no longer resolves its runtime DLLs from the packaged app

- **An installed external program started by this application no longer inherits the frozen build's DLL search environment.** A running **Yeelight Chroma Connector** had loaded `dist\YeelightPCCompanion\_internal\VCRUNTIME140.dll` and `VCRUNTIME140_1.dll`, which kept the PyInstaller output locked and made the next `COLLECT` fail with `PermissionError: [WinError 5]`. The connector's executable never lived in `dist` (it is installed separately, e.g. `C:\Program Files\Yeelight\Yeelight Chroma Connector\`) — only its DLL resolution did.
- **Two causes were measured, and both are now fixed.** (1) The connector inherited this application's **working directory**, and (2) — the one that actually kept the lock alive — it inherited the frozen build's **PATH**, which PyInstaller populates with `dist\YeelightPCCompanion\_internal` and the runtime-hook descendants such as `_internal\PyQt6\Qt6\bin` and `_internal\pywin32_system32`. A live measurement of a running connector launched by the packaged app confirmed its `PATH` contained those three bundle entries even after the working directory had been corrected, so the working-directory fix alone was **not** sufficient.
- **The fix is the documented PyInstaller external-process guidance, applied at the one gate that starts installed third-party programs** (`RestoreEngineThread.launch_process`):
  - the process-wide DLL search directory is reset with `SetDllDirectoryW(NULL)` for exactly the duration of the spawn and then restored in a `finally`, using the value read back with `GetDllDirectoryW()` rather than assuming `sys._MEIPASS`;
  - the child receives a **copy** of the environment whose `PATH` has the bundle directory and its descendants removed — path-component comparison (`abspath`/`normcase`/`commonpath`), never substring matching, so a sibling like `_internal_backup` survives;
  - because `SetDllDirectoryW` is process-global and this application is multithreaded, the reset/spawn/restore window is serialised by `_LAUNCH_LOCK`.
- **The Yeelight Chroma Connector additionally still starts in its own executable's folder** (`cwd=os.path.dirname(connector_path)`), so it has both a correct working directory and a clean search environment.
- **Nothing is hidden and nothing is left changed:** the bundled DLLs stay in `dist`, `os.environ` is never mutated, `SetDllDirectoryW` is never left cleared, no DLL is copied into the connector folder, and the spawned program itself is untouched.
- **Deliberately narrow, and consistent for the integrations that need it.** The gate covers the Yeelight Chroma Connector, Razer Synapse, Artemis and the direct OpenRGB child launch (an already-elevated app). Subprocesses belonging to this application itself — `taskkill` and the elevated OpenRGB provisioning helper — do not route through `launch_process` and are unaffected, and the OpenRGB scheduled task is already isolated by Task Scheduler. Razer and Artemis keep the inherited working directory: only the environment is sanitised for them.
- **The restore sequence is unchanged.** Suspend/resume detection, process termination order, restore order, every wait, the solar logic, the Yeelight power commands and the integration enable flags are all exactly as before: cleanup → wait → OpenRGB → solar state → Yeelight actions → connector when appropriate → Razer → Artemis.
- **Tests:** `tests/test_windows_tasks.py` grew from 186 to 224 tests (302 total across the suite) covering the child environment (bundle entries removed, unrelated and similarly-named entries kept, no `PATH` invented, source runs unchanged), the DLL-directory lifecycle (cleared before the spawn, restored afterwards, restored even when the spawn raises, previous value read back rather than assumed), the lock-protected spawn window, and the per-integration behaviour (connector keeps its own `cwd` and is sanitised, direct OpenRGB keeps its own `cwd`, application-internal subprocesses untouched). The new tests were confirmed to fail without the fix. No third-party application is launched during the suite.
- **Verified live on Windows**, not only in unit tests: with the fix, the connector started through the real restore sequence (a) received its own installation folder as `cwd` in the kernel's view, (b) was created while the DLL directory was cleared, (c) received a `PATH` with every bundle entry removed, and (d) still received all unrelated `PATH` entries and environment variables; the DLL directory was restored afterwards and `os.environ` was untouched. Reverting the gate made the same check fail, with the bundle entries present again.

### Added — seamless OpenRGB elevation (zero-UAC wake)

- **Added `windows_tasks.py`**, a standard-library-only Windows scheduled-task elevation broker for OpenRGB. It owns process-elevation detection (`GetTokenInformation`/`TokenElevation`, with an `IsUserAnAdmin` fallback), the fixed task definition and its Task Scheduler XML, `schtasks.exe` invocation, the task lifecycle (`provision` / `remove` / `run` / inspect) and the configuration→task lifecycle policy. It is testable without a GUI and without the real Task Scheduler.
- **Added the `YeelightPCCompanion-OpenRGB` scheduled task**: run level `HighestAvailable`, logon type `InteractiveToken`, action = the configured OpenRGB executable with the fixed arguments `--gui --startminimized --server`, and **no triggers at all** — it never runs on a schedule and never at logon, only when the application asks for it.
- **Added a seamless elevated launch decision** to the restore sequence: when the application is already elevated, OpenRGB is started directly as a child process; otherwise the pre-approved task is started with `schtasks /run`, which is silent. Waking the PC can no longer show a UAC prompt.
- **Added one-time provisioning from explicit user actions**: the first-run wizard (after the configuration has been saved) and the Settings tab (when OpenRGB is enabled or its executable path changes) request the single administrator approval that creates the task. A dedicated **Set Up Seamless OpenRGB Launch** / **Repair OpenRGB Elevation** button covers existing installations, declined approvals and stale tasks.
- **Added a compact OpenRGB elevation indicator to Settings** — `Seamless elevated launch: Ready` / `Needs setup` / `Needs repair` / `Task points to a different OpenRGB path` / `Not used` — plus the setup/repair button. The Settings layout is otherwise unchanged.
- **Added two narrow elevated provisioning modes**, `--provision-openrgb-task <path>` and `--remove-openrgb-task`. They run before the Qt application, tray and automation exist, do exactly one thing, and are the project's only `"runas"` call site. There is no generic command/argument/task-name interface: the task name and OpenRGB arguments are hardcoded and only the executable path varies.
- **Added `tests/test_windows_tasks.py`** (75 `unittest` tests) covering elevation detection, the launch decision, the restore sequence continuing when the task is missing, the task definition, the task principal, paths with spaces and non-ASCII characters, stale-task reporting, the configuration-driven lifecycle, provisioning failures not damaging the configuration, and the absence of any interactive elevation call in the automatic paths. Every Task Scheduler interaction is mocked: the suite never creates a real task, never elevates and never triggers a power event.

### Changed — seamless OpenRGB elevation

- `RestoreEngineThread.launch_process_elevated()` (which called `ShellExecuteW(..., "runas", ...)` on every wake) is **gone**, replaced by `RestoreEngineThread.launch_openrgb()`. The automatic restore path no longer contains any elevation request.
- When the elevation task is missing, disabled, stale or fails to start, the wake path now logs a warning, emits `WARNING: OpenRGB seamless elevation is not configured. Skipping OpenRGB for this restore. Open Settings to repair.` and continues with Yeelight, Razer and Artemis. An absent OpenRGB launch never aborts the restore sequence.
- The task is created from a full Task Scheduler XML definition via `schtasks /create /xml` rather than a `/tr` command string: that is what makes "no triggers" expressible, and it keeps the executable path and the arguments in separate elements so a path containing spaces can never be split incorrectly. `schtasks.exe` is invoked with an argument list (never `shell=True`) and resolved from `%SystemRoot%\System32`, never through `PATH`.
- A task is only reported as `Ready` when it actually matches the saved configuration (enabled, `HighestAvailable`, exact OpenRGB path, exact arguments). A task that merely has the right name is reported as needing repair instead of being trusted.
- Disabling the OpenRGB integration attempts to remove the task; if removal fails, the integration still disables and a warning is logged rather than blocking the change.
- Startup never repairs the elevation state on its own: for an existing installation the app detects that provisioning is required, marks it as `Needs setup` in Settings and in the log, and waits for the user to approve it. No UAC prompt appears at boot.
- The `YeelightPCCompanion` logon task registered by `register_startup.bat` is unchanged and remains a separate task.

### Notes on the seamless elevation change

- Nothing here disables UAC, modifies consent policy, edits the registry to suppress prompts, exploits auto-elevated binaries or uses any other bypass. The only privilege escalation is a normal, user-approved scheduled-task definition.
- The restore ordering and timing are unchanged (kill → 3 s → OpenRGB → 8 s → solar decision → Yeelight → Razer → Artemis); only the OpenRGB launch mechanism was replaced.
- The task definition was verified against real Task Scheduler behaviour on Windows: the generated XML (including `<Triggers />`) is accepted by `schtasks /create /xml`, `schtasks /query /xml` output round-trips through the parser, `RunLevel: HighestAvailable` is exported for a highest-privilege task, and absent `<Enabled>` correctly means "enabled". A real sleep/wake cycle is left to the maintainer.

### Hardened — OpenRGB elevation task (working directory + protected target)

- **The elevation task now pins OpenRGB's working directory.** The task action carries an explicit `<WorkingDirectory>` of `dirname(OpenRGB.exe)` instead of leaving the folder to Windows (whose default for a task action is not the executable's folder). `Command`, `Arguments` and `WorkingDirectory` remain three separate XML fields — no shell command is ever assembled. A queried task is only `Ready` when its working directory equals `dirname(configured executable)`; a missing or different one is reported as needing repair.
- **The direct launch (already-elevated app) pins the working directory too:** `RestoreEngineThread.launch_openrgb()` starts OpenRGB with `cwd=os.path.dirname(openrgb_path)`, so the elevated child never inherits Yeelight PC Companion's own working directory. The fixed arguments are unchanged, and no other child process is affected.
- **A privileged task is only created for an executable the current user cannot replace.** The provisioning CLI used to require only "absolute path, existing `.exe`"; it now also proves the target is protected, because an approved `HighestAvailable` task is a standing promise to run that path elevated. If OpenRGB can be modified by the normal user account (Downloads, Desktop, Documents, AppData, Temp, a user-owned tools folder, …), seamless elevated launch is refused with a plain-language explanation and the direction to move OpenRGB to a protected location such as Program Files. Nothing is moved, no ownership is taken and no permissions are changed: the user decides where OpenRGB lives.
- **The check is an effective-rights check, not a path test and not `os.access()`.** `windows_tasks.is_elevation_target_secure()` retrieves the security descriptor with `GetNamedSecurityInfoW()` and evaluates the DACL against the user's **unelevated** access token with `AccessCheck()` (inside the elevated helper: the linked limited token). It looks for write data/add file, append, write attributes, delete, delete child, write DAC, write owner and full control on the executable, on the folder containing it and on every parent folder, and treats a target owned by the current user as replaceable even when the DACL looks read-only. It fails closed: an unreadable descriptor, an unavailable token or a failed access check is "could not be verified", which refuses provisioning rather than trusting the target.
- **An existing task in an unsafe location is never reported `Ready`.** Settings shows `Unsafe OpenRGB location` (or `OpenRGB location could not be verified`) with the reason and the repair button; the wizard and the Settings repair button explain the situation instead of asking for a UAC approval that would be refused; and the automatic wake path logs a warning, emits `OpenRGB skipped for this restore` and continues with Yeelight, Razer and Artemis. The automatic path still never calls `"runas"`, never shows UAC and never provisions or repairs anything.
- **Task identity is validated further:** besides enabled/`HighestAvailable`/path/arguments, a `Ready` task must carry the expected working directory, must not state a different `LogonType` or a different `UserId`, and must have **no triggers** at all. Values Task Scheduler omits because they equal its own defaults are read as "not stated" instead of as a mismatch. Only `YeelightPCCompanion-OpenRGB` is ever queried, created or removed.
- **A refusal never touches the configuration:** `config.json` keeps the OpenRGB path, the integration stays enabled and OpenRGB can still be started manually; only the seamless elevated launch stays unconfigured until the user moves OpenRGB.
- **Tests:** `tests/test_windows_tasks.py` grew from 75 to 125 tests, covering the working directory (spaces, non-ASCII, round-trip through the query parser), the direct-launch `cwd`, the extended identity validation, user-writable executables and folders, protected locations, fail-closed behaviour when the security inspection cannot complete, refusal before any approval prompt, and the wake path skipping OpenRGB for an unsafe location. No test creates a privileged task, shows UAC or triggers a power event; the ACL checks that run for real only read security descriptors.

### Fixed — OpenRGB provisioning now reports the real failure (and creates the task)

- **The elevated provisioning helper failed for every target, and the parent could not say why.** Inside the helper, `_open_standard_user_token()` demanded `SecurityImpersonation` when duplicating the UAC linked token. That token is an **identification-level** token and `DuplicateToken()` refuses to raise a level, so the call failed with `ERROR_BAD_IMPERSONATION_LEVEL` (1346). The protected-location check therefore answered "could not be verified" *only* in the elevated branch — which fails closed, so provisioning refused every OpenRGB path, the helper exited with a failure and no task was ever created. The unelevated check that runs first passed, which is why the UAC prompt appeared and the request still went nowhere. The helper now tries `SecurityImpersonation` and falls back to the level the linked token actually carries (`SecurityIdentification`, the same identity — SID/groups — that the access decision is made from, and this check only ever queries). Verified with a real elevated helper run: the target is now correctly accepted and `YeelightPCCompanion-OpenRGB` is created.
- **The parent no longer guesses how provisioning ended.** `ShellExecuteW` returns only "elevation was launched": no process handle, no exit code, so a helper that started and then failed was indistinguishable from one that was still working, and the Settings UI waited out a ~25 s poll before reporting the vague `OpenRGB seamless elevated launch is not ready yet (Needs setup)`. The launcher is now a single `ShellExecuteExW(..., "runas", ...)` call with `SEE_MASK_NOCLOSEPROCESS`, and the parent **waits for the helper's own process**, reads its exit code with `GetExitCodeProcess()` and only then inspects the task. The process handle is closed exactly once on every path, including the timeout and the wait-failed paths.
- **Blind task polling is gone as the primary completion mechanism.** It could not distinguish "still working" from "already failed", which is exactly what hid this bug. A ~30 s timeout remains only as a guard so a broken elevated process cannot hang the Settings UI; on timeout the helper is given a short grace period and then terminated (it only ever creates or deletes one fixed task), the handle is closed, and the timeout is reported as a timeout — never as `Needs setup`.
- **A failure now carries its own reason.** The helper writes `%LOCALAPPDATA%\Yeelight PC Companion\openrgb_provision_result.json` — exactly `success`, `exit_code` and one short user-safe `message` — which the parent displays verbatim. Stale results are deleted before every attempt and additionally rejected by modification time, the file is removed after the parent reads it, and an unwritable/unreadable location only degrades the message (never the behaviour, and never crashes either process). This channel is diagnostics only: it is never read to decide which privileged command to run, and it exposes no command, task-name or path interface.
- **Distinct exit codes** for the narrow provisioning CLI: `0` success, `1` other failure, `2` malformed command line, `3` protected-location check refused or could not verify, `4` `schtasks.exe` refused the task definition, `5` task created but not usable. A child that claims success while the task is not `Ready` is reported as that distinct bug (`Provisioning helper completed, but the task is not ready: <specific status>`), not as a generic failure.
- **No security check was weakened to make this pass.** The protected-target design, the linked-token requirement, the working-directory/principal/no-trigger validation and the fail-closed behaviour are all unchanged; only the impersonation level the fallback uses changed to one Windows accepts for that token.
- **Minimal provisioning diagnostics**: a bounded best-effort `%LOCALAPPDATA%\Yeelight PC Companion\openrgb_provisioning.log` records the action, the helper's exit code, the OpenRGB executable basename and task state — never configuration contents, coordinates, device addresses or unrelated local paths.
- **Tests:** `tests/test_windows_tasks.py` grew from 125 to 186 tests (277 total across the suite) covering the `ShellExecuteExW` wrapper (callback style, quoted arguments, handle returned, declined UAC, launch failure, raising shell), the documented `SHELLEXECUTEINFOW` layout, the wait/exit-code/handle-close lifecycle (including timeout and termination), the diagnostic result file (round trip, stale rejection, malformed JSON, unusable location, narrow field set), the exit-code classes, every failure-message path, the linked-token level fallback, and the guarantee that the automatic wake/suspend paths still contain no interactive elevation or provisioning call. No test shows UAC or creates a task.

### Added — configuration & first-run foundation

- **Added `config_manager.py`**, a single configuration layer owning the canonical defaults/schema, storage-location rules, validation, version migration, atomic writes, backups, legacy discovery and import/export. Standard library only, so it stays testable without a GUI or Windows power behavior.
- **Added configuration versioning**: `config_version` at the top level (current version `1`). A configuration without `config_version` is treated as legacy **version 0** and is migrated `v0 → v1` automatically, without requiring the user to edit anything and without discarding unknown keys.
- **Added per-integration enable flags**: `integrations.<key>.enabled` for `openrgb`, `yeelight_connector`, `razer_synapse` and `artemis`. Migration infers enablement from the already-configured executable paths (non-empty path → enabled, blank/absent → disabled), so an existing working setup behaves exactly as before.
- **Added a first-run setup wizard** (`first_run_wizard.py`, PyQt6 `QWizard`): Welcome (with *Import existing configuration*), Location, Yeelight devices, Integrations, Automation, and Review/Finish. It validates as it goes, explains problems in plain language, and writes the configuration atomically on Finish. Cancelling writes nothing — no partial configuration and no placeholder automation.
- **Added the correct storage location for packaged builds**: `%LOCALAPPDATA%\Yeelight PC Companion\` for `config.json`, the debug log, `crash.log` and backups. Source runs keep using the repository directory, and an opt-in **portable mode** (an empty `portable.flag` beside the executable) stores everything in the application directory. The data directory is created automatically, and the path comes from Windows/environment APIs — never a hardcoded username.
- **Added automatic legacy-configuration adoption on first packaged launch**: a configuration beside the executable, or (safely recognized) the development layout `<repo>\config.json` next to `dist\YeelightPCCompanion\`, is loaded, migrated and written to the new location. The original file is never modified or deleted, and only file names are logged — never configuration values.
- **Added Settings UI coverage for every supported setting**: location, Yeelight addresses, an enable checkbox + executable path + native **Browse** dialog per integration, automation toggles, plus **Save**, **Import Configuration** and **Export Configuration**.
- **Added centralized configuration validation** used by the wizard, the Settings UI, import and load: structure, version, latitude/longitude ranges, elevation, light buffer hours, device IP literals, booleans, the Razer timeout range, path types and the integration structures. Errors are user-facing strings; temporary third-party uninstallation is a warning, not a rejection.
- **Added automated tests** (`tests/test_config_manager.py` and `tests/test_runtime_config.py`, `unittest`, 78 tests) for defaults, `v0 → v1` migration, integration inference, validation rules, save/load round trips, atomic replacement, backups, import/export, storage-location rules, the lightweight runtime loader, malformed-section rejection and executable file-vs-directory checks.
- **Added documentation** for the new configuration architecture, storage rules, portable mode, migration, wizard, import/export and backups in `project_memory.md` (§8, §8b, §12, §13, §17).
- **Added `ConfigManager.load_runtime()`**, the lightweight read path for timing-critical/read-only consumers (JSON read + migration/normalization only, no validation, no writes, no backups, no import/export logic).

### Changed — configuration & first-run foundation

- `config.example.json` is now a genuinely safe fresh template: `config_version`, `integrations`, blank paths and no placeholder IPs. It is asserted equal to the `ConfigManager` defaults by a unit test.
- Configuration saving is **atomic**: write temp file → flush/fsync → verify → `os.replace()`. A single latest `config.json.bak` is kept before destructive replacement (import/migration) instead of accumulating timestamped backups.
- `--tray` no longer hides a required first-run setup: when no usable configuration exists the wizard is shown, and normal tray behavior resumes after setup succeeds.
- Settings changes, imports and exports are validated and explained with friendly messages instead of surfacing Python errors, and their Qt slots are guarded so an unexpected failure cannot abort the process.
- Disabled integrations are now skipped completely: nothing is killed, launched, waited for, retried or used for self-healing for a disabled integration, and Artemis JSON-module data is not published when Artemis is disabled.
- Blank/unconfigured Yeelight addresses are skipped safely everywhere (no socket is opened to an empty address), so a user may configure only a bulb, only a lightstrip, both, or neither.
- An enabled integration whose executable is missing from the machine is skipped with a warning and the rest of the restore sequence continues, instead of aborting the whole sequence.
- `.gitignore` now also covers `config.json.bak`, the atomic-write temp files (`.config-*.tmp`), the default exported configuration name, and the machine-local `portable.flag`.
- Log and crash-report location follows the configuration location, and only file names/storage modes are logged — no coordinates, device addresses or full local paths.

### Fixed — configuration hardening (post-review pass)

An independent review of the new configuration layer found four concrete problems. All four are fixed here; no product feature, UI work, discovery work or orchestration redesign is involved.

- **The suspend callback no longer performs full validation.** `_execute_suspend_actions()` now reads configuration through the new lightweight `ConfigManager.load_runtime()` (JSON read + migration/normalization only) instead of `load()`. Full validation probes the external environment — `os.path.exists()`/`os.path.isfile()` on every enabled integration's executable — which does not belong in a callback that must finish in roughly 1.5 s. The runtime loader performs no validation, no filesystem probing of executables, no writes, no backups and no import/export logic. If that read fails, the configuration already loaded in memory is used, exactly as before. The previously incorrect "no extra I/O is added" comment was replaced.
- **A failed runtime reload keeps the last known-good configuration.** `YeelightPCCompanionWindow.load_config()` used to replace the in-memory configuration with defaults when a reload failed (resume, solar reconciliation, Settings). That could silently disable integrations and blank Yeelight device addresses. Defaults are now used only when no valid configuration has ever been loaded, so the UI can still render while the user fixes the file.
- **Malformed known sections are rejected instead of silently repaired.** `normalize_config()`/`migrate_config()` previously replaced a present-but-malformed known section (`"location": "garbage"`, `"lights": []`, `"paths": "bad"`, `"automation": []`, `"integrations": "bad"`, or a non-object `integrations.<key>` entry) with valid defaults *before* validation ran. Missing sections are still defaulted (so valid legacy configurations keep migrating), but present malformed ones are now preserved so centralized validation reports a clear error. This restores the guarantee that an invalid imported file does not replace the current valid configuration — such an import now changes nothing. Unknown keys are still preserved.
- **Integration executable availability uses file checks.** Validation warnings, `RestoreEngineThread.integration_path_available()` and the launch helpers now use `os.path.isfile()` instead of `os.path.exists()`, so an existing directory is no longer accepted as an executable (previously it passed validation and only failed later at launch). No PE-header or extension check is performed — a normal existing file is enough.
- Regression tests were added for all four findings: the runtime loader never touches `os.path` for executables while the strict path still does, a failed reload preserves the previous valid configuration, each malformed section is rejected (and its import leaves the active configuration untouched), and a directory is not a usable executable. The runtime/suspend boundary is exercised through stub objects — **no PC is suspended and no power event is triggered** in tests.

### Notes on this change

- The proven sleep/wake/shutdown orchestration was **not** rewritten: suspend/resume detection, the suspend timing contract, the raw LAN power-off, restore ordering, thread architecture, solar calculations and self-healing all keep their existing order and timing. Only configuration-related guards (enable flags, blank addresses, missing executables) were added.
- For an existing fully populated configuration, runtime behavior is equivalent to the previous version. The maintainer's local configuration was migrated automatically (`config_version` 1 + integrations) with the original preserved as `config.json.bak`; it was never committed.
- The two long-standing configuration mismatches (`turn_on_yeelight_on_wake_night`, `force_silent_launch`) are still **not** implemented. They are preserved, documented, and are no longer presented as reliably functional controls.
- Yeelight LAN discovery and arbitrary-length device lists are deliberately **not** part of this change; the schema still has exactly two optional device slots.

### Planned / next

- Add a public-facing `README.md` with features, requirements, setup, configuration, screenshots, usage, and troubleshooting.
- Add an explicit open-source license selected by the maintainer.
- Resolve `turn_on_yeelight_on_wake_night` and `force_silent_launch` in a dedicated task.
- Add Yeelight LAN discovery and support for more than two devices.
- Consider a visual redesign of the dashboard/settings window, an installer, update checking, and regenerated icon artwork.

### Added — project documentation baseline

- Added `project_memory.md` as the canonical project context for coding agents and maintainers, including architecture, configuration, power-event constraints, build/release cautions, file map, privacy rules, known issues, and verification expectations.
- Added this changelog so future development has a single running history before the repository is made public.

### Changed — rename & packaging foundation

- **Renamed the application and project files** from the legacy `Lumina` naming to **Yeelight PC Companion** (using `git mv`, so history is preserved):
  - `lumina_app.py` → `yeelight_pc_companion.py`
  - `lumina.spec` → `yeelight_pc_companion.spec`
  - `lumina.ico` → `yeelight_pc_companion.ico` (file renamed only; icon artwork unchanged)
  - `build_lumina.bat` → `build_yeelight_pc_companion.bat`
  - `run_lumina.bat` → `run_yeelight_pc_companion.bat`
  - `register_startup.bat` keeps its already-generic filename.
- **Rebranded user-visible and runtime branding**: main window title, UI header, tray tooltip, tray "Exit" item, startup/shutdown log messages, the hidden shutdown-receiver window class/title, the main window class name (`LuminaWindow` → `YeelightPCCompanionWindow`), and the crash log header.
- **Changed the Windows `AppUserModelID`** from `SlonInc.Lumina.LightOrchestrator` to `YeelightPCCompanion.App`.
- **Renamed the generated rotating debug log** from `lumina_debug.log` to `yeelight_pc_companion_debug.log`.
- **Updated Windows version metadata** (`version_info.txt`): product name/description `Yeelight PC Companion`, internal/original names `YeelightPCCompanion`, and company `Yeelight PC Companion Contributors` — removing `Lumina` / `Slon Inc`. Version number is unchanged (`1.0.0`).
- **Renamed the PyInstaller output** to `YeelightPCCompanion.exe` / `dist\YeelightPCCompanion`.
- **Renamed the scheduled startup task** to `YeelightPCCompanion` and made `register_startup.bat` migration-aware: it removes the known legacy `LuminaLightOrchestrator` task if it still exists, is safe to run repeatedly, and does not fail when the legacy task is absent.
- **Packaging is now privacy-safe**: `yeelight_pc_companion.spec` no longer references `config.json` and instead bundles the placeholder-only `config.example.json`; `build_yeelight_pc_companion.bat` no longer copies the developer's `config.json` into `dist` and now aborts if a `config.json` is ever found in the build output.
- **Expanded `.gitignore`** to cover the new `yeelight_pc_companion_debug.log` (plus rotations) and `crash.log` (plus rotations), while keeping the legacy `lumina_debug.log` rules so upgrading users remain protected.

### Notes on this change

- Existing `config.json` files remain compatible: the rename deliberately does **not** rename any configuration key, and the maintainer's local `config.json` was neither modified nor committed.
- No change was made to sleep/wake/light-control behavior, the suspend timing contract, power detection, or the restore sequence.

### Planned / next (rename & packaging change)

- Add a public-facing `README.md` with features, requirements, setup, configuration, screenshots, usage, and troubleshooting.
- Add an explicit open-source license selected by the maintainer.
- ~~Add friendly first-run configuration handling when `config.json` does not yet exist (a build no longer ships a config).~~ **Done** — see the configuration & first-run foundation section above.
- Review configuration options that are currently exposed but not fully consumed by runtime logic, including `turn_on_yeelight_on_wake_night` and `force_silent_launch`. — **still open**, see the notes above.
- ~~Add practical validation/build checks without weakening or destabilizing the Windows suspend/wake behavior.~~ **Partly done** — centralized configuration validation plus a `unittest` suite for the pure configuration logic.
- Consider regenerating the icon artwork for the new brand (the rename only renamed the existing icon file).

## Repository baseline — 2026-09-17

Initial private baseline: the existing Windows application was imported into a
private GitHub repository on this date, before the first public release baseline.

### Added

- Existing Windows PyQt6 tray application imported into `BobanAliBrz/Yeelight-PC-Companion`.
- PyQt6 dashboard/settings/log UI and system-tray integration.
- Local solar-state calculation using `ephem` with configurable latitude, longitude, elevation, and sunrise/sunset buffer.
- Yeelight LAN control for a configured bulb and lightstrip.
- OpenRGB, Yeelight Chroma Connector, Razer Synapse, and Artemis orchestration during wake/startup and sleep.
- Artemis local JSON-module publishing for solar state.
- Primary Windows suspend/resume handling through `PowerRegisterSuspendResumeNotification`.
- Secondary Qt native power-event filtering.
- Hidden Win32 window for shutdown/logoff session messages.
- Fast suspend path designed to kill Yeelight music-mode ownership and send raw LAN power-off commands before Windows freezes user processes.
- Asynchronous restoration engine for wake/startup sequencing.
- Periodic solar-state reconciliation/self-healing.
- Service-status monitoring and power-detection watchdog logging.
- Rotating file logging for diagnostics.
- PyInstaller packaging files and Windows batch helpers for build, run-to-tray, and elevated startup registration.
- Safe `config.example.json` template.
- `.gitignore` rules excluding real `config.json`, runtime debug logs, build output, Python caches, and local agent/editor metadata.

### Security / privacy baseline

- Real `config.json` was deliberately excluded from Git history because it contains geographic coordinates, device IPs, and machine-specific executable paths.
- Runtime debug logs were deliberately excluded because they can contain local network information.
- `build/` and `dist/` output were excluded from source history.

### Known baseline limitations

- Fresh clones do not yet have a first-run config flow; the app expects `config.json` to exist.
- Packaging still references/copies the developer's real `config.json`, so the current local build flow must **not** be used for a public binary release until corrected.
- The application and helper files are still largely branded `Lumina`, while the intended public project name is **Yeelight PC Companion**.
- No `README.md` or open-source `LICENSE` existed in the initial import.
- No established automated test suite existed in the initial import.

> The branding, packaging-safety, and log-ignore limitations recorded in this dated baseline section have since been addressed — see **[Unreleased]** above.
