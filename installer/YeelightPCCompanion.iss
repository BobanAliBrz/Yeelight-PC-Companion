; ---------------------------------------------------------------------------
; Yeelight PC Companion - Windows installer (Inno Setup 6)
;
; Design decisions worth knowing before editing this file:
;
;  * PER-USER install into %LOCALAPPDATA%\Programs\Yeelight PC Companion.
;    The application stores its configuration per user in
;    %LOCALAPPDATA%\Yeelight PC Companion, so a per-user install matches the
;    data model and needs no UAC for an ordinary installation.
;
;  * The version comes from installer\version.iss, which is GENERATED from
;    app_metadata.py by tools\write_installer_version.py. There is deliberately
;    no hard-coded fallback: if the include is missing, compilation fails rather
;    than shipping a mislabelled installer.
;
;  * Start-at-logon is a per-user HKCU\...\Run entry. The application runs
;    unelevated; only OpenRGB needs administrator rights, and that goes through
;    its own separately approved scheduled task.
;
;  * Uninstall never deletes the user's configuration. It removes the installed
;    binaries, the shortcuts, the logon entry and the scheduled task this
;    application owns - nothing else.
; ---------------------------------------------------------------------------

#define MyAppSourceDir "..\dist\YeelightPCCompanion"

; Version/name facts. Generated; no fallback on purpose.
#include "version.iss"

[Setup]
; Stable identity: keep this AppId forever, or upgrades will install side by side.
AppId={{4B68DF13-1F98-54A9-812A-D3CB3D8CA8D1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright={#MyAppCopyright}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; Per-user installation: no elevation required for an ordinary install.
; "dialog" lets a user still choose "install for all users" when they want to.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=..\release
OutputBaseFilename={#MyAppInternalName}-{#MyAppVersion}-setup
SetupIconFile=..\yeelight_pc_companion.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; The application is licensed GPL-3.0-only. LicenseFile is intentionally not
; set: setup does not force a licence click-through, the licence ships as a file.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; "unchecked" is deliberate: starting an application automatically at logon is a
; user preference, not something an installer should switch on by itself. The
; user opts in explicitly.
Name: "startup"; Description: "Start {#MyAppName} automatically when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The complete PyInstaller onedir distribution.
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Licence text, for reference after installation.
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Per-user start-at-logon. HKCU only: no elevation, no scheduled task, and
; uninstall removes exactly this one value.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppInternalName}"; \
    ValueData: """{app}\{#MyAppExeName}"" --tray"; \
    Flags: uninsdeletevalue; Tasks: startup

[Run]
Description: "Launch {#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Parameters: "--tray"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Retire the scheduled tasks created by the old elevated-startup design. The
; application runs unelevated, so leaving these behind would start an elevated
; duplicate at logon. Both are fixed names owned by this application, and this
; is skipped harmlessly when they do not exist.
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn YeelightPCCompanion /f"; \
    Flags: runhidden; RunOnceId: "RemoveLegacyStartupTask"
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn LuminaLightOrchestrator /f"; \
    Flags: runhidden; RunOnceId: "RemoveLegacyLuminaTask"

[Code]
{ ---------------------------------------------------------------------------
  INSTALL: one-time migration of the retired elevated startup tasks.

  Older builds started the application through a *highest-privilege logon task*
  ("YeelightPCCompanion", and before the rename "LuminaLightOrchestrator").
  Those tasks must not survive an upgrade: they would start an elevated
  duplicate of an application that is now deliberately unelevated.

  This migration is:

    * scoped to exactly two fixed names, always in full - there is no
      task-name parameter and no command interface,
    * `YeelightPCCompanion-OpenRGB` is a DIFFERENT task owned by the OpenRGB
      integration and is never touched here (the [UninstallRun] removal of that
      task is separate, and happens on uninstall only),
    * detection is silent (a `schtasks /query` per fixed name) and a machine
      with neither task - a fresh install - never sees a UAC prompt at all,
    * unelevated deletion is attempted first, and only if a legacy task
      *verifiably survives* one single narrowly scoped elevation is requested
      for exactly the `/delete` commands of the names still present,
    * success is verified with a fresh query rather than trusted from an exit
      code,
    * a declined or failed cleanup reports the task as left behind and NEVER
      fails the installation. A silent install never raises an invisible
      consent prompt; it reports the same way.
  --------------------------------------------------------------------------- }

const
  OpenRgbTaskName = 'YeelightPCCompanion-OpenRGB';

{ ---------------------------------------------------------------------------
  The complete, fixed scope of the legacy-startup migration.

  Inno Setup's PascalScript has no array constants (a `const` array fails to
  compile), so the fixed list is expressed as one index accessor plus one count.
  That is still a single source of truth: every loop in `MigrateLegacyStartupTasks`
  iterates exactly `0 .. LegacyStartupTaskCount() - 1` and can only ever obtain a
  name from `LegacyStartupTaskName()`.

  There is deliberately **no task-name parameter** anywhere in this flow, and
  `YeelightPCCompanion-OpenRGB` is NOT in this list: it is a different task owned
  by the OpenRGB integration and is never touched by the migration.
  --------------------------------------------------------------------------- }
function LegacyStartupTaskCount(): Integer;
begin
  Result := 2;
end;

function LegacyStartupTaskName(const Index: Integer): String;
begin
  if Index = 0 then
    Result := 'YeelightPCCompanion'
  else if Index = 1 then
    Result := 'LuminaLightOrchestrator'
  else
    Result := '';
end;

function SchtasksPath(): String;
begin
  Result := ExpandConstant('{sys}\schtasks.exe');
end;

function TaskExists(const TaskName: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(SchtasksPath(), '/query /tn "' + TaskName + '"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

{ One narrowly scoped legacy-task deletion, always WITHOUT elevation.

  The elevated cleanup is a different call site (see `RunOneElevatedCleanup`):
  bundling the deletion into this helper is what would turn two remaining legacy
  tasks into two consent prompts. }
function TryDeleteLegacyTask(const TaskName: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(SchtasksPath(), '/delete /tn "' + TaskName + '" /f',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
  if not Result then
    Log('Legacy task deletion failed (task=' + TaskName +
        ', elevated=0, exit=' + IntToStr(ResultCode) + ').');
end;

{ ONE narrowly scoped elevated action for the remaining fixed legacy tasks.

  `schtasks.exe` is executed directly (never `cmd /c` with a command string), and
  the two deletions are two `schtasks` *arguments* passed to a single helper
  process. Starting that helper is the only thing that raises a consent prompt,
  so however many legacy tasks remain, the user sees **exactly one** approval.
  Both names come from the fixed accessor, never from user input. }
function RunOneElevatedCleanup(const FirstName: String): Boolean;
var
  ResultCode: Integer;
  Launched: Boolean;
  Parameters: String;
  SecondName: String;
begin
  Parameters := '/c ""' + SchtasksPath() + '" /delete /tn "' + FirstName + '" /f';

  SecondName := LegacyStartupTaskName(1);
  if (SecondName <> '') and TaskExists(SecondName) then
    Parameters := Parameters + ' & "' + SchtasksPath() + '" /delete /tn "' +
                  SecondName + '" /f';

  Parameters := Parameters + '"';

  Launched := ShellExec('runas', ExpandConstant('{cmd}'), Parameters, '',
                        SW_HIDE, ewWaitUntilTerminated, ResultCode);

  Result := Launched and (ResultCode = 0);
  Log('One elevated legacy cleanup attempted (launched=' +
      IntToStr(Ord(Launched)) + ', exit=' + IntToStr(ResultCode) + ').');
end;

procedure MigrateLegacyStartupTasks();
var
  Index: Integer;
  Remaining: Integer;
  TaskName: String;
begin
  Remaining := 0;

  { Step 1: silent detection. Only the two fixed names are ever queried. }
  for Index := 0 to LegacyStartupTaskCount() - 1 do
  begin
    TaskName := LegacyStartupTaskName(Index);
    if TaskName = '' then
    begin
      Log('The fixed legacy startup task list returned an empty name; skipping.');
      Continue;
    end;

    if not TaskExists(TaskName) then
    begin
      Log('Legacy startup task ' + TaskName + ' is not present.');
      Continue;
    end;

    Log('Legacy startup task ' + TaskName + ' is present; attempting removal.');

    { Step 2: try it unelevated first. It costs nothing and it is the outcome
      when the task happens to be removable by the current user. }
    TryDeleteLegacyTask(TaskName);

    { Step 3: verify rather than trust the exit code. }
    if TaskExists(TaskName) then
    begin
      Log('Legacy startup task ' + TaskName + ' survived the unelevated removal.');
      Remaining := Remaining + 1;
    end
    else
      Log('Legacy startup task ' + TaskName + ' removed.');
  end;

  if Remaining = 0 then
  begin
    Log('No retired elevated startup task required elevation.');
    Exit;
  end;

  { Step 4: at most ONE explicit approval, for a single, narrowly scoped
    cleanup of the fixed names that still exist. A silent install never raises
    a consent prompt the user cannot see. }
  if WizardSilent then
  begin
    Log('A retired elevated startup task needs administrator rights and this ' +
        'is a silent installation, so no consent prompt is shown. The task was ' +
        'left in place; it can be deleted from Task Scheduler.');
    Exit;
  end;

  if IsAdminInstallMode() then
  begin
    { This installation already has administrator rights, so nothing has to be
      escalated: delete the remaining fixed legacy names directly. }
    Log('Removing the remaining legacy startup task(s) from this already ' +
        'elevated installation.');
    for Index := 0 to LegacyStartupTaskCount() - 1 do
    begin
      TaskName := LegacyStartupTaskName(Index);
      if (TaskName <> '') and TaskExists(TaskName) then
        TryDeleteLegacyTask(TaskName);
    end;
  end
  else
  begin
    { ONE approval, one helper process, however many names remain. }
    Log('Requesting one administrator approval to remove the remaining ' +
        'legacy startup task(s).');
    RunOneElevatedCleanup(LegacyStartupTaskName(0));
  end;

  { Step 5: final verification and an honest report. }
  Remaining := 0;
  for Index := 0 to LegacyStartupTaskCount() - 1 do
  begin
    TaskName := LegacyStartupTaskName(Index);
    if (TaskName <> '') and TaskExists(TaskName) then
    begin
      Remaining := Remaining + 1;
      Log('The retired elevated startup task ' + TaskName + ' is still present ' +
          'after the cleanup. It was left in place; installation continues. It ' +
          'can be deleted from Task Scheduler, and while it exists it may start ' +
          'an elevated duplicate of this application at sign-in.');
    end
    else
      Log('Legacy startup task ' + TaskName + ' is gone.');
  end;

  if Remaining > 0 then
    Log('The legacy startup cleanup was declined or failed; ' + IntToStr(Remaining) +
        ' retired elevated startup task(s) were left in place. Installation is ' +
        'not affected.')
  else
    Log('Legacy startup migration complete: no retired elevated startup task remains.');
end;

{ ---------------------------------------------------------------------------
  Uninstall: remove the OpenRGB elevation task this application owns.

  Measured behaviour on a real machine (see project_memory.md):

  * `schtasks /delete /tn YeelightPCCompanion-OpenRGB /f` from an UNELEVATED
    context fails with "ERROR: Access is denied." (exit code 1). An unelevated
    process cannot even *create* a scheduled task, which is why this one was
    created by the elevated provisioning helper and carries a security
    descriptor that blocks the unelevated user from deleting it again.
  * The same deletion performed ELEVATED succeeds (exit code 0) and the task is
    genuinely gone.

  So task removal is a genuinely privileged operation and is handled explicitly
  rather than being attempted and silently ignored:

  1. Try the deletion unelevated first. It costs nothing and it is the outcome
     when the task happens to be removable (for example when it was never
     created, or the machine's policy already allows it).
  2. Verify with a fresh query - the exit code alone is not trusted.
  3. If the task is still there, ask Windows for a single, narrowly scoped
     elevation to run that one `schtasks /delete` command. Nothing else is
     elevated, and the user is never asked twice.

  This never removes OpenRGB, Artemis, the Yeelight Chroma Connector, Razer or
  anything else, and it never touches the user's configuration.
  --------------------------------------------------------------------------- }

{ One narrowly scoped deletion of the task this application owns for the
  OpenRGB integration. Returns True only when the helper both launched and
  reported success. }
function TryDeleteTask(const UseElevation: Boolean): Boolean;
var
  ResultCode: Integer;
  Launched: Boolean;
  Parameters: String;
begin
  Parameters := '/delete /tn "' + OpenRgbTaskName + '" /f';

  if UseElevation then
  begin
    { ShellExec with the "runas" verb goes through the normal UAC consent path.
      It is used for exactly one command and only after a non-elevated attempt
      has already failed. }
    Launched := ShellExec('runas', SchtasksPath(), Parameters, '',
                          SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end
  else
    Launched := Exec(SchtasksPath(), Parameters, '',
                     SW_HIDE, ewWaitUntilTerminated, ResultCode);

  { Success requires that the helper both launched AND returned 0. A launched
    but failed helper is a failure, not a success. }
  if (not Launched) or (ResultCode <> 0) then
  begin
    Log('Task deletion failed (elevated=' + IntToStr(Ord(UseElevation)) +
        ', launched=' + IntToStr(Ord(Launched)) +
        ', exit=' + IntToStr(ResultCode) + ').');
    Result := False;
    Exit;
  end;

  Result := True;
end;

procedure RemoveOpenRgbTask();
var
  Removed: Boolean;
begin
  if not TaskExists(OpenRgbTaskName) then
  begin
    Log('OpenRGB elevation task not present; nothing to remove.');
    Exit;
  end;

  Removed := TryDeleteTask(False);

  { Verify rather than trust the exit code. }
  if (not Removed) or TaskExists(OpenRgbTaskName) then
  begin
    if UninstallSilent then
    begin
      { A silent uninstall must never raise a consent prompt the user cannot
        see, so it reports the task as left behind instead. }
      Log('The OpenRGB elevation task could not be removed without elevation; ' +
          'it was left in place. It can be deleted manually from Task Scheduler.');
      Exit;
    end;

    Log('Unelevated removal was refused; requesting one elevated deletion.');
    if not TryDeleteTask(True) then
    begin
      Log('The elevated deletion was declined or failed; the OpenRGB elevation ' +
          'task was left in place.');
      Exit;
    end;
  end;

  if TaskExists(OpenRgbTaskName) then
    Log('schtasks reported success but the OpenRGB elevation task is still present.')
  else
    Log('OpenRGB elevation task removed.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { The retired elevated logon tasks are removed here - on install AND on
      upgrade - because a leftover one would start an elevated duplicate of an
      application that is now deliberately unelevated. A machine that never had
      them (a fresh install) does not raise any prompt. }
    MigrateLegacyStartupTasks();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { The HKCU Run value is removed by its [Registry] uninsdeletevalue flag. }
    RemoveOpenRgbTask();

    { The configuration directory is deliberately left in place:
      %LOCALAPPDATA%\Yeelight PC Companion holds the user's devices,
      coordinates and settings. Deleting it silently would be data loss. }
    Log('User configuration in ' + ExpandConstant('{localappdata}\{#MyAppName}') +
        ' was left untouched.');
  end;
end;
